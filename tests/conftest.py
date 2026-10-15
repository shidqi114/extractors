from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import Column, DateTime, Integer, LargeBinary, MetaData, Numeric, String, Table, Text, create_engine

from extractor.config import DumpConfig
from extractor.dump import dump_tables, open_dest
from extractor.state import State

JPEG = b"\xff\xd8\xff\xe0fake-jpeg-bytes"


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


@pytest.fixture
def legacy_engine(tmp_path):
    """A stand-in for the old VPS database (mixed types + image references)."""
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.sqlite'}")
    md = MetaData()
    rumah = Table(
        "rumah", md,
        Column("id_rumah", Integer, primary_key=True),
        Column("judul", String(100)),
        Column("harga", Numeric(12, 2)),
        Column("foto", String(100)),
        Column("deskripsi", Text),
        Column("tgl", DateTime),
        Column("data", LargeBinary),
    )
    foto = Table(
        "rumah_foto", md,
        Column("id", Integer, primary_key=True),
        Column("id_rumah", Integer),
        Column("foto", String(100)),
        Column("extra", Text),
    )
    md.create_all(engine)
    with engine.begin() as c:
        c.execute(rumah.insert(), [
            dict(id_rumah=1, judul="Rumah A", harga=Decimal("1250.50"), foto="20220324111256_83rxz.jpg",
                 deskripsi="Rumah bagus", tgl=datetime(2022, 3, 24, 11, 12, 56), data=b"\x00\x01"),
            dict(id_rumah=2, judul="Rumah B", harga=Decimal("99.00"), foto="missing.jpg",
                 deskripsi="dekat taman", tgl=None, data=None),
            dict(id_rumah=3, judul="Rumah C", harga=None, foto="broken.jpg", deskripsi=None, tgl=None, data=None),
        ])
        c.execute(foto.insert(), [
            dict(id=1, id_rumah=1, foto="a1.jpg", extra='{"gallery": ["g/1.jpg", "g/2.jpg"]}'),
            dict(id=2, id_rumah=1, foto="a2.jpeg", extra=None),
        ])
    return engine


@pytest.fixture
def dumped(legacy_engine, tmp_path):
    """The local SQLite copy produced by dump_tables."""
    out = tmp_path / "out"
    dest = open_dest(out)
    dump_tables(legacy_engine, dest, DumpConfig(), State(out / "state.json"))
    return dest, out
