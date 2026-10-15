from __future__ import annotations

import hashlib
import posixpath
import re
from dataclasses import dataclass, field
from urllib.parse import quote, unquote, urlparse

from ..config import ImagesConfig
from .detect import matches

_BAD_CHARS = re.compile(r"[^\w.\-+=,@() ]")


@dataclass
class Candidate:
    kind: str  # "http" | "sftp"
    target: str  # URL or remote path


@dataclass
class Job:
    key: str  # identical keys are downloaded once
    local_rel: str  # path under <output>/images/
    candidates: list[Candidate] = field(default_factory=list)


def _safe_local(rel: str, salt: str = "") -> str:
    parts = [p for p in rel.split("/") if p not in ("", ".", "..")]
    parts = [_BAD_CHARS.sub("_", p) for p in parts]
    path = "/".join(parts)
    if salt:
        stem, dot, ext = path.rpartition(".")
        path = f"{stem}_{salt}.{ext}" if dot else f"{path}_{salt}"
    if len(path) > 200 or not path:
        ext = posixpath.splitext(path)[1][:10]
        path = hashlib.sha1(rel.encode()).hexdigest() + ext
    return path


def plan_job(raw: str, table: str, column: str, cfg: ImagesConfig) -> Job | None:
    """Turn a raw DB reference into a download job. None = not downloadable (e.g. data: URI)."""
    raw = raw.strip()
    if raw.lower().startswith("data:"):
        return None
    if raw.startswith("//"):
        raw = "https:" + raw

    parsed = urlparse(raw)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        salt = hashlib.sha1(parsed.query.encode()).hexdigest()[:8] if parsed.query else ""
        rel = _safe_local(f"{parsed.netloc}/{unquote(parsed.path)}", salt)
        return Job(f"abs:{raw}", rel, [Candidate("http", raw)])

    rel_path = re.sub(r"^(\./|/)+", "", unquote(raw.split("?")[0]))
    if ".." in rel_path.split("/"):
        return Job(f"bad:{raw}", _safe_local(raw), [])  # never follow path traversal

    sources = [s for s in cfg.sources if matches(s.columns, table, column)]
    if not sources:
        return Job(f"rel::{rel_path}", _safe_local(f"_unresolved/{rel_path}"), [])

    candidates: list[Candidate] = []
    for s in sources:
        if s.base_url:
            candidates.append(Candidate("http", s.base_url.rstrip("/") + "/" + quote(rel_path)))
        if s.sftp_dir:
            candidates.append(Candidate("sftp", posixpath.join(s.sftp_dir, rel_path)))
    key = f"rel:{'+'.join(s.name for s in sources)}:{rel_path}"
    return Job(key, _safe_local(f"{sources[0].name}/{rel_path}"), candidates)
