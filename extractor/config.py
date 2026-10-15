from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel


class SSHConfig(BaseModel):
    host: str
    port: int = 22
    user: str
    password: str | None = None
    key_path: str | None = None
    strict_host_key: bool = False


class DBConfig(BaseModel):
    dialect: Literal["mysql", "postgresql", "sqlite"] = "mysql"
    host: str = "127.0.0.1"
    port: int | None = None
    name: str = ""
    user: str | None = None
    password: str | None = None
    sqlite_remote_path: str | None = None

    @property
    def default_port(self) -> int:
        return {"mysql": 3306, "postgresql": 5432}.get(self.dialect, 0)


class DumpConfig(BaseModel):
    batch_size: int = 2000
    include_tables: list[str] = []
    exclude_tables: list[str] = []


class ImageSource(BaseModel):
    name: str
    columns: list[str] = ["*"]
    base_url: str | None = None
    sftp_dir: str | None = None


class ImagesConfig(BaseModel):
    sources: list[ImageSource] = []
    include_columns: list[str] = []
    exclude_columns: list[str] = []
    extensions: list[str] = ["jpg", "jpeg", "png", "gif", "webp", "bmp", "svg", "tif", "tiff", "avif"]
    sample_size: int = 500
    min_hit_ratio: float = 0.5
    concurrency: int = 8
    timeout: float = 30
    max_retries: int = 3
    retry_wait: float = 1.0
    user_agent: str = "extractor/0.1 (+legacy-db-migration)"


class OrganizeConfig(BaseModel):
    """How houses, their photos and their agents relate; used to build houses/ and agents/."""

    house_table: str = "rumah"
    house_id: str = "id_rumah"
    house_main_photo: str = "foto"  # column of the house table holding the main photo
    house_agent: str = "agent"  # column of the house table pointing at the agent
    photo_table: str = "rumah_foto"
    photo_id: str = "id_foto"
    photo_house: str = "id_rumah"  # column of the photo table pointing at the house
    agent_table: str = "agent"
    agent_id: str | None = None  # default: the agent table's primary key
    agent_name: str | None = None  # column used for the agents/<name>/ folder; default: auto-detect
    houses_dir: str = "houses"
    agents_dir: str = "agents"


class Config(BaseModel):
    ssh: SSHConfig | None = None
    db: DBConfig
    output_dir: Path = Path("output")
    dump: DumpConfig = DumpConfig()
    images: ImagesConfig = ImagesConfig()
    organize: OrganizeConfig = OrganizeConfig()


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def load_config(path: str | Path) -> Config:
    """Load YAML config; passwords come from the environment / a local .env file."""
    _load_dotenv(Path(".env"))
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    cfg = Config.model_validate(raw)
    if cfg.ssh and not cfg.ssh.password:
        cfg.ssh.password = os.environ.get("EXTRACTOR_SSH_PASSWORD") or None
    if not cfg.db.password:
        cfg.db.password = os.environ.get("EXTRACTOR_DB_PASSWORD") or None
    return cfg
