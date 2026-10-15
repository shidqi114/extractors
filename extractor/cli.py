from __future__ import annotations

import asyncio
from contextlib import contextmanager
from pathlib import Path

import typer
from rich.console import Console
from rich.progress import Progress
from rich.table import Table

from .config import Config, ImageSource, load_config
from .dump import dump_tables, make_source_engine, open_dest, sqlite_source_engine
from .export import export_all
from .images.detect import Detector, scan_refs, select_columns
from .images.resolve import plan_job
from .images.download import check_job, download_all
from .link import link as link_images
from .organize import OrganizeError
from .organize import organize as organize_output
from .probe import probe as run_probe
from .state import State
from .tunnel import SSHSession

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Extract a legacy VPS database and its images.")
images_app = typer.Typer(no_args_is_help=True, help="Find and download images referenced by the database.")
app.add_typer(images_app, name="images")
console = Console()

ConfigOpt = typer.Option("config.yaml", "--config", "-c", help="Path to the YAML config.")


@contextmanager
def _ssh(cfg: Config):
    if cfg.ssh is None:
        yield None
        return
    with SSHSession(cfg.ssh) as session:
        yield session


@contextmanager
def _source_engine(cfg: Config, ssh: SSHSession | None):
    db = cfg.db
    if db.dialect == "sqlite":
        if not db.sqlite_remote_path:
            raise typer.BadParameter("db.sqlite_remote_path is required for dialect sqlite")
        path = Path(db.sqlite_remote_path)
        if ssh is not None:
            path = cfg.output_dir / "source.sqlite"
            path.parent.mkdir(parents=True, exist_ok=True)
            ssh.sftp().get(db.sqlite_remote_path, str(path))
        engine = sqlite_source_engine(path)
    else:
        port = db.port or db.default_port
        if ssh is not None:
            host, port = "127.0.0.1", ssh.forward(db.host, port)
        else:
            host = db.host
        engine = make_source_engine(db, host, port)
    try:
        yield engine
    finally:
        engine.dispose()


def _needs_sftp(cfg: Config) -> bool:
    return cfg.ssh is not None and any(s.sftp_dir for s in cfg.images.sources)


def _dump(cfg: Config, ssh, tables: list[str] | None, fresh: bool) -> None:
    dest = open_dest(cfg.output_dir)
    state = State(cfg.output_dir / "state.json")
    if fresh:
        state.reset_dump()
    with _source_engine(cfg, ssh) as source:
        results = dump_tables(source, dest, cfg.dump, state, only=tables, on_table=lambda r: console.print(
            f"  {'[green]ok[/]' if r.ok else '[red]MISMATCH[/]'} {r.table}: {r.local_rows}/{r.source_rows} rows"
        ))
    skipped = [r.table for r in results if r.skipped]
    if skipped:
        console.print(f"Skipped (already dumped): {', '.join(skipped)}. Use --fresh to redo.")
    bad = [r for r in results if not r.ok]
    if bad:
        console.print(f"[red]{len(bad)} table(s) did not match the source row count; re-run to retry.[/]")
        raise typer.Exit(1)
    console.print(f"[green]Dumped {len(results)} table(s) to {cfg.output_dir / 'db.sqlite'}[/]")


def _scan(cfg: Config, write: bool = True) -> None:
    dest = open_dest(cfg.output_dir)
    det = Detector(cfg.images.extensions)
    reports = select_columns(dest, cfg.images, det)
    if write:
        scan_refs(dest, det, reports)
    table = Table(title="Image columns")
    for h in ("table.column", "sampled", "hit %", "selected", "refs", "distinct"):
        table.add_column(h)
    for r in reports:
        table.add_row(
            f"{r.table}.{r.column}", str(r.sampled), f"{r.ratio:.0%}",
            ("forced" if r.forced else "auto") if r.selected else "no (below threshold)",
            str(r.refs), str(r.distinct),
        )
    console.print(table)
    if not any(r.selected for r in reports):
        console.print("[yellow]No image columns found. Use images.include_columns to force one.[/]")


def _download(cfg: Config, ssh, retry_failed: bool) -> None:
    dest = open_dest(cfg.output_dir)
    sftp = ssh.sftp() if (ssh is not None and _needs_sftp(cfg)) else None
    with Progress(console=console) as progress:
        task = progress.add_task("Downloading images", total=None)
        summary = asyncio.run(
            download_all(dest, cfg.images, cfg.output_dir, sftp, retry_failed, lambda n: progress.advance(task, n))
        )
    ok = summary.counts.get("ok", 0)
    failed = sum(n for status, n in summary.counts.items() if status != "ok")
    console.print(f"Attempted {summary.attempted}. Images on disk: {ok}. Failed: {failed}. Status: {summary.counts}")
    if summary.unresolved_columns:
        console.print("[yellow]No images.sources entry covers these columns, so their images were skipped:[/]")
        for column, n in summary.unresolved_columns:
            console.print(f"  {column}: {n} reference(s)  -> add \"{column}\" to a source's `columns` in config.yaml")
    if summary.samples:
        console.print("[bold]Examples of failures (the exact address that was tried):[/]")
        for status, where, why in summary.samples:
            console.print(f"  {status}: {where} {why}".rstrip())
    if summary.attempted == 0 and failed:
        console.print(
            f"[yellow]{failed} image(s) failed earlier and were not retried because the sources are unchanged. "
            "Use --retry-failed to try them again.[/]"
        )
    if ok == 0 and failed:
        console.print(
            "[red]No image was downloaded.[/] The base_url / sftp_dir in config.yaml are probably wrong. "
            "Test one image with:  extractor images check --base-url https://YOUR-SITE/PATH/"
        )


def _link_and_export(cfg: Config) -> None:
    dest = open_dest(cfg.output_dir)
    added = link_images(dest)
    for t, cols in added.items():
        console.print(f"  added {', '.join(cols)} to {t}")
    counts = export_all(dest, cfg.output_dir)
    console.print(f"[green]Exported {len(counts)} tables to {cfg.output_dir}/csv and /jsonl[/]")


def _organize(cfg: Config, hardlink: bool = False) -> None:
    try:
        s = organize_output(open_dest(cfg.output_dir), cfg.output_dir, cfg.organize, hardlink)
    except OrganizeError as e:
        console.print(f"[red]Organize failed:[/] {e}")
        raise typer.Exit(1)
    console.print(
        f"[green]Organized {s.houses} houses into {cfg.output_dir / cfg.organize.houses_dir} "
        f"and {s.agents} agents into {cfg.output_dir / cfg.organize.agents_dir}[/]"
    )
    console.print(f"  photos placed: {s.photos_placed}, referenced but unavailable: {s.photos_unavailable}")
    for label, n in (
        ("houses without an agent", s.houses_without_agent),
        ("houses pointing at an unknown agent", s.unknown_agent),
        ("photo rows whose house doesn't exist", s.orphan_photos),
        ("photo rows with no image link", s.photo_rows_without_image),
        ("house rows without an id (skipped)", s.houses_skipped),
    ):
        if n:
            console.print(f"  [yellow]{label}: {n}[/]")


@app.command()
def probe(config: Path = ConfigOpt, find_file: str = typer.Option(None, help="A known filename to locate on the VPS.")):
    """Read-only look at the VPS: DB engine, candidate files, and where an image file lives."""
    cfg = load_config(config)
    if cfg.ssh is None:
        raise typer.BadParameter("probe needs an `ssh:` section in the config")
    with SSHSession(cfg.ssh) as ssh:
        result = run_probe(ssh, find_file)
    for key, value in result.items():
        console.print(f"[bold]{key}[/]")
        for line in value if isinstance(value, list) else [value]:
            console.print(f"  {line}")


@app.command()
def dump(
    config: Path = ConfigOpt,
    table: list[str] = typer.Option(None, "--table", "-t", help="Only this table (repeatable)."),
    fresh: bool = typer.Option(False, help="Ignore saved progress and re-copy everything."),
):
    """Copy the remote database into output/db.sqlite (read-only on the VPS)."""
    cfg = load_config(config)
    with _ssh(cfg) as ssh:
        _dump(cfg, ssh, table or None, fresh)


@images_app.command("scan")
def images_scan(config: Path = ConfigOpt, dry_run: bool = typer.Option(False, help="Only report; don't write refs.")):
    """Detect image columns in the local copy and list every reference."""
    _scan(load_config(config), write=not dry_run)


@images_app.command("check")
def images_check(
    config: Path = ConfigOpt,
    ref: str = typer.Option(None, help="An image value from the DB, e.g. 20220324111256_83rxz.jpg (default: the first one found)."),
    column: str = typer.Option(None, help="table.column the value belongs to (default: where it was found)."),
    base_url: str = typer.Option(None, help="Try this URL prefix instead of the configured sources."),
    sftp_dir: str = typer.Option(None, help="Try this folder on the VPS instead of the configured sources."),
):
    """Test where one image can be fetched from: shows the result for every place tried."""
    cfg = load_config(config)
    images_cfg = cfg.images.model_copy(deep=True)
    if base_url or sftp_dir:
        images_cfg.sources = [ImageSource(name="check", columns=["*"], base_url=base_url, sftp_dir=sftp_dir)]
    table_name, column_name = (column.split(".", 1) + [""])[:2] if column else (None, None)
    if ref is None:
        with open_dest(cfg.output_dir).connect() as c:
            row = c.exec_driver_sql("SELECT table_name, column_name, raw_ref FROM _image_refs LIMIT 1").fetchone()
        if row is None:
            raise typer.BadParameter("No image references yet. Run `extractor images scan` first, or pass --ref.")
        table_name, column_name, ref = table_name or row[0], column_name or row[1], row[2]
    job = plan_job(ref, table_name or "", column_name or "", images_cfg)
    if job is None or not job.candidates:
        console.print(f"[red]No place to look for '{ref}'.[/] It matches no source: check images.sources[].columns, "
                      "or pass --base-url / --sftp-dir.")
        raise typer.Exit(1)
    needs_ssh = cfg.ssh is not None and any(c.kind == "sftp" for c in job.candidates)
    with _ssh(cfg) if needs_ssh else _null() as ssh:
        results = asyncio.run(check_job(job, images_cfg, ssh.sftp() if ssh else None, cfg.output_dir / "_check"))
    console.print(f"Checking [bold]{ref}[/]")
    for cand, o in results:
        mark = "[green]FOUND[/]" if o.kind == "ok" else "[red]" + o.kind.upper() + "[/]"
        detail = f"HTTP {o.http_status}" if o.http_status else ""
        detail += f" {o.content_type or ''} {o.size or ''}B" if o.kind == "ok" else f" {o.error or ''}"
        console.print(f"  {mark} {cand.kind}: {cand.target} {detail}".rstrip())
    if not any(o.kind == "ok" for _, o in results):
        raise typer.Exit(1)


@images_app.command("download")
def images_download(
    config: Path = ConfigOpt,
    retry_failed: bool = typer.Option(False, help="Re-attempt images that failed or were not found."),
):
    """Download all referenced images into output/images/."""
    cfg = load_config(config)
    with _ssh(cfg) if _needs_sftp(cfg) else _null() as ssh:
        _download(cfg, ssh, retry_failed)


@app.command()
def link(config: Path = ConfigOpt):
    """Add <column>_local columns and the _image_links table, then export CSV/JSONL."""
    _link_and_export(load_config(config))


@app.command()
def export(config: Path = ConfigOpt):
    """Write CSV and JSONL for every table in the local copy (no linking)."""
    cfg = load_config(config)
    counts = export_all(open_dest(cfg.output_dir), cfg.output_dir)
    console.print(f"[green]Exported {len(counts)} tables[/]")


@app.command()
def organize(
    config: Path = ConfigOpt,
    hardlink: bool = typer.Option(False, help="Hard-link photos instead of copying (saves disk space)."),
):
    """Build houses/<id_rumah>/ (house.json + photos/) and agents/<id>/ from the local copy."""
    _organize(load_config(config), hardlink)


@app.command()
def run(
    config: Path = ConfigOpt,
    retry_failed: bool = typer.Option(False, help="Re-attempt images that failed or were not found."),
    fresh: bool = typer.Option(False, help="Re-copy all tables from scratch."),
    hardlink: bool = typer.Option(False, help="Hard-link photos into houses/ instead of copying."),
):
    """Everything in order: dump, scan, download, link, export, organize."""
    cfg = load_config(config)
    with _ssh(cfg) as ssh:
        _dump(cfg, ssh, None, fresh)
        _scan(cfg)
        _download(cfg, ssh, retry_failed)
    _link_and_export(cfg)
    _organize(cfg, hardlink)


@contextmanager
def _null():
    yield None


if __name__ == "__main__":
    app()
