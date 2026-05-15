from __future__ import annotations

import asyncio
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
from sglypa_bot.state import BotState
from sglypa_bot.telegram_api import TelegramAPIError, TelegramBotAPI

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
TRIGGER_RE = re.compile(r"(?<![а-яёa-z])бля(?![а-яёa-z])", re.IGNORECASE)
REACTIONS = ["👍", "❤", "🤡"]

log = logging.getLogger("sglypa-channel-bot")


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


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
                {"text": "🔇 выключить хаос" if enabled else "🔊 включить хаос", "callback_data": "toggle"},
            ],
            [{"text": "❌ сбросить режим ввода", "callback_data": "cancel_mode"}],
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
        text += (
            "🐸 Панель канального бредогенератора\n"
            f"Канал: {self.config.channel_id}\n"
            f"Мем-шаблонов найдено: {templates}\n"
            f"Статус хаоса: {'включен' if self.state.enabled else 'выключен'}"
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
