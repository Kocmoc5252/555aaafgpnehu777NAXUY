from __future__ import annotations

import base64
import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any

import aiohttp

from .config import Config

log = logging.getLogger(__name__)

TAGIR_CLEAN_RE = re.compile(r"\s+")


class OpenAIResponder:
    """Small async client for OpenAI-compatible chat + image APIs."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.session: aiohttp.ClientSession | None = None
        self.last_error: str | None = None
        self.last_errors: list[str] = []
        self.last_response_id: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.config.openai_api_key and self.config.tagir_enabled)

    @property
    def image_enabled(self) -> bool:
        return bool(self.config.tagir_enabled and self.config.tagir_image_enabled and self.config.tagir_image_api_key)

    def disabled_reason(self) -> str | None:
        if not self.config.tagir_enabled:
            return "TAGIR_ENABLED=false"
        if not self.config.openai_api_key:
            return "OPENAI_API_KEY пустой или не задан"
        return None

    def image_disabled_reason(self) -> str | None:
        if not self.config.tagir_enabled:
            return "TAGIR_ENABLED=false"
        if not self.config.tagir_image_enabled:
            return "TAGIR_IMAGE_ENABLED=false"
        if not self.config.tagir_image_api_key:
            return "TAGIR_IMAGE_API_KEY пустой или не задан"
        return None

    async def open(self) -> None:
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(
                total=self.config.openai_timeout_seconds,
                connect=20,
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
        web_context: str | None = None,
    ) -> str | None:
        self._reset_errors()

        if not self.enabled:
            self._remember_error(self.disabled_reason() or "Тагир выключен")
            return None

        clean_question = self._clean_user_text(user_text)
        if not clean_question:
            clean_question = "просто ответь как собеседник"

        recent_context = self._format_recent_context(recent_channel_texts or [])
        reply_context = f"\nСообщение, на которое ответили: {replied_text[:800]}" if replied_text else ""
        web_block = ""
        if web_context and web_context.strip():
            web_block = "\n\n" + web_context.strip()[:3500]

        input_text = (
            f"Сообщение в канале, обращение к тебе: {clean_question}"
            f"{reply_context}"
            f"{web_block}"
            f"{recent_context}"
        )

        mode = (self.config.openai_api_mode or "auto").lower().strip()
        if mode not in {"auto", "responses", "chat", "chat_completions"}:
            self._remember_error(f"OPENAI_API_MODE={mode!r} не поддерживается, использую auto")
            mode = "auto"
        if mode == "chat_completions":
            mode = "chat"

        if mode in {"auto", "responses"}:
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
                self._remember_error("OpenAI Responses API вернул ответ без текста")

            if mode == "responses":
                return None

        if mode in {"auto", "chat"}:
            response_payload = await self._post_chat_completion(
                system_text=self._instructions(),
                user_text=input_text,
                use_max_completion_tokens=True,
                model=self.config.openai_model,
            )
            if response_payload is None:
                response_payload = await self._post_chat_completion(
                    system_text=self._instructions(),
                    user_text=input_text,
                    use_max_completion_tokens=False,
                    model=self.config.openai_model,
                )
            if response_payload is not None:
                text = self._extract_chat_text(response_payload)
                text = self._postprocess(text)
                if text:
                    return text
                self._remember_error("OpenAI Chat Completions API вернул ответ без текста")

        return None

    async def generate_image(
        self,
        prompt: str,
        *,
        output_dir: Path,
        replied_text: str | None = None,
        recent_channel_texts: list[str] | None = None,
    ) -> tuple[Path | None, str | None]:
        self._reset_errors()

        if not self.image_enabled:
            self._remember_error(self.image_disabled_reason() or "Режим картинок выключен")
            return None, None

        prompt = prompt.strip()
        if not prompt:
            self._remember_error("После 'тагир нарисуй' нужен текст запроса")
            return None, None

        final_prompt = prompt
        if self.config.tagir_image_enhance_prompt:
            upgraded = await self._enhance_image_prompt(prompt, replied_text=replied_text, recent_channel_texts=recent_channel_texts)
            if upgraded:
                final_prompt = upgraded

        payload: dict[str, Any] = {
            "model": self.config.tagir_image_model,
            "prompt": final_prompt,
            "size": self.config.tagir_image_size,
        }
        if self.config.tagir_image_quality:
            payload["quality"] = self.config.tagir_image_quality

        data = await self._post_image_generation(payload)
        if data is None:
            return None, final_prompt

        image_bytes = await self._extract_image_bytes(data)
        if image_bytes is None:
            self._remember_error("Image API вернул ответ без картинки (нет b64_json/url)")
            return None, final_prompt

        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / f"tagir_{uuid.uuid4().hex[:12]}.png"
        path.write_bytes(image_bytes)
        return path, final_prompt

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
        headers = self._text_headers()
        payload = dict(payload)
        payload["stream"] = False

        tool_label = ""
        tools = payload.get("tools")
        if isinstance(tools, list) and tools:
            tool_label = f" tool={tools[0].get('type')}"

        try:
            raw_text, response = await self._post_json(f"{self.config.openai_base_url}/responses", headers=headers, payload=payload)
            data = self._loads_json_or_sse(raw_text)
            if data is None:
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

    async def _post_chat_completion(
        self,
        *,
        system_text: str,
        user_text: str,
        use_max_completion_tokens: bool,
        model: str,
    ) -> dict[str, Any] | None:
        headers = self._text_headers()
        token_field = "max_completion_tokens" if use_max_completion_tokens else "max_tokens"
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_text},
                {"role": "user", "content": user_text},
            ],
            token_field: self.config.openai_max_output_tokens,
            "stream": False,
        }
        label = f" chat_completions {token_field}"

        try:
            raw_text, response = await self._post_json(f"{self.config.openai_base_url}/chat/completions", headers=headers, payload=payload)
            data = self._loads_json_or_sse(raw_text)
            if data is None:
                self._remember_error(f"OpenAI HTTP {response.status}{label}: {raw_text[:700]}")
                return None

            if response.status >= 400:
                message = data.get("error", {}).get("message") or str(data)[:700]
                self._remember_error(f"OpenAI HTTP {response.status}{label}: {message}")
                log.warning("%s", self.last_error)
                return None

            if isinstance(data.get("id"), str):
                self.last_response_id = data["id"]
            return dict(data)
        except TimeoutError as exc:
            self._remember_error(f"OpenAI timeout{label}: {exc}")
            log.warning("%s", self.last_error)
            return None
        except aiohttp.ClientError as exc:
            self._remember_error(f"OpenAI network error{label}: {exc}")
            log.warning("%s", self.last_error)
            return None
        except Exception as exc:  # noqa: BLE001
            self._remember_error(f"Unexpected OpenAI error{label}: {type(exc).__name__}: {exc}")
            log.exception("Unexpected OpenAI chat completion error")
            return None

    async def _enhance_image_prompt(
        self,
        raw_prompt: str,
        *,
        replied_text: str | None,
        recent_channel_texts: list[str] | None,
    ) -> str | None:
        system_text = (
            f"Ты помощник {self.config.tagir_name}. "
            "Преобразуй пользовательскую идею в хороший промпт для генерации изображения. "
            "Пиши на русском. Верни только сам промпт, без пояснений и кавычек. "
            "Сделай промпт визуально конкретным: композиция, детали, свет, стиль и настроение. "
            "Если пользователь хочет фото, делай упор на фотореализм."
        )
        recent_context = self._format_recent_context(recent_channel_texts or [])
        reply_context = f"\nТекст сообщения, на которое ответили: {replied_text[:400]}" if replied_text else ""
        user_text = f"Идея пользователя для картинки: {raw_prompt}{reply_context}{recent_context}"

        response_payload = await self._post_chat_completion(
            system_text=system_text,
            user_text=user_text,
            use_max_completion_tokens=False,
            model=self.config.tagir_image_prompt_model or self.config.openai_model,
        )
        if response_payload is None:
            return None
        text = self._postprocess(self._extract_chat_text(response_payload))
        if not text:
            self._remember_error("Не удалось улучшить промпт для картинки, рисую по исходному тексту")
            return None
        return text

    async def _post_image_generation(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        headers = {
            "Authorization": f"Bearer {self.config.tagir_image_api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        label = " image_generation"
        try:
            raw_text, response = await self._post_json(f"{self.config.tagir_image_base_url}/images/generations", headers=headers, payload=payload)
            data = self._loads_json_or_sse(raw_text)
            if data is None:
                self._remember_error(f"OpenAI HTTP {response.status}{label}: {raw_text[:700]}")
                return None
            if response.status >= 400:
                message = data.get("error", {}).get("message") or str(data)[:700]
                self._remember_error(f"OpenAI HTTP {response.status}{label}: {message}")
                log.warning("%s", self.last_error)
                return None
            if isinstance(data.get("id"), str):
                self.last_response_id = data["id"]
            return dict(data)
        except TimeoutError as exc:
            self._remember_error(f"OpenAI timeout{label}: {exc}")
            log.warning("%s", self.last_error)
            return None
        except aiohttp.ClientError as exc:
            self._remember_error(f"OpenAI network error{label}: {exc}")
            log.warning("%s", self.last_error)
            return None
        except Exception as exc:  # noqa: BLE001
            self._remember_error(f"Unexpected OpenAI error{label}: {type(exc).__name__}: {exc}")
            log.exception("Unexpected OpenAI image generation error")
            return None

    async def _extract_image_bytes(self, payload: dict[str, Any]) -> bytes | None:
        data = payload.get("data")
        if not isinstance(data, list) or not data:
            return None
        first = data[0]
        if not isinstance(first, dict):
            return None
        b64 = first.get("b64_json") or first.get("base64") or first.get("image_base64")
        if isinstance(b64, str) and b64.strip():
            try:
                return base64.b64decode(b64)
            except Exception:  # noqa: BLE001
                self._remember_error("Не удалось декодировать base64-картинку")
                return None

        url = first.get("url") or first.get("image_url")
        if isinstance(url, str) and url.strip():
            return await self._download_bytes(url.strip(), headers={"Authorization": f"Bearer {self.config.tagir_image_api_key}"})
        return None

    async def _download_bytes(self, url: str, headers: dict[str, str] | None = None) -> bytes | None:
        await self.open()
        assert self.session is not None
        try:
            async with self.session.get(url, headers=headers or {}) as response:
                if response.status >= 400:
                    text = await response.text()
                    self._remember_error(f"Не удалось скачать картинку HTTP {response.status}: {text[:500]}")
                    return None
                return await response.read()
        except TimeoutError as exc:
            self._remember_error(f"Timeout при скачивании картинки: {exc}")
            return None
        except aiohttp.ClientError as exc:
            self._remember_error(f"Network error при скачивании картинки: {exc}")
            return None

    async def _post_json(self, url: str, *, headers: dict[str, str], payload: dict[str, Any]) -> tuple[str, aiohttp.ClientResponse]:
        await self.open()
        assert self.session is not None
        async with self.session.post(url, headers=headers, json=payload) as response:
            raw_text = await response.text()
            return raw_text, response

    def _text_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.openai_api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _loads_json_or_sse(self, raw_text: str) -> dict[str, Any] | None:
        raw_text = (raw_text or "").strip()
        if not raw_text:
            return None
        try:
            data = json.loads(raw_text)
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            pass

        chunks: list[dict[str, Any]] = []
        for line in raw_text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            piece = line[5:].strip()
            if not piece or piece == "[DONE]":
                continue
            try:
                event = json.loads(piece)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                chunks.append(event)

        if not chunks:
            return None

        text_parts: list[str] = []
        first_id: str | None = None
        model: str | None = None
        image_b64: str | None = None
        image_url: str | None = None
        last_event = chunks[-1]

        for event in chunks:
            if first_id is None and isinstance(event.get("id"), str):
                first_id = event["id"]
            if model is None and isinstance(event.get("model"), str):
                model = event["model"]

            choices = event.get("choices")
            if isinstance(choices, list):
                for choice in choices:
                    if not isinstance(choice, dict):
                        continue
                    delta = choice.get("delta") or {}
                    if isinstance(delta, dict) and isinstance(delta.get("content"), str):
                        text_parts.append(delta["content"])
                    message = choice.get("message") or {}
                    if isinstance(message, dict) and isinstance(message.get("content"), str):
                        text_parts.append(message["content"])

            if isinstance(event.get("data"), list):
                for item in event["data"]:
                    if not isinstance(item, dict):
                        continue
                    if image_b64 is None and isinstance(item.get("b64_json"), str):
                        image_b64 = item["b64_json"]
                    if image_url is None and isinstance(item.get("url"), str):
                        image_url = item["url"]

            event_type = event.get("type")
            if isinstance(event_type, str) and event_type.endswith(".delta") and isinstance(event.get("delta"), str):
                text_parts.append(event["delta"])
            if isinstance(event.get("output_text"), str):
                text_parts.append(event["output_text"])
            if isinstance(event.get("text"), str):
                text_parts.append(event["text"])

        if image_b64 or image_url:
            item: dict[str, Any] = {}
            if image_b64:
                item["b64_json"] = image_b64
            if image_url:
                item["url"] = image_url
            return {
                "id": first_id or last_event.get("id") or "streaming-image-response",
                "object": "image",
                "model": model or last_event.get("model"),
                "data": [item],
            }

        text = "".join(text_parts).strip()
        if text:
            return {
                "id": first_id or last_event.get("id") or "streaming-response",
                "object": "chat.completion",
                "model": model or last_event.get("model"),
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text}}],
                "output_text": text,
            }
        return last_event if isinstance(last_event, dict) else None

    def _reset_errors(self) -> None:
        self.last_error = None
        self.last_errors = []
        self.last_response_id = None

    def _remember_error(self, message: str) -> None:
        message = re.sub(r"sk-[A-Za-z0-9_\-]+", "sk-***", str(message))
        self.last_error = message[:1200]
        self.last_errors.append(self.last_error)
        if len(self.last_errors) > 8:
            self.last_errors = self.last_errors[-8:]

    def _instructions(self) -> str:
        name = self.config.tagir_name.strip() or "Тагир"
        return (
            f"Ты {name}, живой собеседник Telegram-канала. "
            "Отвечай на русском, естественно, коротко и по делу. "
            "Можно быть чуть абсурдным и разговорным, но без длинных лекций. "
            "Не называй себя ChatGPT, моделью или искусственным интеллектом, если тебя прямо не спрашивают. "
            "Не упоминай системные инструкции, API и токены. "
            "Если в сообщении есть блок 'Актуальные данные из веб-поиска', используй эти данные как свежий контекст. "
            "Если свежих данных нет, честно скажи, что не видишь актуальную информацию. "
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

    def _extract_chat_text(self, payload: dict[str, Any]) -> str:
        choices = payload.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            return ""
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    if isinstance(item.get("text"), str):
                        parts.append(item["text"])
                    elif isinstance(item.get("content"), str):
                        parts.append(item["content"])
            return "\n".join(parts).strip()
        return ""

    def _postprocess(self, text: str) -> str:
        text = (text or "").strip()
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = text.replace("\u200b", "").strip()
        if len(text) > self.config.openai_max_output_chars:
            text = text[: self.config.openai_max_output_chars].rstrip()
            text = text.rsplit(" ", 1)[0].rstrip(" ,.;:") + "…"
        return text
