from __future__ import annotations

import base64
import csv
import json
from pathlib import Path

from sqlalchemy import Engine, inspect

from .dump import q


def _plain(v):
    return base64.b64encode(v).decode("ascii") if isinstance(v, (bytes, bytearray)) else v


def export_all(dest: Engine, out_dir: Path, batch: int = 5000) -> dict[str, int]:
    """Write every table of the local SQLite copy to csv/<table>.csv and jsonl/<table>.jsonl."""
    (out_dir / "csv").mkdir(parents=True, exist_ok=True)
    (out_dir / "jsonl").mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for table in sorted(inspect(dest).get_table_names()):
        n = 0
        with dest.connect() as c, (out_dir / "csv" / f"{table}.csv").open(
            "w", newline="", encoding="utf-8-sig"
        ) as fcsv, (out_dir / "jsonl" / f"{table}.jsonl").open("w", encoding="utf-8") as fjson:
            result = c.exec_driver_sql(f"SELECT * FROM {q(table)}")
            keys = list(result.keys())
            writer = csv.writer(fcsv)
            writer.writerow(keys)
            while rows := result.fetchmany(batch):
                for row in rows:
                    values = [_plain(v) for v in row]
                    writer.writerow(values)
                    fjson.write(json.dumps(dict(zip(keys, values)), ensure_ascii=False) + "\n")
                    n += 1
        counts[table] = n
    return counts
