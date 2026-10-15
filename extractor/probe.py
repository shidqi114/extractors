from __future__ import annotations

import posixpath
import re
import shlex

from .tunnel import SSHSession

_SKIP = "-not -path '/proc/*' -not -path '/sys/*' -not -path '/usr/*' -not -path '*/node_modules/*' -not -path '*/vendor/*'"


def probe(ssh: SSHSession, find_file: str | None = None) -> dict[str, list[str] | str]:
    """Read-only look at the VPS: which DB engine runs, and where files live.

    Only lists paths; it never prints the contents of config files (they hold passwords).
    """

    def lines(cmd: str) -> list[str]:
        return [ln for ln in ssh.run(cmd).splitlines() if ln.strip()]

    listening = lines("(ss -ltn 2>/dev/null || netstat -ltn 2>/dev/null) | grep -E ':(3306|5432)\\b'")
    procs = lines("ps -eo comm | sort -u | grep -Ei 'mysql|maria|postgres'")
    sqlite_files = lines(
        f"find / -xdev -type f \\( -name '*.sqlite' -o -name '*.sqlite3' -o -name '*.db' \\) -size +10k {_SKIP} "
        "-not -path '/var/lib/mysql/*' 2>/dev/null | head -20"
    )
    config_files = lines(
        "find / -xdev -type f \\( -name wp-config.php -o -name .env -o -name database.php "
        f"-o -name config.php -o -name koneksi.php \\) {_SKIP} 2>/dev/null | head -20"
    )

    text = " ".join(listening + procs).lower()
    if "3306" in text or "mysql" in text or "maria" in text:
        engine = "mysql"
    elif "5432" in text or "postgres" in text:
        engine = "postgresql"
    elif sqlite_files:
        engine = "sqlite (file candidates found)"
    else:
        engine = "unknown"

    result: dict[str, list[str] | str] = {
        "engine_guess": engine,
        "listening_ports": listening,
        "db_processes": procs,
        "sqlite_candidates": sqlite_files,
        "app_config_files": config_files,
    }
    if find_file:
        # No -xdev here: web roots are often on a separate mount (/var/www, /home, /data).
        found = lines(
            f"find / \\( -path /proc -o -path /sys \\) -prune -o -type f -name {shlex.quote(find_file)} -print 2>/dev/null | head -5"
        )
        result["find_file_matches"] = found
        result["suggested_sftp_dirs"] = sorted({posixpath.dirname(p) for p in found})
        # Fallback: the unique part of the name, in case the file was renamed or has a size suffix.
        token = re.sub(r"\.[A-Za-z0-9]+$", "", find_file).split("_")[-1]
        if not found and len(token) >= 4:
            result["similar_files"] = lines(
                f"find / \\( -path /proc -o -path /sys \\) -prune -o -type f -iname {shlex.quote('*' + token + '*')} -print 2>/dev/null | head -5"
            )
    result["web_roots"] = lines(
        "ls -d /var/www /var/www/* /srv/* /home/*/public_html /home/*/www /home/*/htdocs /usr/share/nginx/html "
        "/opt/lampp/htdocs /var/lib/docker 2>/dev/null | head -40"
    )
    return result
