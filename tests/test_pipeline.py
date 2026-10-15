"""Scan -> download -> link -> export against a local fake image server."""
import asyncio
import csv
import json
import shutil

import pytest
from sqlalchemy import inspect

from extractor.config import ImagesConfig, ImageSource
from extractor.export import export_all
from extractor.images.detect import Detector, scan_refs, select_columns
from extractor.images.download import download_all
from extractor.images.resolve import plan_job
from extractor.link import link

from .conftest import JPEG


class FakeSFTP:
    def stat(self, path):
        import os

        return os.stat(path)  # FileNotFoundError when missing, like paramiko

    def get(self, remote, local):
        shutil.copyfile(remote, local)


@pytest.fixture
def server(httpserver):
    for path in ("20220324111256_83rxz.jpg", "a1.jpg", "a2.jpeg", "g/1.jpg"):
        httpserver.expect_request(f"/foto/{path}").respond_with_data(JPEG, content_type="image/jpeg")
    httpserver.expect_request("/foto/missing.jpg").respond_with_data("nope", status=404)
    httpserver.expect_request("/foto/broken.jpg").respond_with_data("<html>oops</html>", content_type="text/html")
    httpserver.expect_request("/foto/g/2.jpg").respond_with_data("boom", status=500)
    return httpserver


def _cfg(server, sftp_dir=None):
    return ImagesConfig(
        sources=[ImageSource(name="foto", columns=["rumah.foto", "rumah_foto.*"],
                             base_url=server.url_for("/foto/"), sftp_dir=sftp_dir)],
        max_retries=2, retry_wait=0, concurrency=4,
    )


def _scan(dest, cfg):
    det = Detector(cfg.extensions)
    scan_refs(dest, det, select_columns(dest, cfg, det))


def _statuses(dest):
    with dest.connect() as c:
        return dict(c.exec_driver_sql("SELECT local_path, status FROM _images").fetchall())


def test_download_statuses_and_files(dumped, server):
    dest, out = dumped
    cfg = _cfg(server)
    _scan(dest, cfg)

    summary = asyncio.run(download_all(dest, cfg, out))

    assert summary.attempted == 7
    assert summary.counts == {"ok": 4, "not_found": 1, "error": 2}
    st = _statuses(dest)
    assert st["images/foto/20220324111256_83rxz.jpg"] == "ok"
    assert st["images/foto/missing.jpg"] == "not_found"
    assert st["images/foto/broken.jpg"] == "error"  # 200 but html, not an image
    assert st["images/foto/g/2.jpg"] == "error"  # 500 after retries
    assert (out / "images/foto/g/1.jpg").read_bytes() == JPEG
    assert not list((out / "images").rglob("*.part"))


def test_second_run_resumes_and_retry_failed(dumped, server):
    dest, out = dumped
    cfg = _cfg(server)
    _scan(dest, cfg)
    asyncio.run(download_all(dest, cfg, out))

    assert asyncio.run(download_all(dest, cfg, out)).attempted == 0
    assert asyncio.run(download_all(dest, cfg, out, retry_failed=True)).attempted == 3  # 1 not_found + 2 error

    (out / "images/foto/a1.jpg").unlink()  # a deleted file is re-fetched
    assert asyncio.run(download_all(dest, cfg, out)).attempted == 1


def test_sftp_fallback_when_http_has_no_file(dumped, server, tmp_path):
    dest, out = dumped
    remote = tmp_path / "remote"
    remote.mkdir()
    (remote / "missing.jpg").write_bytes(JPEG)
    cfg = _cfg(server, sftp_dir=str(remote))
    _scan(dest, cfg)

    asyncio.run(download_all(dest, cfg, out, sftp=FakeSFTP()))

    st = _statuses(dest)
    assert st["images/foto/missing.jpg"] == "ok"
    assert (out / "images/foto/missing.jpg").read_bytes() == JPEG
    with dest.connect() as c:
        via = c.exec_driver_sql("SELECT via FROM _images WHERE local_path = 'images/foto/missing.jpg'").scalar()
    assert via == "sftp"


def test_link_and_export(dumped, server):
    dest, out = dumped
    cfg = _cfg(server)
    _scan(dest, cfg)
    asyncio.run(download_all(dest, cfg, out))

    added = link(dest)

    assert added == {"rumah": ["foto_local"], "rumah_foto": ["foto_local"]}  # `extra` holds 2 images -> no *_local
    assert "extra_local" not in {c["name"] for c in inspect(dest).get_columns("rumah_foto")}
    with dest.connect() as c:
        local = dict(c.exec_driver_sql("SELECT id_rumah, foto_local FROM rumah").fetchall())
        n_links = c.exec_driver_sql("SELECT COUNT(*) FROM _image_links").scalar()
    assert local == {1: "images/foto/20220324111256_83rxz.jpg", 2: None, 3: None}
    assert n_links == 7

    counts = export_all(dest, out)
    assert counts["rumah"] == 3 and counts["_image_links"] == 7
    with (out / "csv/rumah.csv").open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 3 and rows[0]["foto_local"].endswith("83rxz.jpg") and rows[0]["data"] == "AAE="
    lines = (out / "jsonl/rumah.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["id_rumah"] == 1


def test_resolve_rules():
    cfg = ImagesConfig(sources=[ImageSource(name="foto", columns=["rumah.foto"], base_url="https://h.id/foto/",
                                            sftp_dir="/srv/foto")])
    job = plan_job("a b.jpg", "rumah", "foto", cfg)
    assert [(c.kind, c.target) for c in job.candidates] == [("http", "https://h.id/foto/a%20b.jpg"),
                                                           ("sftp", "/srv/foto/a b.jpg")]
    assert job.local_rel == "foto/a b.jpg"
    assert plan_job("../../etc/passwd.jpg", "rumah", "foto", cfg).candidates == []  # traversal refused
    assert plan_job("x.jpg", "other", "col", cfg).candidates == []  # no source -> unresolved
    assert plan_job("data:image/png;base64,AA", "rumah", "foto", cfg) is None
    absolute = plan_job("https://cdn.x.com/p/a.jpg?w=1", "rumah", "foto", cfg)
    assert absolute.candidates[0].target == "https://cdn.x.com/p/a.jpg?w=1"
    assert absolute.local_rel.startswith("cdn.x.com/p/a_")
