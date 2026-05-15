from __future__ import annotations

import json
import random
import re
import tempfile
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

WORD_RE = re.compile(r"@?[A-Za-zА-Яа-яЁё0-9_]+(?:[-'][A-Za-zА-Яа-яЁё0-9_]+)?|[\U0001F300-\U0001FAFF]", re.UNICODE)
URL_RE = re.compile(r"https?://\S+|t\.me/\S+", re.IGNORECASE)

DEFAULT_EMOJIS = [
    "😐", "🤨", "🥴", "😎", "😭", "😳", "🤡", "💀", "🔥", "❤️", "👍", "🐸", "🤯", "😈", "🤝", "🫠",
]

PUNCT_ENDINGS = ["", "", "", ")", "...", "?", "!"]


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
        tmp = Path(fh.name)
    tmp.replace(path)


def _weighted_choice(mapping: dict[str, int]) -> str:
    if not mapping:
        raise ValueError("empty mapping")
    keys = list(mapping.keys())
    weights = [max(1, int(mapping[key])) for key in keys]
    return random.choices(keys, weights=weights, k=1)[0]


def clean_text(text: str) -> str:
    text = URL_RE.sub(" ", text or "")
    text = text.replace("\u200b", " ").replace("\ufeff", " ")
    return re.sub(r"\s+", " ", text).strip()


def tokenize(text: str) -> list[str]:
    text = clean_text(text)
    tokens = []
    for match in WORD_RE.finditer(text):
        token = match.group(0).strip()
        if not token:
            continue
        if token.startswith("/"):
            continue
        if len(token) > 40:
            continue
        tokens.append(token.lower())
    return tokens


def prettify_tokens(tokens: Iterable[str]) -> str:
    result = " ".join(token for token in tokens if token).strip()
    result = re.sub(r"\s+", " ", result)
    if not result:
        return random.choice(DEFAULT_EMOJIS)

    # Аккуратно возвращаем вид @username и не ломаем эмодзи.
    words = result.split()
    if words:
        first = words[0]
        if first and first[0].isalpha() and random.random() < 0.35:
            words[0] = first[0].upper() + first[1:]
        result = " ".join(words)

    result += random.choice(PUNCT_ENDINGS)
    return result.strip()


class Brain:
    def __init__(self, path: Path, *, max_recent_messages: int = 1000) -> None:
        self.path = path
        self.max_recent_messages = max_recent_messages
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty()
        with self.path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
        base = self._empty()
        base.update(payload)
        for key, value in base["stats"].items():
            base["stats"][key] = int(value or 0)
        return base

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "schema_version": 1,
            "word_counts": {},
            "transitions": {},
            "recent_messages": [],
            "admin_aliases": {},
            "stats": {
                "messages_seen": 0,
                "words_seen": 0,
                "generated_messages": 0,
                "generated_memes": 0,
                "generated_polls": 0,
                "reactions_set": 0,
            },
        }

    def save(self) -> None:
        atomic_write_json(self.path, self.data)

    @property
    def word_counts(self) -> dict[str, int]:
        return self.data.setdefault("word_counts", {})

    @property
    def transitions(self) -> dict[str, dict[str, int]]:
        return self.data.setdefault("transitions", {})

    @property
    def stats(self) -> dict[str, int]:
        return self.data.setdefault("stats", {})

    def learn(self, text: str, *, source_label: str | None = None, message_id: int | None = None) -> int:
        cleaned = clean_text(text)
        tokens = tokenize(cleaned)
        if not tokens:
            return 0

        counts = self.word_counts
        for token in tokens:
            counts[token] = int(counts.get(token, 0)) + 1

        transitions = self.transitions
        for prev, current in zip(tokens, tokens[1:], strict=False):
            bucket = transitions.setdefault(prev, {})
            bucket[current] = int(bucket.get(current, 0)) + 1

        self.stats["messages_seen"] = int(self.stats.get("messages_seen", 0)) + 1
        self.stats["words_seen"] = int(self.stats.get("words_seen", 0)) + len(tokens)

        recent = self.data.setdefault("recent_messages", [])
        recent.append(
            {
                "text": cleaned[:1000],
                "source_label": source_label,
                "message_id": message_id,
                "ts": int(time.time()),
            }
        )
        if len(recent) > self.max_recent_messages:
            del recent[: len(recent) - self.max_recent_messages]

        if source_label:
            self.remember_admin(source_label)
        return len(tokens)

    def remember_admin(self, label: str | None) -> None:
        if not label:
            return
        label = label.strip()
        if not label or len(label) > 80:
            return
        aliases = self.data.setdefault("admin_aliases", {})
        aliases[label] = int(aliases.get(label, 0)) + 1

    def remember_admins(self, labels: Iterable[str]) -> None:
        for label in labels:
            self.remember_admin(label)

    def random_admin_label(self, *, prefer_username: bool = True) -> str | None:
        aliases: dict[str, int] = self.data.setdefault("admin_aliases", {})
        if not aliases:
            return None
        candidates = aliases
        if prefer_username:
            usernames = {k: v for k, v in aliases.items() if k.startswith("@")}
            if usernames:
                candidates = usernames
        return _weighted_choice(candidates)

    def random_words_from_text(self, text: str, *, min_words: int = 3, max_words: int = 13) -> str:
        tokens = tokenize(text)
        if not tokens:
            return self.generate_sentence(min_words=min_words, max_words=max_words)
        random.shuffle(tokens)
        count = min(len(tokens), random.randint(min_words, max_words))
        return prettify_tokens(tokens[:count])

    def generate_sentence(self, *, min_words: int = 3, max_words: int = 13, add_emoji: bool = True) -> str:
        counts = self.word_counts
        if not counts:
            return self.fallback_sentence(add_emoji=add_emoji)

        target_len = random.randint(min_words, max_words)
        tokens: list[str] = []
        current = _weighted_choice(counts)
        tokens.append(current)

        for _ in range(target_len - 1):
            next_bucket = self.transitions.get(current) or {}
            if next_bucket and random.random() < 0.78:
                current = _weighted_choice(next_bucket)
            else:
                current = _weighted_choice(counts)
            tokens.append(current)

        text = prettify_tokens(tokens)
        if add_emoji:
            text = self.maybe_add_emoji(text)
        self.stats["generated_messages"] = int(self.stats.get("generated_messages", 0)) + 1
        return text

    def fallback_sentence(self, *, add_emoji: bool = True) -> str:
        samples = [
            "я пока пустой но уже странный",
            "канал молчит а я учусь",
            "сглыпный режим включен",
            "мне нужны слова срочно",
            "нейросеть на минималках проснулась",
        ]
        text = random.choice(samples)
        if add_emoji:
            text = self.maybe_add_emoji(text)
        return text

    def maybe_add_emoji(self, text: str, *, chance: float = 0.42) -> str:
        if random.random() > chance:
            return text
        emoji = random.choice(DEFAULT_EMOJIS)
        parts = text.split()
        if not parts or random.random() < 0.45:
            return f"{text} {emoji}".strip()
        index = random.randint(1, len(parts))
        parts.insert(index, emoji)
        return " ".join(parts)

    def random_emoji(self) -> str:
        return random.choice(DEFAULT_EMOJIS)

    def random_recent_text(self) -> str | None:
        recent = self.data.setdefault("recent_messages", [])
        if not recent:
            return None
        item = random.choice(recent)
        return item.get("text") or None

    def mark_meme_generated(self) -> None:
        self.stats["generated_memes"] = int(self.stats.get("generated_memes", 0)) + 1

    def mark_poll_generated(self) -> None:
        self.stats["generated_polls"] = int(self.stats.get("generated_polls", 0)) + 1

    def mark_reaction_set(self) -> None:
        self.stats["reactions_set"] = int(self.stats.get("reactions_set", 0)) + 1

    def summary(self) -> str:
        counts = self.word_counts
        aliases: dict[str, int] = self.data.setdefault("admin_aliases", {})
        stats = self.stats
        top_words = sorted(counts.items(), key=lambda item: item[1], reverse=True)[:8]
        top = ", ".join(f"{word}×{count}" for word, count in top_words) or "пока пусто"
        return (
            "📚 Память бота\n"
            f"Сообщений прочитано: {stats.get('messages_seen', 0)}\n"
            f"Слов прочитано: {stats.get('words_seen', 0)}\n"
            f"Уникальных слов: {len(counts)}\n"
            f"Админ-алиасов: {len(aliases)}\n"
            f"Сгенерировано сообщений: {stats.get('generated_messages', 0)}\n"
            f"Мемов: {stats.get('generated_memes', 0)}\n"
            f"Опросов: {stats.get('generated_polls', 0)}\n"
            f"Реакций: {stats.get('reactions_set', 0)}\n"
            f"Топ слов: {top}"
        )
