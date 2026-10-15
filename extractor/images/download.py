from __future__ import annotations

import asyncio
import errno
import hashlib
import os
import threading
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

import httpx
from sqlalchemy import Engine
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..config import ImagesConfig
from .resolve import Candidate, Job, plan_job


class SFTPLike(Protocol):
    def stat(self, path: str): ...
    def get(self, remotepath: str, localpath: str): ...


@dataclass
class Outcome:
    kind: str  # ok | not_found | error | unresolved
    http_status: int | None = None
    via: str | None = None
    size: int | None = None
    sha256: str | None = None
    content_type: str | None = None
    error: str | None = None
    url: str | None = None


@dataclass
class Summary:
    attempted: int
    counts: dict[str, int]  # final status of every job in the manifest
    samples: list[tuple[str, str, str]] = field(default_factory=list)  # (status, where, why) of a few failures
    unresolved_columns: list[tuple[str, int]] = field(default_factory=list)  # ("table.column", refs) with no source


class _Retry(Exception):
    pass


async def _http_once(client: httpx.AsyncClient, url: str, target: Path) -> Outcome:
    async with client.stream("GET", url) as r:
        code = r.status_code
        if code in (404, 410):
            return Outcome("not_found", http_status=code, via="http", url=url)
        if code >= 500 or code == 429:
            raise _Retry(f"HTTP {code}")
        if code != 200:
            return Outcome("error", http_status=code, via="http", url=url, error=f"HTTP {code}")
        ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype.startswith("text/") or ctype in {"application/json", "application/xml"}:
            return Outcome("error", http_status=code, via="http", url=url, content_type=ctype, error="response is not an image")
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.name + ".part")
        digest, size = hashlib.sha256(), 0
        try:
            with part.open("wb") as f:
                async for chunk in r.aiter_bytes(65536):
                    f.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
            if size == 0:
                return Outcome("error", http_status=code, via="http", url=url, error="empty response")
            os.replace(part, target)
        finally:
            part.unlink(missing_ok=True)
        return Outcome("ok", http_status=code, via="http", size=size, sha256=digest.hexdigest(), content_type=ctype, url=url)


async def _http_fetch(client: httpx.AsyncClient, url: str, target: Path, cfg: ImagesConfig) -> Outcome:
    try:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(max(1, cfg.max_retries)),
            wait=wait_exponential(multiplier=cfg.retry_wait, max=30),
            retry=retry_if_exception_type((_Retry, httpx.TransportError)),
            reraise=True,
        ):
            with attempt:
                return await _http_once(client, url, target)
    except _Retry as e:
        return Outcome("error", via="http", url=url, error=str(e))
    except httpx.HTTPError as e:
        return Outcome("error", via="http", url=url, error=f"{type(e).__name__}: {e}")
    return Outcome("error", via="http", url=url, error="unreachable")


def _sftp_fetch(sftp: SFTPLike, lock: threading.Lock, remote: str, target: Path) -> Outcome:
    part = target.with_name(target.name + ".part")
    try:
        with lock:
            try:
                sftp.stat(remote)
            except OSError as e:
                if isinstance(e, FileNotFoundError) or e.errno == errno.ENOENT:
                    return Outcome("not_found", via="sftp", url=remote)
                raise
            target.parent.mkdir(parents=True, exist_ok=True)
            sftp.get(remote, str(part))
        digest, size = hashlib.sha256(), 0
        with part.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                digest.update(chunk)
                size += len(chunk)
        if size == 0:
            return Outcome("error", via="sftp", url=remote, error="empty file")
        os.replace(part, target)
        return Outcome("ok", via="sftp", size=size, sha256=digest.hexdigest(), url=remote)
    except Exception as e:  # network/permission problems should not abort the whole run
        return Outcome("error", via="sftp", url=remote, error=f"{type(e).__name__}: {e}")
    finally:
        part.unlink(missing_ok=True)


async def _run_job(job: Job, target: Path, client, sftp, lock, cfg: ImagesConfig) -> Outcome:
    if not job.candidates:
        return Outcome("unresolved", error="no matching image source in config, or the file name has no date for {year}")
    last: Outcome | None = None
    for cand in job.candidates:
        if cand.kind == "http":
            outcome = await _http_fetch(client, cand.target, target, cfg)
        elif sftp is not None:
            outcome = await asyncio.to_thread(_sftp_fetch, sftp, lock, cand.target, target)
        else:
            outcome = Outcome("error", via="sftp", url=cand.target, error="SFTP not available (no ssh config)")
        if outcome.kind == "ok":
            return outcome
        if last is None or outcome.kind == "error":
            last = outcome  # prefer a retryable error over a plain "not found"
    return last  # type: ignore[return-value]


_CREATE_IMAGES = (
    "CREATE TABLE IF NOT EXISTS _images (job_key TEXT PRIMARY KEY, local_path TEXT, url TEXT, status TEXT, "
    "http_status INTEGER, via TEXT, size INTEGER, sha256 TEXT, content_type TEXT, error TEXT, fetched_at TEXT, "
    "tried_sig TEXT)"
)


def _sig(job: Job) -> str:
    """Fingerprint of where we look for a file. A failure is only final for the same places."""
    return hashlib.sha1("|".join(f"{c.kind}:{c.target}" for c in job.candidates).encode()).hexdigest()[:16]


async def check_job(job: Job, cfg: ImagesConfig, sftp: SFTPLike | None, tmp_dir: Path) -> list[tuple[Candidate, Outcome]]:
    """Try each place a file might be, once, without retries or keeping the file. For troubleshooting."""
    results: list[tuple[Candidate, Outcome]] = []
    tmp_dir.mkdir(parents=True, exist_ok=True)
    target = tmp_dir / "check.bin"
    async with httpx.AsyncClient(timeout=cfg.timeout, follow_redirects=True, headers={"User-Agent": cfg.user_agent}) as client:
        for cand in job.candidates:
            if cand.kind == "http":
                try:
                    outcome = await _http_once(client, cand.target, target)
                except _Retry as e:
                    outcome = Outcome("error", via="http", url=cand.target, error=str(e))
                except httpx.HTTPError as e:
                    outcome = Outcome("error", via="http", url=cand.target, error=f"{type(e).__name__}: {e}")
            elif sftp is not None:
                outcome = await asyncio.to_thread(_sftp_fetch, sftp, threading.Lock(), cand.target, target)
            else:
                outcome = Outcome("error", via="sftp", url=cand.target, error="SFTP not available (no ssh config)")
            target.unlink(missing_ok=True)
            results.append((cand, outcome))
    return results


async def download_all(
    dest: Engine,
    cfg: ImagesConfig,
    out_dir: Path,
    sftp: SFTPLike | None = None,
    retry_failed: bool = False,
    on_progress: Callable[[int], None] | None = None,
) -> Summary:
    """Download every image referenced in `_image_refs` into <out_dir>/images/.

    Resumable: jobs already `ok` (and still on disk) are skipped. Failed jobs are only
    re-attempted with retry_failed=True.
    """
    with dest.begin() as c:
        c.exec_driver_sql(_CREATE_IMAGES)
        refs = c.exec_driver_sql("SELECT id, table_name, column_name, raw_ref FROM _image_refs").fetchall()
        if "tried_sig" not in {r[1] for r in c.exec_driver_sql("PRAGMA table_info(_images)")}:
            c.exec_driver_sql("ALTER TABLE _images ADD COLUMN tried_sig TEXT")  # manifest from an older version
        existing = {k: (st, sg) for k, st, sg in c.exec_driver_sql("SELECT job_key, status, tried_sig FROM _images")}

    jobs: dict[str, Job] = {}
    ref_keys: list[tuple[str | None, int]] = []
    for ref_id, table, column, raw in refs:
        job = plan_job(raw, table, column, cfg)
        if job is None:
            ref_keys.append((None, ref_id))
            continue
        jobs.setdefault(job.key, job)
        ref_keys.append((job.key, ref_id))
    if ref_keys:
        with dest.begin() as c:
            c.exec_driver_sql("UPDATE _image_refs SET job_key = ? WHERE id = ?", ref_keys)

    img_dir = out_dir / "images"

    def needs_run(job: Job) -> bool:
        if job.key not in existing:
            return True
        status, tried = existing[job.key]
        if status == "ok":
            return not (img_dir / job.local_rel).exists()
        # Failed before: retry on request, or automatically if the places to look have changed since.
        return retry_failed or tried != _sig(job)

    pending = [j for j in jobs.values() if needs_run(j)]
    sem = asyncio.Semaphore(cfg.concurrency)
    lock = threading.Lock()

    async with httpx.AsyncClient(
        timeout=cfg.timeout, follow_redirects=True, headers={"User-Agent": cfg.user_agent}
    ) as client:

        async def worker(job: Job) -> None:
            async with sem:
                outcome = await _run_job(job, img_dir / job.local_rel, client, sftp, lock, cfg)
            with dest.begin() as c:
                c.exec_driver_sql(
                    "INSERT OR REPLACE INTO _images VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        job.key,
                        f"images/{job.local_rel}",
                        outcome.url,
                        outcome.kind,
                        outcome.http_status,
                        outcome.via,
                        outcome.size,
                        outcome.sha256,
                        outcome.content_type,
                        outcome.error,
                        datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        _sig(job),
                    ),
                )
            if on_progress:
                on_progress(1)

        await asyncio.gather(*(worker(j) for j in pending))

    with dest.connect() as c:
        counts = Counter(s for (s,) in c.exec_driver_sql("SELECT status FROM _images"))
        samples = [
            (st, url or "", err or (f"HTTP {code}" if code else ""))
            for st, url, err, code in c.exec_driver_sql(
                "SELECT status, url, error, http_status FROM _images WHERE status NOT IN ('ok', 'unresolved') "
                "ORDER BY CASE status WHEN 'error' THEN 0 ELSE 1 END LIMIT 5"
            )
        ]
        unresolved = [
            (f"{t}.{col}", n)
            for t, col, n in c.exec_driver_sql(
                "SELECT r.table_name, r.column_name, COUNT(*) FROM _image_refs r JOIN _images i ON i.job_key = r.job_key "
                "WHERE i.status = 'unresolved' GROUP BY 1, 2 ORDER BY 3 DESC"
            )
        ]
    return Summary(len(pending), dict(counts), samples, unresolved)
