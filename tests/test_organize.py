import asyncio
import json
import os
import shutil

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine

from extractor.config import DumpConfig, ImagesConfig, ImageSource, OrganizeConfig
from extractor.dump import dump_tables, open_dest
from extractor.images.detect import Detector, scan_refs, select_columns
from extractor.images.download import download_all
from extractor.link import link
from extractor.organize import OrganizeError, organize
from extractor.state import State

from .conftest import JPEG


@pytest.fixture
def prepared(tmp_path, httpserver):
    """Fake legacy DB (agent, rumah, rumah_foto) taken through dump -> scan -> download -> link."""
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.sqlite'}")
    md = MetaData()
    agent = Table("agent", md, Column("id_agent", Integer, primary_key=True), Column("nama", String(50)),
                  Column("foto", String(100)))
    rumah = Table("rumah", md, Column("id_rumah", Integer, primary_key=True), Column("judul", String(100)),
                  Column("foto", String(100)), Column("agent", String(10)))
    foto = Table("rumah_foto", md, Column("id_foto", Integer, primary_key=True), Column("id_rumah", Integer),
                 Column("foto", String(100)))
    md.create_all(engine)
    with engine.begin() as c:
        c.execute(agent.insert(), [dict(id_agent=4, nama="Widodo", foto="ag4.jpg"),
                                   dict(id_agent=5, nama="Sari", foto=None)])
        c.execute(rumah.insert(), [
            dict(id_rumah=1, judul="A", foto="main1.jpg", agent="4"),
            dict(id_rumah=2, judul="B", foto="missing.jpg", agent="4"),
            dict(id_rumah=3, judul="C", foto=None, agent="99"),
            dict(id_rumah=6, judul="D", foto=None, agent=""),
        ])
        c.execute(foto.insert(), [
            dict(id_foto=10, id_rumah=1, foto="p1.jpg"), dict(id_foto=11, id_rumah=1, foto="p2.jpg"),
            dict(id_foto=12, id_rumah=2, foto="p3.jpg"), dict(id_foto=13, id_rumah=77, foto="orphan.jpg"),
        ])

    for name in ("main1.jpg", "p1.jpg", "p2.jpg", "p3.jpg", "orphan.jpg", "ag4.jpg"):
        httpserver.expect_request(f"/foto/{name}").respond_with_data(JPEG, content_type="image/jpeg")
    httpserver.expect_request("/foto/missing.jpg").respond_with_data("nope", status=404)

    out = tmp_path / "out"
    dest = open_dest(out)
    dump_tables(engine, dest, DumpConfig(), State(out / "state.json"))
    cfg = ImagesConfig(
        sources=[ImageSource(name="foto", columns=["rumah.foto", "rumah_foto.foto", "agent.foto"],
                             base_url=httpserver.url_for("/foto/"))],
        max_retries=1, retry_wait=0,
    )
    det = Detector(cfg.extensions)
    scan_refs(dest, det, select_columns(dest, cfg, det))
    asyncio.run(download_all(dest, cfg, out))
    link(dest)
    return dest, out


def _json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_houses_photos_and_main_photo(prepared):
    dest, out = prepared
    summary = organize(dest, out, OrganizeConfig())

    h1 = out / "houses" / "1"
    assert sorted(p.name for p in (h1 / "photos").iterdir()) == ["10_p1.jpg", "11_p2.jpg", "main_main1.jpg"]
    assert (h1 / "photos" / "10_p1.jpg").read_bytes() == JPEG
    data = _json(h1 / "house.json")
    assert data["id_rumah"] == 1 and data["house"]["judul"] == "A"
    assert data["main_photo"] == {"file": "photos/main_main1.jpg", "original": "main1.jpg", "status": "ok"}
    assert [(p["id_foto"], p["images"][0]["file"]) for p in data["photos"]] == [
        (10, "photos/10_p1.jpg"), (11, "photos/11_p2.jpg")]
    assert data["photos"][0]["row"]["id_rumah"] == 1
    assert data["agent"] == {"id": "4", "name": "Widodo", "file": "../../agents/Widodo/agent.json"}

    # house 2: main photo 404 is recorded, its other photo is still placed
    d2 = _json(out / "houses" / "2" / "house.json")
    assert d2["main_photo"]["file"] is None and d2["main_photo"]["status"] == "not_found"
    assert (out / "houses" / "2" / "photos" / "12_p3.jpg").is_file()

    assert summary.houses == 4 and summary.orphan_photos == 1
    assert summary.unknown_agent == 1 and summary.houses_without_agent == 1
    assert summary.photos_placed == 4  # main1, p1, p2, p3 (ag4 is an agent photo, orphan has no house)
    assert summary.photos_unavailable == 1  # missing.jpg
    assert not list(out.glob("houses/*/photos/*orphan*"))


def test_agents_folder_links_back_to_houses(prepared):
    dest, out = prepared
    organize(dest, out, OrganizeConfig())

    assert sorted(p.name for p in (out / "agents").iterdir()) == ["Sari", "Widodo"]  # named, not numbered
    a4 = _json(out / "agents" / "Widodo" / "agent.json")
    assert a4["id_agent"] == 4 and a4["agent"]["nama"] == "Widodo" and a4["houses"] == ["1", "2"]
    assert a4["files"] == ["ag4.jpg"] and (out / "agents" / "Widodo" / "ag4.jpg").is_file()
    a5 = _json(out / "agents" / "Sari" / "agent.json")
    assert a5["houses"] == [] and a5["files"] == []

    assert _json(out / "houses" / "3" / "house.json")["agent"] == {"id": "99", "file": None}
    assert _json(out / "houses" / "6" / "house.json")["agent"] is None


def test_rerun_and_hardlink(prepared):
    dest, out = prepared
    organize(dest, out, OrganizeConfig())
    organize(dest, out, OrganizeConfig())  # idempotent
    shutil.rmtree(out / "houses")

    organize(dest, out, OrganizeConfig(), hardlink=True)

    assert os.stat(out / "houses" / "1" / "photos" / "10_p1.jpg").st_nlink >= 2


def test_clear_errors(prepared):
    dest, out = prepared
    with pytest.raises(OrganizeError, match="no column"):
        organize(dest, out, OrganizeConfig(photo_id="nope"))
    with pytest.raises(OrganizeError, match="not in the local copy"):
        organize(dest, out, OrganizeConfig(agent_table="brokers"))
    with dest.begin() as c:
        c.exec_driver_sql("DROP TABLE _image_links")
    with pytest.raises(OrganizeError, match="extractor link"):
        organize(dest, out, OrganizeConfig())


def _rename_agents(dest, names):
    with dest.begin() as c:
        for agent_id, name in names.items():
            c.exec_driver_sql("UPDATE agent SET nama = ? WHERE id_agent = ?", (name, agent_id))


def test_agent_folder_names_are_safe_and_unique(prepared):
    dest, out = prepared
    _rename_agents(dest, {4: "Pak/Budi: Santoso", 5: "pak/budi: santoso"})  # same name once cleaned

    organize(dest, out, OrganizeConfig())

    names = sorted(p.name for p in (out / "agents").iterdir())
    assert names == ["Pak_Budi_ Santoso_4", "pak_budi_ santoso_5"]  # id keeps duplicates apart; no path separators
    assert not (out.parent / "Budi").exists()


def test_agent_without_name_falls_back_to_id(prepared):
    dest, out = prepared
    _rename_agents(dest, {5: ""})

    organize(dest, out, OrganizeConfig())

    assert (out / "agents" / "agent_5" / "agent.json").is_file()


def test_agent_name_column_is_configurable_and_checked(prepared):
    dest, out = prepared
    organize(dest, out, OrganizeConfig(agent_name="foto"))  # any column can name the folders
    assert (out / "agents" / "ag4.jpg").is_dir()
    with pytest.raises(OrganizeError, match="no column"):
        organize(dest, out, OrganizeConfig(agent_name="nope"))
    with dest.begin() as c:
        c.exec_driver_sql("ALTER TABLE agent RENAME COLUMN nama TO julukan")
    with pytest.raises(OrganizeError, match="organize.agent_name"):
        organize(dest, out, OrganizeConfig())
