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
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_mode: str = "auto"
    openai_model: str = "gpt-5.2"
    openai_web_search: bool = True
    openai_web_search_tool: str = "web_search_preview"
    openai_timeout_seconds: int = 45
    openai_max_output_tokens: int = 260
    openai_max_output_chars: int = 900
    tagir_enabled: bool = True
    tagir_name: str = "тагир"
    tagir_debug_to_owner: bool = True
    tagir_error_to_channel: bool = False
    tagir_image_enabled: bool = True
    tagir_image_model: str = "gpt-5.5"
    tagir_image_api_key: str = ""
    tagir_image_base_url: str = ""
    tagir_image_size: str = "1024x1024"
    tagir_image_quality: str = "auto"
    tagir_image_prompt_model: str = "gpt-5.5"
    tagir_image_enhance_prompt: bool = True
    search_enabled: bool = False
    search_provider: str = "auto"
    search_max_results: int = 5
    search_timeout_seconds: int = 12
    search_always: bool = False
    search_debug_to_owner: bool = True
    search_searxng_urls: list[str] | None = None


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

    openai_api_key = _env_str("OPENAI_API_KEY", "")
    openai_base_url = _env_str("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")

    tagir_image_api_key = _env_str("TAGIR_IMAGE_API_KEY", "") or openai_api_key
    tagir_image_base_url = _env_str("TAGIR_IMAGE_BASE_URL", "").rstrip("/") or openai_base_url
    raw_searxng_urls = _env_str("SEARCH_SEARXNG_URLS", "")
    searxng_urls = [item.strip().rstrip("/") for item in raw_searxng_urls.split(",") if item.strip()] or None

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
        openai_api_key=openai_api_key,
        openai_base_url=openai_base_url,
        openai_api_mode=(_env_str("OPENAI_API_MODE", "auto") or "auto").lower(),
        openai_model=_env_str("OPENAI_MODEL", "gpt-5.2"),
        openai_web_search=_env_bool("OPENAI_WEB_SEARCH", True),
        openai_web_search_tool=_env_str("OPENAI_WEB_SEARCH_TOOL", "web_search_preview") or "web_search_preview",
        openai_timeout_seconds=max(10, _env_int("OPENAI_TIMEOUT_SECONDS", 45)),
        openai_max_output_tokens=max(32, _env_int("OPENAI_MAX_OUTPUT_TOKENS", 260)),
        openai_max_output_chars=max(120, _env_int("OPENAI_MAX_OUTPUT_CHARS", 900)),
        tagir_enabled=_env_bool("TAGIR_ENABLED", True),
        tagir_name=_env_str("TAGIR_NAME", "тагир") or "тагир",
        tagir_debug_to_owner=_env_bool("TAGIR_DEBUG_TO_OWNER", True),
        tagir_error_to_channel=_env_bool("TAGIR_ERROR_TO_CHANNEL", False),
        tagir_image_enabled=_env_bool("TAGIR_IMAGE_ENABLED", True),
        tagir_image_model=_env_str("TAGIR_IMAGE_MODEL", "gpt-5.5") or "gpt-5.5",
        tagir_image_api_key=tagir_image_api_key,
        tagir_image_base_url=tagir_image_base_url,
        tagir_image_size=_env_str("TAGIR_IMAGE_SIZE", "1024x1024") or "1024x1024",
        tagir_image_quality=_env_str("TAGIR_IMAGE_QUALITY", "auto") or "auto",
        tagir_image_prompt_model=_env_str("TAGIR_IMAGE_PROMPT_MODEL", _env_str("TAGIR_IMAGE_MODEL", "gpt-5.5")) or "gpt-5.5",
        tagir_image_enhance_prompt=_env_bool("TAGIR_IMAGE_ENHANCE_PROMPT", True),
        search_enabled=_env_bool("SEARCH_ENABLED", False),
        search_provider=(_env_str("SEARCH_PROVIDER", "auto") or "auto").lower(),
        search_max_results=max(1, min(8, _env_int("SEARCH_MAX_RESULTS", 5))),
        search_timeout_seconds=max(5, _env_int("SEARCH_TIMEOUT_SECONDS", 12)),
        search_always=_env_bool("SEARCH_ALWAYS", False),
        search_debug_to_owner=_env_bool("SEARCH_DEBUG_TO_OWNER", True),
        search_searxng_urls=searxng_urls,
    )
