"""Настройки из переменных окружения (BotHost передаёт BOT_TOKEN, DOMAIN, PORT)."""
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DOMAIN = "gxbtbzht.bothost.tech"


def _env(*names, default=""):
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return default


def webapp_url(raw: str) -> str:
    raw = raw.strip().rstrip("/")
    if not raw:
        raw = DEFAULT_DOMAIN
    if "://" not in raw:
        raw = "https://" + raw
    return raw.replace("http://", "https://", 1)


@dataclass
class Config:
    bot_token: str
    url: str
    port: int
    data_dir: Path
    owner_username: str
    owner_id: int
    api_id: int
    api_hash: str
    session_string: str


def load() -> Config:
    data_dir = Path(_env("DATA_DIR", default="/app/data" if Path("/app").exists() else "data"))
    return Config(
        bot_token=_env("BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "API_TOKEN"),
        url=webapp_url(_env("WEBAPP_URL", "DOMAIN")),
        port=int(_env("PORT", default="3000")),
        data_dir=data_dir,
        owner_username=_env("OWNER_USERNAME", default="ownerparadoxy").lstrip("@").lower(),
        owner_id=int(_env("OWNER_ID", default="0")),
        api_id=int(_env("TELEGRAM_API_ID", default="0")),
        api_hash=_env("TELEGRAM_API_HASH"),
        session_string=_env("SESSION_STRING"),
    )
