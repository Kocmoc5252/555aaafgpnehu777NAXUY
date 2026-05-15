from __future__ import annotations

import json
import logging
import mimetypes
from pathlib import Path
from typing import Any

import aiohttp

log = logging.getLogger(__name__)


class TelegramAPIError(RuntimeError):
    def __init__(self, method: str, description: str, status: int | None = None) -> None:
        self.method = method
        self.description = description
        self.status = status
        super().__init__(f"Telegram API error in {method}: {description}")


class TelegramBotAPI:
    def __init__(self, token: str) -> None:
        self.token = token
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "TelegramBotAPI":
        await self.open()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def open(self) -> None:
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=90, connect=20, sock_read=90)
            self.session = aiohttp.ClientSession(timeout=timeout)

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        files: dict[str, str | Path] | None = None,
    ) -> Any:
        await self.open()
        assert self.session is not None

        params = {k: v for k, v in (params or {}).items() if v is not None}
        url = f"{self.base_url}/{method}"

        opened_files: list[Any] = []
        try:
            if files:
                form = aiohttp.FormData()
                for key, value in params.items():
                    if isinstance(value, (dict, list)):
                        form.add_field(key, json.dumps(value, ensure_ascii=False))
                    elif isinstance(value, bool):
                        form.add_field(key, "true" if value else "false")
                    else:
                        form.add_field(key, str(value))

                for field_name, file_path in files.items():
                    path = Path(file_path)
                    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                    fh = path.open("rb")
                    opened_files.append(fh)
                    form.add_field(field_name, fh, filename=path.name, content_type=content_type)

                async with self.session.post(url, data=form) as response:
                    payload = await self._read_payload(response)
            else:
                async with self.session.post(url, json=params) as response:
                    payload = await self._read_payload(response)
        finally:
            for fh in opened_files:
                try:
                    fh.close()
                except Exception:  # noqa: BLE001
                    pass

        if not payload.get("ok"):
            description = payload.get("description", "unknown error")
            raise TelegramAPIError(method, description)
        return payload.get("result")

    async def _read_payload(self, response: aiohttp.ClientResponse) -> dict[str, Any]:
        try:
            return await response.json(content_type=None)
        except Exception as exc:  # noqa: BLE001
            text = await response.text()
            raise TelegramAPIError("unknown", f"HTTP {response.status}: {text[:500]}", response.status) from exc

    async def get_updates(self, *, offset: int | None, timeout: int, allowed_updates: list[str]) -> list[dict[str, Any]]:
        result = await self.call(
            "getUpdates",
            {
                "offset": offset,
                "timeout": timeout,
                "allowed_updates": allowed_updates,
            },
        )
        return list(result or [])

    async def delete_webhook(self, *, drop_pending_updates: bool) -> bool:
        return bool(await self.call("deleteWebhook", {"drop_pending_updates": drop_pending_updates}))

    async def get_me(self) -> dict[str, Any]:
        return dict(await self.call("getMe"))

    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        *,
        reply_to_message_id: int | None = None,
        reply_markup: dict[str, Any] | None = None,
        disable_notification: bool | None = None,
    ) -> dict[str, Any]:
        text = text[:4096]
        reply_parameters = {"message_id": reply_to_message_id} if reply_to_message_id else None
        return dict(
            await self.call(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": text,
                    "reply_parameters": reply_parameters,
                    "reply_markup": reply_markup,
                    "disable_notification": disable_notification,
                },
            )
        )

    async def send_photo(
        self,
        chat_id: int | str,
        photo_path: str | Path,
        *,
        caption: str | None = None,
        reply_to_message_id: int | None = None,
        disable_notification: bool | None = None,
    ) -> dict[str, Any]:
        reply_parameters = {"message_id": reply_to_message_id} if reply_to_message_id else None
        return dict(
            await self.call(
                "sendPhoto",
                {
                    "chat_id": chat_id,
                    "caption": caption[:1024] if caption else None,
                    "reply_parameters": reply_parameters,
                    "disable_notification": disable_notification,
                },
                files={"photo": photo_path},
            )
        )

    async def send_poll(
        self,
        chat_id: int | str,
        question: str,
        options: list[dict[str, Any]],
        *,
        description: str | None = None,
        media_path: str | Path | None = None,
        allows_multiple_answers: bool = False,
        is_anonymous: bool = True,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "question": question[:300],
            "options": options,
            "is_anonymous": is_anonymous,
            "type": "regular",
            "allows_multiple_answers": allows_multiple_answers,
            "shuffle_options": True,
            "description": description[:1024] if description else None,
        }
        files = None
        if media_path:
            params["media"] = {"type": "photo", "media": "attach://poll_media"}
            files = {"poll_media": media_path}

        return dict(await self.call("sendPoll", params, files=files))

    async def set_message_reaction(
        self,
        chat_id: int | str,
        message_id: int,
        emoji: str,
        *,
        is_big: bool = False,
    ) -> bool:
        return bool(
            await self.call(
                "setMessageReaction",
                {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "reaction": [{"type": "emoji", "emoji": emoji}],
                    "is_big": is_big,
                },
            )
        )

    async def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> bool:
        return bool(await self.call("answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text}))

    async def get_chat_administrators(self, chat_id: int | str) -> list[dict[str, Any]]:
        try:
            result = await self.call("getChatAdministrators", {"chat_id": chat_id, "return_bots": False})
        except TelegramAPIError as exc:
            if "return_bots" not in exc.description:
                raise
            log.debug("Bot API without return_bots support, retrying getChatAdministrators without it")
            result = await self.call("getChatAdministrators", {"chat_id": chat_id})
        return list(result or [])
