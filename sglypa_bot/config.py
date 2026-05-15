from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _env_str(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return default if value is None else value.strip()


def _env_int(name: str, default: int) -> int:
    raw = _env_str(name, "")
    if not raw:
        return default
    return int(raw)


def _env_float(name: str, default: float) -> float:
    raw = _env_str(name, "")
    if not raw:
        return default
    value = float(raw)
    return max(0.0, min(1.0, value))


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env_str(name, "")
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "y", "on", "да"}


def _path_from_env(name: str, default: str) -> Path:
    raw = _env_str(name, default)
    path = Path(raw)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


@dataclass(slots=True)
class Config:
    bot_token: str
    channel_id: int = -1003009758716
    owner_id: int = 7877092881
    data_dir: Path = PROJECT_ROOT / "data"
    memes_dir: Path = PROJECT_ROOT / "memes"
    brain_path: Path = PROJECT_ROOT / "data" / "brain.json"
    state_path: Path = PROJECT_ROOT / "data" / "state.json"
    generated_dir: Path = PROJECT_ROOT / "data" / "generated"
    font_path: str | None = None
    drop_pending_updates: bool = False
    on_message_action_chance: float = 0.04
    reaction_chance: float = 0.12
    idle_min_seconds: int = 240
    idle_max_seconds: int = 900
    idle_action_chance: float = 0.28
    poll_media_chance: float = 0.35
    max_recent_messages: int = 1000


def load_config() -> Config:
    load_dotenv(PROJECT_ROOT / ".env")

    token = _env_str("BOT_TOKEN")
    if not token or token == "123456:PASTE_REAL_TOKEN_HERE":
        raise RuntimeError("BOT_TOKEN не задан. Скопируй .env.example в .env и вставь токен из @BotFather.")

    data_dir = _path_from_env("DATA_DIR", "data")
    memes_dir = _path_from_env("MEMES_DIR", "memes")
    generated_dir = data_dir / "generated"

    idle_min = max(30, _env_int("IDLE_MIN_SECONDS", 240))
    idle_max = max(idle_min, _env_int("IDLE_MAX_SECONDS", 900))

    raw_font_path = _env_str("FONT_PATH", "")
    font_path = None
    if raw_font_path:
        path = Path(raw_font_path)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        font_path = str(path)

    return Config(
        bot_token=token,
        channel_id=_env_int("CHANNEL_ID", -1003009758716),
        owner_id=_env_int("OWNER_ID", 7877092881),
        data_dir=data_dir,
        memes_dir=memes_dir,
        brain_path=data_dir / "brain.json",
        state_path=data_dir / "state.json",
        generated_dir=generated_dir,
        font_path=font_path,
        drop_pending_updates=_env_bool("DROP_PENDING_UPDATES", False),
        on_message_action_chance=_env_float("ON_MESSAGE_ACTION_CHANCE", 0.04),
        reaction_chance=_env_float("REACTION_CHANCE", 0.12),
        idle_min_seconds=idle_min,
        idle_max_seconds=idle_max,
        idle_action_chance=_env_float("IDLE_ACTION_CHANCE", 0.28),
        poll_media_chance=_env_float("POLL_MEDIA_CHANCE", 0.35),
        max_recent_messages=max(50, _env_int("MAX_RECENT_MESSAGES", 1000)),
    )
