from sqlalchemy import text

from extractor.config import DumpConfig
from extractor.dump import dump_tables, normalise, open_dest
from extractor.state import State


def test_dump_copies_all_rows_and_normalises(legacy_engine, tmp_path):
    out = tmp_path / "out"
    dest = open_dest(out)
    results = dump_tables(legacy_engine, dest, DumpConfig(), State(out / "state.json"))

    assert {r.table: (r.source_rows, r.local_rows) for r in results} == {"rumah": (3, 3), "rumah_foto": (2, 2)}
    with dest.connect() as c:
        row = c.execute(text("SELECT harga, tgl, data FROM rumah WHERE id_rumah = 1")).one()
    assert row[0] == "1250.50"  # Decimal kept exact, as text
    assert row[1].startswith("2022-03-24T11:12:56")
    assert row[2] == b"\x00\x01"


def test_resume_skips_finished_tables(legacy_engine, tmp_path):
    out = tmp_path / "out"
    dest = open_dest(out)
    dump_tables(legacy_engine, dest, DumpConfig(), State(out / "state.json"))
    with legacy_engine.begin() as c:
        c.execute(text("DELETE FROM rumah"))

    second = dump_tables(legacy_engine, dest, DumpConfig(), State(out / "state.json"))

    assert all(r.skipped for r in second)
    with dest.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM rumah")).scalar_one() == 3  # untouched


def test_include_and_exclude_tables(legacy_engine, tmp_path):
    out = tmp_path / "out"
    results = dump_tables(
        legacy_engine, open_dest(out), DumpConfig(include_tables=["rumah", "rumah_foto"], exclude_tables=["rumah"]),
        State(out / "state.json"),
    )
    assert [r.table for r in results] == ["rumah_foto"]


def test_normalise_zero_dates():
    assert normalise("0000-00-00") is None
    assert normalise("0000-00-00 00:00:00") is None
    assert normalise("2022-01-01") == "2022-01-01"
