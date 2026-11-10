"""Ayarları config.yaml ve .env dosyalarından okur."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parent.parent
TZ = ZoneInfo("Europe/Istanbul")


def _load_env(path: Path) -> None:
    """python-dotenv varsa onu kullan, yoksa basit okuyucu."""
    if not path.exists():
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(path)
        return
    except ImportError:
        pass
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


@dataclass
class Config:
    raw: dict
    telegram_token: str = ""
    telegram_chat_id: str = ""
    algolab_api_key: str = ""
    algolab_username: str = ""
    algolab_password: str = ""
    db_path: Path = field(default_factory=lambda: ROOT / "data" / "bot.db")

    # Kısa yollar
    @property
    def mode(self) -> str:
        return self.raw.get("mode", "paper")

    @property
    def universe(self) -> list[str]:
        return [s.upper().strip() for s in self.raw.get("universe", [])]

    @property
    def strategy(self) -> dict:
        return self.raw["strategy"]

    @property
    def risk(self) -> dict:
        return self.raw["risk"]

    @property
    def schedule(self) -> dict:
        return self.raw["schedule"]

    @property
    def news(self) -> dict:
        return self.raw.get("news", {})


def load_config(config_path: str | Path | None = None) -> Config:
    config_path = Path(config_path or ROOT / "config.yaml")
    _load_env(ROOT / ".env")
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    cfg = Config(
        raw=raw,
        telegram_token=os.getenv("TELEGRAM_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        algolab_api_key=os.getenv("ALGOLAB_API_KEY", ""),
        algolab_username=os.getenv("ALGOLAB_USERNAME", ""),
        algolab_password=os.getenv("ALGOLAB_PASSWORD", ""),
    )
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    return cfg
