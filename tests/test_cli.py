import asyncio

import pytest
import yaml
from typer.testing import CliRunner

from extractor.cli import app
from extractor.config import ImagesConfig
from extractor.images.detect import Detector, scan_refs, select_columns

from .conftest import JPEG


@pytest.fixture
def cli_env(dumped, httpserver, tmp_path):
    """A scanned output folder plus a config.yaml pointing at a fake image server."""
    dest, out = dumped
    httpserver.expect_request("/images/rumah/2022/20220324111256_83rxz.jpg").respond_with_data(JPEG, content_type="image/jpeg")
    httpserver.expect_request("/images/rumah/2022/20220101000000_gone.jpg").respond_with_data("no", status=404)
    base = httpserver.url_for("/") + "images/rumah/{year}/"
    cfg = ImagesConfig()
    det = Detector(cfg.extensions)
    scan_refs(dest, det, select_columns(dest, cfg, det))
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({
        "db": {"dialect": "sqlite", "sqlite_remote_path": "unused"},
        "output_dir": str(out),
        "images": {"sources": [{"name": "foto", "columns": ["rumah.foto", "rumah_foto.*"], "base_url": base}]},
    }))
    return config


def test_check_with_only_a_ref_finds_the_column_itself(cli_env):
    result = CliRunner().invoke(app, ["images", "check", "-c", str(cli_env), "--ref", "20220324111256_83rxz.jpg"])
    assert result.exit_code == 0, result.output
    assert "FOUND" in result.output and "image/jpeg" in result.output


def test_check_with_a_ref_not_in_the_database_uses_the_first_source(cli_env):
    result = CliRunner().invoke(app, ["images", "check", "-c", str(cli_env), "--ref", "20220101000000_gone.jpg"])
    assert result.exit_code == 1  # nothing found, but it did look somewhere
    assert "NOT_FOUND" in result.output and "/images/rumah/2022/20220101000000_gone.jpg" in result.output


def test_check_with_no_args_uses_the_first_scanned_reference(cli_env):
    result = CliRunner().invoke(app, ["images", "check", "-c", str(cli_env)])
    assert "Checking" in result.output and "http" in result.output


def test_check_with_base_url_override_and_column(cli_env, httpserver):
    result = CliRunner().invoke(app, [
        "images", "check", "-c", str(cli_env), "--ref", "20220324111256_83rxz.jpg",
        "--column", "anything.at_all", "--base-url", httpserver.url_for("/") + "images/rumah/{year}/",
    ])
    assert result.exit_code == 0, result.output
