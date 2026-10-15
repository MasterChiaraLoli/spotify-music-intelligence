"""Caricamento di .env e dei file di configurazione YAML."""
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _read_yaml(filename: str) -> dict:
    path = ROOT / "config" / filename
    if not path.exists():
        raise FileNotFoundError(f"File di configurazione mancante: {path}")
    with open(path, encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


@dataclass(frozen=True)
class Settings:
    client_id: str
    client_secret: str
    redirect_uri: str
    test_mode: bool
    dry_run: bool
    use_claude: bool
    log_level: str
    timezone: str
    token_path: Path
    app: dict
    preferences: dict
    blacklist: dict


def load_settings() -> Settings:
    client_id = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise RuntimeError(
            "Mancano SPOTIFY_CLIENT_ID e/o SPOTIFY_CLIENT_SECRET nel file .env"
        )
    app = _read_yaml("settings.yaml")
    return Settings(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=os.getenv("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback").strip(),
        test_mode=_env_bool("TEST_MODE"),
        dry_run=_env_bool("DRY_RUN"),
        use_claude=_env_bool("USE_CLAUDE"),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
        timezone=app.get("timezone", "Europe/Rome"),
        token_path=ROOT / ".spotify_token.json",
        app=app,
        preferences=_read_yaml("preferences.yaml"),
        blacklist=_read_yaml("blacklist.yaml"),
    )


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="[%(asctime)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )