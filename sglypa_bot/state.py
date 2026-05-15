from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .brain import atomic_write_json


class BotState:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"enabled": True, "last_channel_message_id": None, "owner_modes": {}}
        with self.path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
        payload.setdefault("enabled", True)
        payload.setdefault("last_channel_message_id", None)
        payload.setdefault("owner_modes", {})
        return payload

    def save(self) -> None:
        atomic_write_json(self.path, self.data)

    @property
    def enabled(self) -> bool:
        return bool(self.data.get("enabled", True))

    def set_enabled(self, value: bool) -> None:
        self.data["enabled"] = bool(value)

    @property
    def last_channel_message_id(self) -> int | None:
        value = self.data.get("last_channel_message_id")
        return int(value) if value else None

    def set_last_channel_message_id(self, message_id: int | None) -> None:
        self.data["last_channel_message_id"] = message_id

    def set_owner_mode(self, owner_id: int, mode: str | None) -> None:
        modes = self.data.setdefault("owner_modes", {})
        key = str(owner_id)
        if mode is None:
            modes.pop(key, None)
        else:
            modes[key] = mode

    def get_owner_mode(self, owner_id: int) -> str | None:
        return self.data.setdefault("owner_modes", {}).get(str(owner_id))
