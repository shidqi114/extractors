from __future__ import annotations

import json
import os
import re
import shutil
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Engine, inspect

from .config import OrganizeConfig
from .dump import q
from .export import _plain


class OrganizeError(Exception):
    pass


@dataclass
class OrganizeSummary:
    houses: int = 0
    houses_skipped: int = 0  # house row without an id
    photos_placed: int = 0
    photos_unavailable: int = 0  # referenced but not downloaded (404, error, ...)
    photo_rows_without_image: int = 0
    orphan_photos: int = 0  # photo rows whose house id matches no house
    agents: int = 0
    houses_without_agent: int = 0
    unknown_agent: int = 0  # house points at an agent id that is not in the agent table


def _name(value) -> str | None:
    """A folder/file-safe version of an id value."""
    if value is None:
        return None
    s = re.sub(r"[^\w.\-]", "_", str(value).strip())
    return s.strip(".") or None


_NAME_COLUMNS = ("nama_agent", "nama", "name", "agent_name", "nama_lengkap", "full_name", "fullname")


def _folder(value) -> str | None:
    """A readable folder name from free text such as an agent's name."""
    if value is None:
        return None
    s = re.sub(r"[^\w.\- ]", "_", str(value).strip())
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s or None


def _agent_folders(agents: list[dict], id_col: str, name_col: str) -> dict[str, str]:
    """agent id -> folder name. Same name (case-insensitive) twice, or no name: the id keeps them apart."""
    base: dict[str, str] = {}
    for a in agents:
        if a.get(id_col) is None:
            continue
        key = str(a[id_col]).strip()
        base[key] = _folder(a.get(name_col)) or f"agent_{_name(key)}"
    counts = Counter(v.lower() for v in base.values())
    return {k: v if counts[v.lower()] == 1 else f"{v}_{_name(k)}" for k, v in base.items()}


def _rows(conn, table: str) -> list[dict]:
    result = conn.exec_driver_sql(f"SELECT rowid AS _rowid, * FROM {q(table)}")
    keys = list(result.keys())
    return [dict(zip(keys, row)) for row in result.fetchall()]


def _clean(row: dict) -> dict:
    return {k: _plain(v) for k, v in row.items() if k != "_rowid"}


def _require(insp, table: str, columns: list[str | None]) -> None:
    have = [c["name"] for c in insp.get_columns(table)]
    missing = [c for c in columns if c and c not in have]
    if missing:
        raise OrganizeError(
            f"Table '{table}' has no column(s) {missing}. Available columns: {have}. "
            "Set the right names in the `organize:` section of config.yaml."
        )


def _place(out_dir: Path, src_rel: str | None, dst: Path, hardlink: bool) -> bool:
    """Copy (or hard-link) a downloaded image to dst. False if it isn't on disk."""
    if not src_rel:
        return False
    src = out_dir / src_rel
    if not src.is_file():
        return False
    if dst.is_file() and dst.stat().st_size == src.stat().st_size:
        return True
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.unlink(missing_ok=True)
    if hardlink:
        try:
            os.link(src, dst)
            return True
        except OSError:
            pass  # different filesystem etc.: fall back to a copy
    shutil.copy2(src, dst)
    return True


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def organize(dest: Engine, out_dir: Path, cfg: OrganizeConfig, hardlink: bool = False) -> OrganizeSummary:
    """Build houses/<id_rumah>/{house.json,photos/} and agents/<agent name>/agent.json.

    Reads the local SQLite copy after `link`; downloaded images are copied (or hard-linked)
    out of images/, so the download cache stays intact and the step can be re-run.
    """
    insp = inspect(dest)
    tables = set(insp.get_table_names())
    for table in (cfg.house_table, cfg.photo_table, cfg.agent_table):
        if table not in tables:
            raise OrganizeError(f"Table '{table}' is not in the local copy. Tables: {sorted(t for t in tables if not t.startswith('_'))}")
    if "_image_links" not in tables:
        raise OrganizeError("No _image_links table yet. Run `extractor link` (or `extractor run`) first.")

    agent_id_col = cfg.agent_id
    if not agent_id_col:
        pk = insp.get_pk_constraint(cfg.agent_table)["constrained_columns"]
        if len(pk) != 1:
            raise OrganizeError(
                f"Can't tell which column identifies an agent in '{cfg.agent_table}'. "
                "Set organize.agent_id in config.yaml."
            )
        agent_id_col = pk[0]
    _require(insp, cfg.house_table, [cfg.house_id, cfg.house_main_photo, cfg.house_agent])
    _require(insp, cfg.photo_table, [cfg.photo_id, cfg.photo_house])
    _require(insp, cfg.agent_table, [agent_id_col, cfg.agent_name])
    agent_cols = [c["name"] for c in insp.get_columns(cfg.agent_table)]
    name_col = cfg.agent_name or next(
        (c for want in _NAME_COLUMNS for c in agent_cols if c.lower() == want), None
    )
    if not name_col:
        raise OrganizeError(
            f"Can't tell which column of '{cfg.agent_table}' holds the agent's name. "
            f"Available columns: {agent_cols}. Set organize.agent_name in config.yaml."
        )

    with dest.connect() as c:
        houses = _rows(c, cfg.house_table)
        photos = _rows(c, cfg.photo_table)
        agents = _rows(c, cfg.agent_table)
        links = c.exec_driver_sql(
            "SELECT table_name, column_name, src_rowid, raw_ref, local_path, status FROM _image_links "
            "ORDER BY table_name, src_rowid, position"
        ).fetchall()

    links_by: dict[tuple[str, int], list] = {}
    for link in links:
        links_by.setdefault((link[0], link[2]), []).append(link)

    photos_by_house: dict[str, list[dict]] = {}
    house_ids = {_name(h.get(cfg.house_id)) for h in houses}
    summary = OrganizeSummary()
    for p in photos:
        hid = _name(p.get(cfg.photo_house))
        if hid in house_ids and hid is not None:
            photos_by_house.setdefault(hid, []).append(p)
        else:
            summary.orphan_photos += 1

    agent_by_id = {str(a[agent_id_col]).strip(): a for a in agents if a.get(agent_id_col) is not None}
    folders = _agent_folders(agents, agent_id_col, name_col)
    houses_of_agent: dict[str, list[str]] = {}
    houses_root = out_dir / cfg.houses_dir
    agents_root = out_dir / cfg.agents_dir

    for h in houses:
        hid = _name(h.get(cfg.house_id))
        if hid is None:
            summary.houses_skipped += 1
            continue
        hdir = houses_root / hid
        (hdir / "photos").mkdir(parents=True, exist_ok=True)

        main = None
        for ln in links_by.get((cfg.house_table, h["_rowid"]), []):
            if ln[1] != cfg.house_main_photo:
                continue
            base = _name(Path(ln[4]).name) if ln[4] else _name(Path(ln[3]).name)
            rel = f"photos/main_{base}"
            placed = _place(out_dir, ln[4], hdir / rel, hardlink)
            summary.photos_placed += placed
            summary.photos_unavailable += not placed
            main = {
                "file": rel if placed else None,
                "original": ln[3],
                "status": "ok" if placed else (ln[5] if ln[5] and ln[5] != "ok" else "missing_on_disk"),
            }
            break

        photo_entries = []
        for p in photos_by_house.get(hid, []):
            pid = _name(p.get(cfg.photo_id)) or f"row{p['_rowid']}"
            images, used = [], set()
            for ln in links_by.get((cfg.photo_table, p["_rowid"]), []):
                base = _name(Path(ln[4]).name) if ln[4] else _name(Path(ln[3]).name)
                fname = f"{pid}_{base}"
                if fname in used:
                    fname = f"{pid}_{len(used)}_{base}"
                used.add(fname)
                placed = _place(out_dir, ln[4], hdir / "photos" / fname, hardlink)
                summary.photos_placed += placed
                summary.photos_unavailable += not placed
                images.append({
                    "file": f"photos/{fname}" if placed else None,
                    "original": ln[3],
                    "status": "ok" if placed else (ln[5] if ln[5] and ln[5] != "ok" else "missing_on_disk"),
                })
            if not images:
                summary.photo_rows_without_image += 1
            photo_entries.append({cfg.photo_id: p.get(cfg.photo_id), "row": _clean(p), "images": images})

        agent_ref = None
        raw_agent = h.get(cfg.house_agent)
        aid_key = str(raw_agent).strip() if raw_agent is not None else ""
        if not aid_key:
            summary.houses_without_agent += 1
        elif aid_key in agent_by_id:
            houses_of_agent.setdefault(aid_key, []).append(hid)
            agent_ref = {
                "id": aid_key,
                "name": agent_by_id[aid_key].get(name_col),
                "file": f"../../{cfg.agents_dir}/{folders[aid_key]}/agent.json",
            }
        else:
            summary.unknown_agent += 1
            agent_ref = {"id": aid_key, "file": None}

        _write_json(hdir / "house.json", {
            cfg.house_id: h.get(cfg.house_id),
            "house": _clean(h),
            "main_photo": main,
            "photos": photo_entries,
            "agent": agent_ref,
        })
        summary.houses += 1

    for a in agents:
        if a.get(agent_id_col) is None:
            continue
        key = str(a[agent_id_col]).strip()
        adir = agents_root / folders[key]
        files = []
        for col, value in a.items():  # images linked to the agent (e.g. a portrait column)
            if col.endswith("_local") and value:
                name = _name(Path(value).name)
                if _place(out_dir, value, adir / name, hardlink):
                    files.append(name)
        _write_json(adir / "agent.json", {
            agent_id_col: a.get(agent_id_col),
            "agent": _clean(a),
            "files": files,
            "houses": houses_of_agent.get(key, []),
        })
        summary.agents += 1
    return summary
