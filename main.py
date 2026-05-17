from __future__ import annotations

import asyncio
import html
import logging
import random
import re
import sys
import time
from pathlib import Path
from typing import Any

from sglypa_bot.brain import Brain, tokenize
from sglypa_bot.config import PROJECT_ROOT, Config, load_config
from sglypa_bot.memes import MemeGenerator
from sglypa_bot.openai_responder import OpenAIResponder
from sglypa_bot.search import FreeSearchClient, format_search_context, make_search_query, wants_web_search
from sglypa_bot.live_data import fetch_live_context
from sglypa_bot.state import BotState
from sglypa_bot.telegram_api import TelegramAPIError, TelegramBotAPI

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
TRIGGER_RE = re.compile(r"(?<![а-яёa-z])бля(?![а-яёa-z])", re.IGNORECASE)
CODE_FENCE_RE = re.compile(r"```([a-zA-Z0-9_+.#-]*)\n([\s\S]*?)```", re.MULTILINE)
REACTIONS = ["👍", "❤", "🤡"]

log = logging.getLogger("sglypa-channel-bot")


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


def format_tagir_answer_for_telegram(text: str) -> tuple[str, str | None]:
    """Convert Markdown-style fenced code blocks to Telegram HTML.

    Telegram will show raw triple backticks unless sendMessage gets a parse_mode.
    HTML is safer here than MarkdownV2 because we can escape normal text and code
    without fighting every special Markdown character.
    """
    if "```" not in text:
        return text, None

    parts: list[str] = []
    last = 0
    found = False
    for match in CODE_FENCE_RE.finditer(text):
        found = True
        parts.append(html.escape(text[last:match.start()]))
        language = re.sub(r"[^a-zA-Z0-9_-]", "", match.group(1).strip())[:32]
        code = match.group(2).strip("\n")
        escaped_code = html.escape(code)
        if language:
            parts.append(f'<pre><code class="language-{language}">{escaped_code}</code></pre>')
        else:
            parts.append(f"<pre>{escaped_code}</pre>")
        last = match.end()

    if not found:
        return text, None

    parts.append(html.escape(text[last:]))
    return "".join(parts), "HTML"


def message_text(message: dict[str, Any]) -> str:
    text = message.get("text") or message.get("caption") or ""
    if text:
        return str(text)
    poll = message.get("poll")
    if poll:
        parts = [poll.get("question", "")]
        for option in poll.get("options", []):
            parts.append(option.get("text", ""))
        return " ".join(part for part in parts if part)
    return ""


def alias_from_user(user: dict[str, Any] | None) -> str | None:
    if not user or user.get("is_bot"):
        return None
    username = user.get("username")
    if username:
        return f"@{username}"
    first = user.get("first_name") or ""
    last = user.get("last_name") or ""
    label = f"{first} {last}".strip()
    return label or None


def alias_from_message(message: dict[str, Any]) -> str | None:
    signature = message.get("author_signature")
    if signature:
        return str(signature).strip()
    return alias_from_user(message.get("from"))


def has_meme_trigger(text: str) -> bool:
    lower = text.lower()
    return "сделай мем" in lower or "сделай мемчик" in lower or "делай мем" in lower or bool(TRIGGER_RE.search(lower))


def starts_with_tagir(text: str, name: str) -> bool:
    text = (text or "").strip()
    if not text:
        return False
    names = {name.lower().strip() or "тагир", "тагир", "tagir"}
    escaped = "|".join(re.escape(item) for item in sorted(names, key=len, reverse=True) if item)
    return bool(re.match(rf"^\s*(?:{escaped})(?=$|[\s,.:;!?\-—])", text, flags=re.IGNORECASE))


def is_tagir_draw_request(text: str, name: str) -> bool:
    text = (text or "").strip()
    if not text:
        return False
    names = {name.lower().strip() or "тагир", "тагир", "tagir"}
    escaped = "|".join(re.escape(item) for item in sorted(names, key=len, reverse=True) if item)
    pattern = rf"^\s*(?:{escaped})\s*[,.:;!?\-—]*\s*нарисуй(?=$|[\s,.:;!?\-—])"
    return bool(re.match(pattern, text, flags=re.IGNORECASE))


def extract_tagir_draw_prompt(text: str, name: str) -> str:
    names = {name.lower().strip() or "тагир", "тагир", "tagir"}
    escaped = "|".join(re.escape(item) for item in sorted(names, key=len, reverse=True) if item)
    pattern = rf"^\s*(?:{escaped})\s*[,.:;!?\-—]*\s*нарисуй\s*[,.:;!?\-—]*\s*"
    return re.sub(pattern, "", (text or "").strip(), flags=re.IGNORECASE).strip()


def owner_keyboard(enabled: bool) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "🧠 отправить бред", "callback_data": "send_random"}],
            [
                {"text": "🏷 бред с тегом", "callback_data": "ask_tag"},
                {"text": "🖼 отправить мем", "callback_data": "send_meme"},
            ],
            [
                {"text": "✍️ написать в канал", "callback_data": "ask_post"},
                {"text": "📊 рандом опрос", "callback_data": "send_poll"},
            ],
            [
                {"text": "🤡 реакция на последний", "callback_data": "react_last"},
                {"text": "🍽 скормить фразу", "callback_data": "ask_learn"},
            ],
            [
                {"text": "📚 статистика", "callback_data": "stats"},
                {"text": "🩺 проверка Тагира", "callback_data": "tagir_diag"},
            ],
            [
                {"text": "🔇 выключить хаос" if enabled else "🔊 включить хаос", "callback_data": "toggle"},
                {"text": "❌ сбросить режим ввода", "callback_data": "cancel_mode"},
            ],
        ]
    }


class SglypaChannelBot:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.api = TelegramBotAPI(config.bot_token)
        self.brain = Brain(config.brain_path, max_recent_messages=config.max_recent_messages)
        self.state = BotState(config.state_path)
        self.memes = MemeGenerator(
            project_root=PROJECT_ROOT,
            memes_dir=config.memes_dir,
            output_dir=config.generated_dir,
            font_path=config.font_path,
        )
        self.openai = OpenAIResponder(config)
        self.search = FreeSearchClient(config)
        self.brain_lock = asyncio.Lock()
        self.offset: int | None = None
        self.last_admin_refresh = 0.0

    async def run(self) -> None:
        self.config.data_dir.mkdir(parents=True, exist_ok=True)
        self.config.generated_dir.mkdir(parents=True, exist_ok=True)
        self.config.memes_dir.mkdir(parents=True, exist_ok=True)

        async with self.api:
            await self.api.delete_webhook(drop_pending_updates=self.config.drop_pending_updates)
            me = await self.api.get_me()
            log.info("Бот запущен: @%s (%s)", me.get("username"), me.get("id"))
            await self.refresh_admin_cache()

            background_task = asyncio.create_task(self.background_loop(), name="background-loop")
            try:
                await self.polling_loop()
            finally:
                background_task.cancel()
                with contextlib_suppress(asyncio.CancelledError):
                    await background_task
                await self.openai.close()
                await self.search.close()

    async def polling_loop(self) -> None:
        allowed_updates = ["message", "channel_post", "edited_channel_post", "callback_query"]
        while True:
            try:
                updates = await self.api.get_updates(offset=self.offset, timeout=60, allowed_updates=allowed_updates)
                for update in updates:
                    self.offset = int(update["update_id"]) + 1
                    try:
                        await self.handle_update(update)
                    except Exception:  # noqa: BLE001
                        log.exception("Ошибка обработки update_id=%s", update.get("update_id"))
            except TelegramAPIError as exc:
                log.warning("Telegram API: %s", exc)
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("Неожиданная ошибка polling")
                await asyncio.sleep(5)

    async def handle_update(self, update: dict[str, Any]) -> None:
        if callback := update.get("callback_query"):
            await self.handle_callback(callback)
            return

        if message := update.get("message"):
            await self.handle_private_message(message)
            return

        if channel_post := update.get("channel_post"):
            await self.handle_channel_post(channel_post, edited=False)
            return

        if edited_post := update.get("edited_channel_post"):
            await self.handle_channel_post(edited_post, edited=True)

    async def handle_channel_post(self, message: dict[str, Any], *, edited: bool) -> None:
        chat = message.get("chat") or {}
        if int(chat.get("id", 0)) != self.config.channel_id:
            return

        message_id = int(message.get("message_id"))
        self.state.set_last_channel_message_id(message_id)
        self.state.save()

        text = message_text(message)
        label = alias_from_message(message)
        if text:
            async with self.brain_lock:
                learned = self.brain.learn(text, source_label=label, message_id=message_id)
                self.brain.save()
            if learned:
                log.info("Выучено слов: %s из message_id=%s", learned, message_id)

        if edited or not self.state.enabled:
            return

        if text and starts_with_tagir(text, self.config.tagir_name):
            if is_tagir_draw_request(text, self.config.tagir_name):
                await self.draw_as_tagir(message, text)
            else:
                await self.answer_as_tagir(message, text)
            return

        # Реакции не блокируют остальную логику: если канал их запретил, бот просто продолжит жить.
        if random.random() < self.config.reaction_chance:
            await self.set_random_reaction(message_id)

        if text and has_meme_trigger(text):
            reply = message.get("reply_to_message") or {}
            source_text = message_text(reply) or text
            await self.send_random_meme(source_text=source_text, reply_to_message_id=message_id)
            return

        if random.random() < self.config.on_message_action_chance:
            await self.random_channel_action(reply_to_message_id=message_id)

    async def handle_private_message(self, message: dict[str, Any]) -> None:
        chat = message.get("chat") or {}
        if chat.get("type") != "private":
            return
        user = message.get("from") or {}
        user_id = int(user.get("id", 0))
        if user_id != self.config.owner_id:
            return

        text = (message.get("text") or "").strip()
        chat_id = int(chat.get("id"))

        if text in {"/start", "/panel", "панель"}:
            await self.send_owner_panel(chat_id)
            return

        if text == "/stats":
            async with self.brain_lock:
                summary = self.brain.summary()
            await self.api.send_message(chat_id, summary, reply_markup=owner_keyboard(self.state.enabled))
            return

        if text in {"/tagir", "/tagir_status", "/diag"}:
            await self.send_tagir_status(chat_id)
            return

        if text.startswith("/tagirtest"):
            question = text.removeprefix("/tagirtest").strip() or "привет"
            await self.run_tagir_diagnostic(chat_id, question)
            return

        mode = self.state.get_owner_mode(self.config.owner_id)
        if text in {"/cancel", "отмена"}:
            self.state.set_owner_mode(self.config.owner_id, None)
            self.state.save()
            await self.api.send_message(chat_id, "Сбросил режим ввода.", reply_markup=owner_keyboard(self.state.enabled))
            return

        if mode == "await_tag":
            await self.owner_send_tagged(text, chat_id)
            return

        if mode == "await_post":
            await self.owner_send_custom_post(text, chat_id)
            return

        if mode == "await_learn":
            await self.owner_learn_phrase(text, chat_id)
            return

        # На любой другой текст от владельца отвечаем панелью, чтобы бот не болтал в личке бесконечно.
        await self.send_owner_panel(chat_id, prefix="Я понял только кнопки панели и /stats.")

    async def handle_callback(self, callback: dict[str, Any]) -> None:
        user = callback.get("from") or {}
        user_id = int(user.get("id", 0))
        callback_id = str(callback.get("id"))
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = int(chat.get("id", self.config.owner_id))

        if user_id != self.config.owner_id:
            await self.api.answer_callback_query(callback_id, "не для тебя")
            return

        action = str(callback.get("data") or "")
        await self.api.answer_callback_query(callback_id)

        if action == "send_random":
            sent = await self.send_random_text()
            if sent:
                await self.api.send_message(chat_id, f"Улетело: {sent}", reply_markup=owner_keyboard(self.state.enabled))
            else:
                await self.api.send_message(chat_id, "Память пустая: сначала нужны слова из канала.", reply_markup=owner_keyboard(self.state.enabled))
        elif action == "ask_tag":
            self.state.set_owner_mode(self.config.owner_id, "await_tag")
            self.state.save()
            await self.api.send_message(chat_id, "Кого тегнуть? Пришли @username или любой текст.")
        elif action == "send_meme":
            path = await self.send_random_meme()
            if path:
                await self.api.send_message(chat_id, f"Мем отправлен: {Path(path).name}", reply_markup=owner_keyboard(self.state.enabled))
            else:
                await self.api.send_message(chat_id, "Мем не отправлен: памяти пока мало.", reply_markup=owner_keyboard(self.state.enabled))
        elif action == "ask_post":
            self.state.set_owner_mode(self.config.owner_id, "await_post")
            self.state.save()
            await self.api.send_message(chat_id, "Пришли текст, который бот напишет в канал.")
        elif action == "send_poll":
            question = await self.send_random_poll()
            if question:
                await self.api.send_message(chat_id, f"Опрос отправлен: {question}", reply_markup=owner_keyboard(self.state.enabled))
            else:
                await self.api.send_message(chat_id, "Опрос не отправлен: памяти пока мало.", reply_markup=owner_keyboard(self.state.enabled))
        elif action == "react_last":
            ok = await self.react_to_last_post()
            await self.api.send_message(chat_id, "Реакция поставлена." if ok else "Не вышло поставить реакцию.", reply_markup=owner_keyboard(self.state.enabled))
        elif action == "ask_learn":
            self.state.set_owner_mode(self.config.owner_id, "await_learn")
            self.state.save()
            await self.api.send_message(chat_id, "Пришли фразу, которую надо скормить в память без публикации.")
        elif action == "tagir_diag":
            await self.run_tagir_diagnostic(chat_id, "привет, скажи что ты жив")
        elif action == "stats":
            async with self.brain_lock:
                summary = self.brain.summary()
            await self.api.send_message(chat_id, summary, reply_markup=owner_keyboard(self.state.enabled))
        elif action == "toggle":
            self.state.set_enabled(not self.state.enabled)
            self.state.save()
            await self.api.send_message(
                chat_id,
                "Хаос включен." if self.state.enabled else "Хаос выключен. Учиться продолжу, но сам постить не буду.",
                reply_markup=owner_keyboard(self.state.enabled),
            )
        elif action == "cancel_mode":
            self.state.set_owner_mode(self.config.owner_id, None)
            self.state.save()
            await self.api.send_message(chat_id, "Режим ввода сброшен.", reply_markup=owner_keyboard(self.state.enabled))
        else:
            await self.send_owner_panel(chat_id, prefix="Неизвестная кнопка.")

    async def send_owner_panel(self, chat_id: int, *, prefix: str | None = None) -> None:
        templates = len(self.memes.find_templates())
        text = prefix + "\n\n" if prefix else ""
        tagir_status = "включен" if self.openai.enabled else "выключен"
        text += (
            "🐸 Панель канального бредогенератора\n"
            f"Канал: {self.config.channel_id}\n"
            f"Мем-шаблонов найдено: {templates}\n"
            f"Статус хаоса: {'включен' if self.state.enabled else 'выключен'}\n"
            f"Тагир: {tagir_status}"
        )
        await self.api.send_message(chat_id, text, reply_markup=owner_keyboard(self.state.enabled))

    async def owner_send_tagged(self, tag: str, chat_id: int) -> None:
        tag = tag.strip()[:80]
        if not tag:
            await self.api.send_message(chat_id, "Пришли тег или /cancel.")
            return
        async with self.brain_lock:
            text = self.brain.generate_sentence(min_words=3, max_words=13)
            self.brain.save()
        if not text.strip():
            await self.api.send_message(chat_id, "Память пустая: сначала нужны слова из канала.")
            return
        full_text = f"{tag} {text}"[:4096]
        await self.send_channel_message(full_text)
        self.state.set_owner_mode(self.config.owner_id, None)
        self.state.save()
        await self.api.send_message(chat_id, f"Отправил: {full_text}", reply_markup=owner_keyboard(self.state.enabled))

    async def owner_send_custom_post(self, text: str, chat_id: int) -> None:
        text = text.strip()
        if not text:
            await self.api.send_message(chat_id, "Пустое не отправил. Пришли текст или /cancel.")
            return
        await self.send_channel_message(text[:4096])
        self.state.set_owner_mode(self.config.owner_id, None)
        self.state.save()
        await self.api.send_message(chat_id, "Готово, написал в канал.", reply_markup=owner_keyboard(self.state.enabled))

    async def owner_learn_phrase(self, text: str, chat_id: int) -> None:
        if not text.strip():
            await self.api.send_message(chat_id, "Пустое не съел. Пришли фразу или /cancel.")
            return
        async with self.brain_lock:
            learned = self.brain.learn(text, source_label="owner_panel")
            self.brain.save()
        self.state.set_owner_mode(self.config.owner_id, None)
        self.state.save()
        await self.api.send_message(chat_id, f"Съел слов: {learned}", reply_markup=owner_keyboard(self.state.enabled))

    async def send_channel_message(self, text: str, *, reply_to_message_id: int | None = None) -> dict[str, Any]:
        sent = await self.api.send_message(self.config.channel_id, text, reply_to_message_id=reply_to_message_id)
        if message_id := sent.get("message_id"):
            self.state.set_last_channel_message_id(int(message_id))
            self.state.save()
        return sent

    async def send_random_text(self, *, reply_to_message_id: int | None = None, tagged: bool = False) -> str | None:
        async with self.brain_lock:
            text = self.brain.generate_sentence(min_words=3, max_words=13)
            if not text.strip():
                return None
            if tagged:
                tag = self.brain.random_admin_label(prefer_username=True)
                if tag:
                    text = f"{tag} {text}"
            self.brain.save()
        await self.send_channel_message(text, reply_to_message_id=reply_to_message_id)
        return text

    async def send_random_meme(self, *, source_text: str | None = None, reply_to_message_id: int | None = None) -> Path | None:
        async with self.brain_lock:
            if source_text:
                word_count = len(tokenize(source_text))
                meme_text = source_text if 1 <= word_count <= 16 and len(source_text) <= 180 else self.brain.random_words_from_text(source_text)
            else:
                recent = self.brain.random_recent_text()
                if recent and random.random() < 0.35:
                    meme_text = self.brain.random_words_from_text(recent)
                else:
                    meme_text = self.brain.generate_sentence(min_words=3, max_words=13)
            if not meme_text.strip():
                return None
            self.brain.mark_meme_generated()
            self.brain.save()

        path = self.memes.generate(meme_text)
        sent = await self.api.send_photo(
            self.config.channel_id,
            path,
            caption=None,
            reply_to_message_id=reply_to_message_id,
        )
        if message_id := sent.get("message_id"):
            self.state.set_last_channel_message_id(int(message_id))
            self.state.save()
        return path

    async def send_random_poll(self) -> str | None:
        async with self.brain_lock:
            question = self.brain.generate_sentence(min_words=3, max_words=9).rstrip(".!?) ")
            if not question.strip():
                return None
            if not question.endswith("?"):
                question += "?"

            options_count = random.randint(2, 6)
            options: list[dict[str, Any]] = []
            seen: set[str] = set()
            attempts = 0
            while len(options) < options_count and attempts < 40:
                attempts += 1
                option = self.brain.generate_sentence(min_words=1, max_words=4, add_emoji=random.random() < 0.25)
                option = option.strip(".!? ")[:100]
                if not option:
                    continue
                key = option.lower()
                if key in seen:
                    continue
                seen.add(key)
                options.append({"text": option})
            if len(options) < 2:
                return None

            description = self.brain.generate_sentence(min_words=4, max_words=14) if random.random() < 0.65 else None
            description = description if description and description.strip() else None
            self.brain.mark_poll_generated()
            self.brain.save()

        media_path = self.memes.random_template() if random.random() < self.config.poll_media_chance else None
        try:
            sent = await self.api.send_poll(
                self.config.channel_id,
                question,
                options,
                description=description,
                media_path=media_path,
                allows_multiple_answers=random.random() < 0.22,
            )
        except TelegramAPIError as exc:
            if media_path:
                log.warning("Опрос с медиа не отправился, пробую без медиа: %s", exc.description)
                sent = await self.api.send_poll(
                    self.config.channel_id,
                    question,
                    options,
                    description=description,
                    media_path=None,
                    allows_multiple_answers=random.random() < 0.22,
                )
            else:
                raise

        if message_id := sent.get("message_id"):
            self.state.set_last_channel_message_id(int(message_id))
            self.state.save()
        return question

    async def draw_as_tagir(self, message: dict[str, Any], text: str) -> None:
        message_id = int(message.get("message_id"))
        log.info("Тагир-картинка триггер: message_id=%s text=%r", message_id, text[:140])

        if not self.openai.image_enabled:
            reason = self.openai.image_disabled_reason() or "неизвестно"
            msg = f"Тагир поймал запрос на картинку, но не рисует: {reason}"
            log.warning("%s", msg)
            await self.notify_owner(msg)
            if self.config.tagir_error_to_channel:
                await self.safe_channel_error_reply(message_id, "Тагир пока не умеет рисовать: проверь TAGIR_IMAGE_*.")
            return

        prompt = extract_tagir_draw_prompt(text, self.config.tagir_name)
        reply = message.get("reply_to_message") or {}
        replied_text = message_text(reply) or None
        async with self.brain_lock:
            recent = self.brain.recent_texts(limit=8)

        image_path, final_prompt = await self.openai.generate_image(
            prompt,
            output_dir=self.config.generated_dir,
            replied_text=replied_text,
            recent_channel_texts=recent,
        )
        if image_path is None:
            reason = self.openai.last_error or "Image API не вернул картинку"
            log.warning("Тагир не смог сгенерировать картинку: %s", reason)
            await self.notify_owner(
                "⚠️ Ошибка Тагира (картинка)\n"
                f"Модель: {self.config.tagir_image_model}\n"
                f"Сообщение: {text[:250]}\n"
                f"Промпт: {(final_prompt or prompt)[:400]}\n"
                f"Причина: {reason}"
            )
            if self.config.tagir_error_to_channel:
                await self.safe_channel_error_reply(message_id, "Тагир не смог нарисовать, ошибка ушла владельцу.")
            return

        sent = await self.api.send_photo(
            self.config.channel_id,
            image_path,
            caption=None,
            reply_to_message_id=message_id,
            disable_notification=True,
        )
        if sent_id := sent.get("message_id"):
            self.state.set_last_channel_message_id(int(sent_id))
            self.state.save()

    async def answer_as_tagir(self, message: dict[str, Any], text: str) -> None:
        message_id = int(message.get("message_id"))
        log.info("Тагир-триггер пойман: message_id=%s text=%r", message_id, text[:120])

        if not self.openai.enabled:
            reason = self.openai.disabled_reason() or "неизвестно"
            msg = f"Тагир поймал обращение, но не отвечает: {reason}"
            log.warning("%s", msg)
            await self.notify_owner(msg)
            if self.config.tagir_error_to_channel:
                await self.safe_channel_error_reply(message_id, "Тагир не настроен: проверь OPENAI_API_KEY/TAGIR_ENABLED.")
            return

        reply = message.get("reply_to_message") or {}
        replied_text = message_text(reply) or None
        async with self.brain_lock:
            recent = self.brain.recent_texts(limit=8)

        web_context_parts: list[str] = []
        live_context = await fetch_live_context(text, timeout_seconds=min(10, self.config.search_timeout_seconds))
        if live_context:
            web_context_parts.append(live_context.text)
            log.info("Прямые данные для Тагира: source=%s text=%r", live_context.source, live_context.text[:220])
            if self.config.search_debug_to_owner:
                await self.notify_owner(
                    "📌 Тагир получил точные данные\n"
                    f"Источник: {live_context.source}\n"
                    f"{live_context.text[:500]}"
                )

        web_context = "\n\n".join(web_context_parts) if web_context_parts else None
        if self.search.enabled and wants_web_search(text, always=self.config.search_always):
            query = make_search_query(text, self.config.tagir_name)
            started = time.monotonic()
            if self.config.search_debug_to_owner:
                await self.notify_owner(
                    "🔎 Тагир ищет в интернете\n"
                    f"Провайдер: {self.config.search_provider}\n"
                    f"Запрос: {query[:300]}"
                )
            try:
                results = await asyncio.wait_for(
                    self.search.search(query),
                    timeout=max(5, self.config.search_timeout_seconds),
                )
            except asyncio.TimeoutError:
                results = []
                self.search.last_error = f"search timeout after {self.config.search_timeout_seconds}s"
            elapsed = time.monotonic() - started
            search_context = format_search_context(results)
            if search_context:
                web_context_parts.append(search_context)
                web_context = "\n\n".join(web_context_parts)
                log.info("Веб-поиск для Тагира: query=%r results=%s elapsed=%.1fs", query, len(results), elapsed)
                if self.config.search_debug_to_owner:
                    preview = "\n".join(f"{i}. {item.title[:120]}" for i, item in enumerate(results[:3], start=1))
                    await self.notify_owner(
                        "✅ Поиск сработал\n"
                        f"Запрос: {query[:250]}\n"
                        f"Найдено: {len(results)} за {elapsed:.1f}с\n"
                        f"{preview}"
                    )
            else:
                log.info("Веб-поиск для Тагира ничего не дал: query=%r error=%r elapsed=%.1fs", query, self.search.last_error, elapsed)
                if self.config.search_debug_to_owner:
                    await self.notify_owner(
                        "⚠️ Поиск ничего не дал\n"
                        f"Запрос: {query[:250]}\n"
                        f"Ошибка: {self.search.last_error or 'результатов нет'}\n"
                        f"Время: {elapsed:.1f}с"
                    )

        await self.notify_owner(
            "🧠 Тагир отправляет запрос в нейронку\n"
            f"Сообщение: {text[:300]}\n"
            f"Есть web_context: {'да' if web_context else 'нет'}"
        )

        answer = await self.openai.answer(
            text,
            replied_text=replied_text,
            recent_channel_texts=recent,
            web_context=web_context,
        )

        if not answer:
            reason = self.openai.last_error or "OpenAI не вернул текст"
            log.warning("Тагир не смог получить ответ: %s", reason)
            await self.notify_owner(
                "❌ Нейронка не вернула ответ\n"
                f"Модель: {self.config.openai_model}\n"
                f"Сообщение: {text[:300]}\n"
                f"Ошибка: {reason}"
            )
            if self.config.tagir_error_to_channel:
                await self.safe_channel_error_reply(message_id, "Тагир сейчас сломался, ошибка ушла владельцу.")
            return

        await self.notify_owner(
            "✅ Нейронка ответила\n"
            f"Длина ответа: {len(answer)}\n"
            f"Ответ: {answer[:500]}"
        )

        formatted_answer, parse_mode = format_tagir_answer_for_telegram(answer)
        try:
            sent = await self.api.send_message(
                self.config.channel_id,
                formatted_answer,
                reply_to_message_id=message_id,
                disable_notification=True,
                parse_mode=parse_mode,
            )
            await self.notify_owner("✅ Ответ отправлен в канал")
        except Exception as exc:  # noqa: BLE001
            log.exception("Тагир получил ответ, но не смог отправить его в канал")
            await self.notify_owner(
                "❌ Ошибка отправки в канал\n"
                f"{type(exc).__name__}: {exc}\n"
                f"Ответ был: {answer[:700]}"
            )
            return

        if sent_id := sent.get("message_id"):
            self.state.set_last_channel_message_id(int(sent_id))
            self.state.save()

    async def notify_owner(self, text: str) -> None:
        if not self.config.tagir_debug_to_owner:
            return
        try:
            await self.api.send_message(self.config.owner_id, text[:4096])
        except TelegramAPIError as exc:
            log.warning("Не удалось отправить диагностику владельцу: %s", exc.description)

    async def safe_channel_error_reply(self, reply_to_message_id: int, text: str) -> None:
        try:
            await self.api.send_message(self.config.channel_id, text, reply_to_message_id=reply_to_message_id, disable_notification=True)
        except TelegramAPIError as exc:
            log.warning("Не удалось отправить ошибку Тагира в канал: %s", exc.description)

    async def send_tagir_status(self, chat_id: int) -> None:
        status = "включен" if self.openai.enabled else f"выключен: {self.openai.disabled_reason() or 'нет причины'}"
        last_error = self.openai.last_error or "нет"
        text = (
            "🩺 Статус Тагира\n"
            f"TAGIR_ENABLED: {self.config.tagir_enabled}\n"
            f"OPENAI_API_KEY: {'есть' if self.config.openai_api_key else 'нет'}\n"
            f"OPENAI_MODEL: {self.config.openai_model}\n"
            f"OPENAI_WEB_SEARCH: {self.config.openai_web_search}\n"
            f"OPENAI_WEB_SEARCH_TOOL: {self.config.openai_web_search_tool}\n"
            f"Статус: {status}\n"
            f"Последняя ошибка: {last_error}\n\n"
            "Для проверки напиши: /tagirtest привет"
        )
        await self.api.send_message(chat_id, text[:4096], reply_markup=owner_keyboard(self.state.enabled))

    async def run_tagir_diagnostic(self, chat_id: int, question: str) -> None:
        await self.api.send_message(chat_id, "Проверяю Тагира через OpenAI...")
        ok, result = await self.openai.diagnostic(question)
        if ok:
            await self.api.send_message(chat_id, f"✅ Тагир отвечает:\n{result}", reply_markup=owner_keyboard(self.state.enabled))
        else:
            errors = "\n".join(f"- {item}" for item in self.openai.last_errors[-5:]) or result
            await self.api.send_message(
                chat_id,
                (
                    "❌ Тагир не отвечает\n"
                    f"Модель: {self.config.openai_model}\n"
                    f"Причина: {result}\n"
                    f"Ошибки:\n{errors}"
                )[:4096],
                reply_markup=owner_keyboard(self.state.enabled),
            )

    async def set_random_reaction(self, message_id: int) -> bool:
        emoji = random.choice(REACTIONS)
        try:
            ok = await self.api.set_message_reaction(
                self.config.channel_id,
                message_id,
                emoji,
                is_big=random.random() < 0.25,
            )
        except TelegramAPIError as exc:
            log.info("Не удалось поставить реакцию %s на %s: %s", emoji, message_id, exc.description)
            return False
        if ok:
            async with self.brain_lock:
                self.brain.mark_reaction_set()
                self.brain.save()
        return ok

    async def react_to_last_post(self) -> bool:
        message_id = self.state.last_channel_message_id
        if not message_id:
            return False
        return await self.set_random_reaction(message_id)

    async def random_channel_action(self, *, reply_to_message_id: int | None = None) -> None:
        roll = random.random()
        if roll < 0.48:
            await self.send_random_text()
        elif roll < 0.67:
            await self.send_random_text(reply_to_message_id=reply_to_message_id, tagged=True)
        elif roll < 0.84:
            await self.send_random_meme(reply_to_message_id=reply_to_message_id if random.random() < 0.45 else None)
        elif roll < 0.95:
            await self.send_random_poll()
        else:
            async with self.brain_lock:
                emoji = self.brain.random_emoji()
            await self.send_channel_message(emoji)

    async def background_loop(self) -> None:
        while True:
            await asyncio.sleep(random.randint(self.config.idle_min_seconds, self.config.idle_max_seconds))

            if time.time() - self.last_admin_refresh > 6 * 60 * 60:
                await self.refresh_admin_cache()

            if not self.state.enabled:
                continue
            if random.random() > self.config.idle_action_chance:
                continue
            try:
                await self.random_channel_action()
            except TelegramAPIError as exc:
                log.warning("Фоновое действие не удалось: %s", exc.description)
            except Exception:  # noqa: BLE001
                log.exception("Фоновое действие упало")

    async def refresh_admin_cache(self) -> None:
        self.last_admin_refresh = time.time()
        try:
            admins = await self.api.get_chat_administrators(self.config.channel_id)
        except TelegramAPIError as exc:
            log.warning("Не смог получить админов канала: %s", exc.description)
            return

        labels: list[str] = []
        for member in admins:
            label = alias_from_user(member.get("user"))
            if label:
                labels.append(label)
        if labels:
            async with self.brain_lock:
                self.brain.remember_admins(labels)
                self.brain.save()
            log.info("Админ-алиасы обновлены: %s", ", ".join(labels[:8]))


class contextlib_suppress:
    def __init__(self, *exceptions: type[BaseException]) -> None:
        self.exceptions = exceptions

    def __enter__(self) -> "contextlib_suppress":
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: object) -> bool:
        return exc_type is not None and issubclass(exc_type, self.exceptions)


async def amain() -> None:
    config = load_config()
    bot = SglypaChannelBot(config)
    await bot.run()


def main() -> None:
    setup_logging()
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        log.info("Остановлено с клавиатуры")
    except RuntimeError as exc:
        log.error("%s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
