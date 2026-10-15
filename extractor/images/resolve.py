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


_DATED = re.compile(r"^(\d{4})(\d{2})(\d{2})")


def _expand(template: str, rel_path: str) -> str | None:
    """Fill {year}/{month}/{day} from the date at the start of the file name (20260603142126_x.jpg).

    None when the template needs a date and the file name has none.
    """
    if "{" not in template:
        return template
    m = _DATED.match(posixpath.basename(rel_path))
    if not m:
        return None
    try:
        return template.format(year=m.group(1), month=m.group(2), day=m.group(3))
    except (KeyError, IndexError, ValueError):
        return None  # an unknown placeholder: treat as "no place to look" instead of crashing


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
        base = _expand(s.base_url, rel_path) if s.base_url else None
        if base:
            candidates.append(Candidate("http", base.rstrip("/") + "/" + quote(rel_path)))
        folder = _expand(s.sftp_dir, rel_path) if s.sftp_dir else None
        if folder:
            candidates.append(Candidate("sftp", posixpath.join(folder, rel_path)))
    key = f"rel:{'+'.join(s.name for s in sources)}:{rel_path}"
    return Job(key, _safe_local(f"{sources[0].name}/{rel_path}"), candidates)
