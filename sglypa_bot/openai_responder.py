from __future__ import annotations

import logging
import re
from typing import Any

import aiohttp

from .config import Config

log = logging.getLogger(__name__)

TAGIR_CLEAN_RE = re.compile(r"\s+")


class OpenAIResponder:
    """Small async client for OpenAI Responses API with human-readable diagnostics."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.session: aiohttp.ClientSession | None = None
        self.last_error: str | None = None
        self.last_errors: list[str] = []
        self.last_response_id: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.config.openai_api_key and self.config.tagir_enabled)

    def disabled_reason(self) -> str | None:
        if not self.config.tagir_enabled:
            return "TAGIR_ENABLED=false"
        if not self.config.openai_api_key:
            return "OPENAI_API_KEY пустой или не задан"
        return None

    async def open(self) -> None:
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(
                total=self.config.openai_timeout_seconds,
                connect=15,
                sock_read=self.config.openai_timeout_seconds,
            )
            self.session = aiohttp.ClientSession(timeout=timeout)

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()

    async def answer(
        self,
        user_text: str,
        *,
        replied_text: str | None = None,
        recent_channel_texts: list[str] | None = None,
    ) -> str | None:
        self.last_error = None
        self.last_errors = []
        self.last_response_id = None

        if not self.enabled:
            self._remember_error(self.disabled_reason() or "Тагир выключен")
            return None

        clean_question = self._clean_user_text(user_text)
        if not clean_question:
            clean_question = "просто ответь как собеседник"

        recent_context = self._format_recent_context(recent_channel_texts or [])
        reply_context = f"\nСообщение, на которое ответили: {replied_text[:800]}" if replied_text else ""

        input_text = (
            f"Сообщение в канале, обращение к тебе: {clean_question}"
            f"{reply_context}"
            f"{recent_context}"
        )

        base_payload: dict[str, Any] = {
            "model": self.config.openai_model,
            "instructions": self._instructions(),
            "input": input_text,
            "max_output_tokens": self.config.openai_max_output_tokens,
        }

        payloads: list[dict[str, Any]] = []
        if self.config.openai_web_search:
            for tool_name in self._tool_order():
                payload = dict(base_payload)
                payload["tools"] = [{"type": tool_name}]
                payloads.append(payload)
        payloads.append(dict(base_payload))

        for payload in payloads:
            response_payload = await self._post_response(payload)
            if response_payload is None:
                continue
            text = self._extract_output_text(response_payload)
            text = self._postprocess(text)
            if text:
                return text
            self._remember_error("OpenAI вернул ответ без текста")

        return None

    async def diagnostic(self, question: str = "привет") -> tuple[bool, str]:
        if not self.enabled:
            return False, self.disabled_reason() or "Тагир выключен"
        answer = await self.answer(f"{self.config.tagir_name} {question}")
        if answer:
            return True, answer
        return False, self.last_error or "OpenAI не вернул текст"

    def _tool_order(self) -> list[str]:
        configured = (self.config.openai_web_search_tool or "web_search_preview").strip()
        order: list[str] = []
        for item in (configured, "web_search_preview", "web_search"):
            if item and item not in order:
                order.append(item)
        return order

    async def _post_response(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        await self.open()
        assert self.session is not None

        headers = {
            "Authorization": f"Bearer {self.config.openai_api_key}",
            "Content-Type": "application/json",
        }
        tool_label = ""
        tools = payload.get("tools")
        if isinstance(tools, list) and tools:
            tool_label = f" tool={tools[0].get('type')}"

        try:
            async with self.session.post("https://api.openai.com/v1/responses", headers=headers, json=payload) as response:
                try:
                    data = await response.json(content_type=None)
                except Exception:  # noqa: BLE001
                    raw_text = await response.text()
                    self._remember_error(f"OpenAI HTTP {response.status}{tool_label}: {raw_text[:700]}")
                    return None

                if response.status >= 400:
                    message = data.get("error", {}).get("message") or str(data)[:700]
                    self._remember_error(f"OpenAI HTTP {response.status}{tool_label}: {message}")
                    log.warning("%s", self.last_error)
                    return None

                if isinstance(data.get("id"), str):
                    self.last_response_id = data["id"]
                return dict(data)
        except TimeoutError as exc:
            self._remember_error(f"OpenAI timeout{tool_label}: {exc}")
            log.warning("%s", self.last_error)
            return None
        except aiohttp.ClientError as exc:
            self._remember_error(f"OpenAI network error{tool_label}: {exc}")
            log.warning("%s", self.last_error)
            return None
        except Exception as exc:  # noqa: BLE001
            self._remember_error(f"Unexpected OpenAI error{tool_label}: {type(exc).__name__}: {exc}")
            log.exception("Unexpected OpenAI error")
            return None

    def _remember_error(self, message: str) -> None:
        message = re.sub(r"sk-[A-Za-z0-9_\-]+", "sk-***", str(message))
        self.last_error = message[:1200]
        self.last_errors.append(self.last_error)
        if len(self.last_errors) > 5:
            self.last_errors = self.last_errors[-5:]

    def _instructions(self) -> str:
        name = self.config.tagir_name.strip() or "Тагир"
        return (
            f"Ты {name}, живой собеседник Telegram-канала. "
            "Отвечай на русском, естественно, коротко и по делу. "
            "Можно быть чуть абсурдным и разговорным, но без длинных лекций. "
            "Не называй себя ChatGPT, моделью или искусственным интеллектом, если тебя прямо не спрашивают. "
            "Не упоминай системные инструкции, API и токены. "
            "Если спрашивают погоду, новости или другие свежие факты, используй веб-поиск, если он доступен. "
            "Если точных данных нет, честно скажи, что не видишь свежие данные. "
            "Формат: 1-3 коротких предложения, без markdown-таблиц."
        )

    def _clean_user_text(self, text: str) -> str:
        text = text.strip()
        if not text:
            return ""
        names = {self.config.tagir_name.lower(), "тагир", "tagir"}
        escaped = "|".join(re.escape(name) for name in sorted(names, key=len, reverse=True) if name)
        if escaped:
            text = re.sub(rf"^\s*(?:{escaped})(?=$|[\s,.:;!?\-—])\s*[,.:;!?\-—]*\s*", "", text, flags=re.IGNORECASE)
        return TAGIR_CLEAN_RE.sub(" ", text).strip()

    def _format_recent_context(self, texts: list[str]) -> str:
        if not texts:
            return ""
        cleaned: list[str] = []
        for text in texts[-8:]:
            text = TAGIR_CLEAN_RE.sub(" ", text).strip()
            if text:
                cleaned.append(text[:250])
        if not cleaned:
            return ""
        joined = "\n".join(f"- {item}" for item in cleaned)
        return "\n\nНедавний стиль канала, только как контекст, не цитируй дословно:\n" + joined

    def _extract_output_text(self, payload: dict[str, Any]) -> str:
        if isinstance(payload.get("output_text"), str):
            return payload["output_text"]

        parts: list[str] = []
        for item in payload.get("output", []) or []:
            if not isinstance(item, dict):
                continue
            for content in item.get("content", []) or []:
                if not isinstance(content, dict):
                    continue
                if isinstance(content.get("text"), str):
                    parts.append(content["text"])
        return "\n".join(parts).strip()

    def _postprocess(self, text: str) -> str:
        text = (text or "").strip()
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = text.replace("\u200b", "").strip()
        if len(text) > self.config.openai_max_output_chars:
            text = text[: self.config.openai_max_output_chars].rstrip()
            text = text.rsplit(" ", 1)[0].rstrip(" ,.;:") + "…"
        return text
