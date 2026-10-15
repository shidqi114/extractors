from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import Column, Engine, MetaData, Table, create_engine, event, func, inspect, select
from sqlalchemy import types as T
from sqlalchemy.engine import URL

from .config import DBConfig, DumpConfig
from .state import State

_ZERO_DATES = {"0000-00-00", "0000-00-00 00:00:00"}


def q(name: str) -> str:
    """Quote an SQL identifier."""
    return '"' + name.replace('"', '""') + '"'


def make_source_engine(db: DBConfig, host: str, port: int) -> Engine:
    """Engine for the old DB. Every connection is switched to read-only mode."""
    mysql = db.dialect == "mysql"
    url = URL.create(
        "mysql+pymysql" if mysql else "postgresql+psycopg",
        username=db.user,
        password=db.password,
        host=host,
        port=port,
        database=db.name,
        query={"charset": "utf8mb4"} if mysql else {},
    )
    engine = create_engine(url, pool_pre_ping=True)
    read_only = "SET SESSION TRANSACTION READ ONLY" if mysql else "SET default_transaction_read_only = on"

    @event.listens_for(engine, "connect")
    def _set_read_only(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        try:
            cur.execute(read_only)
        finally:
            cur.close()
        dbapi_conn.commit()

    return engine


def sqlite_source_engine(path: Path | str) -> Engine:
    return create_engine(f"sqlite:///file:{path}?mode=ro&uri=true")


def open_dest(out_dir: Path) -> Engine:
    out_dir.mkdir(parents=True, exist_ok=True)
    return create_engine(f"sqlite:///{out_dir / 'db.sqlite'}")


def normalise(v):
    """Make a source value storable in SQLite and stable in CSV/JSON."""
    if v is None or isinstance(v, (int, float, bytes)):
        return v
    if isinstance(v, str):
        return None if v in _ZERO_DATES else v
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (datetime, date, time)):
        return v.isoformat()
    if isinstance(v, timedelta):
        return str(v)
    if isinstance(v, (bytearray, memoryview)):
        return bytes(v)
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, default=str)
    return str(v)


def _target_type(t):
    if isinstance(t, T.Boolean):
        return T.Integer()
    if isinstance(t, T.Integer):
        return T.Integer()
    if isinstance(t, T.Float):
        return T.Float()
    if isinstance(t, T.Numeric):
        return T.String()  # keep exact decimal text, no float rounding
    if isinstance(t, (T.DateTime, T.Date, T.Time)):
        return T.String()  # ISO-8601 text; also tolerates odd legacy values
    if isinstance(t, T._Binary):
        return T.LargeBinary()
    return T.Text()


def _build_target(src: Table, md: MetaData) -> Table:
    pk = {c.name for c in src.primary_key.columns}
    cols = [Column(c.name, _target_type(c.type), primary_key=c.name in pk, autoincrement=False) for c in src.columns]
    return Table(src.name, md, *cols)


@dataclass
class TableResult:
    table: str
    source_rows: int
    local_rows: int
    skipped: bool = False

    @property
    def ok(self) -> bool:
        return self.source_rows == self.local_rows


def dump_tables(
    source: Engine,
    dest: Engine,
    cfg: DumpConfig,
    state: State,
    only: list[str] | None = None,
    on_table=None,
) -> list[TableResult]:
    """Copy tables from `source` into the local SQLite `dest`, table by table.

    Tables already finished (per state file) are skipped, so a dropped connection
    resumes at table granularity. A table is only marked finished when its row
    count in the copy matches the source.
    """
    names = sorted(inspect(source).get_table_names())
    wanted = only or cfg.include_tables
    if wanted:
        names = [n for n in names if n in wanted]
    names = [n for n in names if n not in cfg.exclude_tables]

    results: list[TableResult] = []
    for name in names:
        if state.dump_done(name):
            n = state.dump_rows(name)
            results.append(TableResult(name, n, n, skipped=True))
            continue

        src = Table(name, MetaData(), autoload_with=source)
        md = MetaData()
        dst = _build_target(src, md)
        with dest.begin() as c:
            c.exec_driver_sql(f"DROP TABLE IF EXISTS {q(name)}")
        md.create_all(dest)

        with source.connect() as conn:
            result = conn.execution_options(stream_results=True).execute(select(src))
            for part in result.partitions(cfg.batch_size):
                batch = [{col.name: normalise(v) for col, v in zip(src.columns, row)} for row in part]
                with dest.begin() as dc:
                    dc.execute(dst.insert(), batch)
            n_src = conn.execute(select(func.count()).select_from(src)).scalar_one()

        with dest.connect() as dc:
            n_dst = dc.execute(select(func.count()).select_from(dst)).scalar_one()

        res = TableResult(name, n_src, n_dst)
        if res.ok:
            state.mark_dump_done(name, n_dst)
        results.append(res)
        if on_table:
            on_table(res)
    return results
