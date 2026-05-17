from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote

import aiohttp

log = logging.getLogger(__name__)

SPACE_RE = re.compile(r"\s+")
CURRENCY_TRIGGER_RE = re.compile(
    r"(курс|сколько|поч[её]м|стоимост|цена|валют|рубл|rub|rur).*(доллар|долларов|usd|евро|eur|юан|юань|cny)|"
    r"(доллар|долларов|usd|евро|eur|юан|юань|cny).*(курс|сколько|поч[её]м|стоимост|цена|рубл|rub|rur)",
    re.IGNORECASE,
)

WIKIPEDIA_TRIGGER_RE = re.compile(
    r"\b(wiki|wikipedia|вики|википед(?:ия|ии|ию|ией)|энциклопед)\b|"
    r"\b(кто\s+так(?:ой|ая|ое|ие)|что\s+такое|что\s+за|расскажи\s+про|расскажи\s+о|биография)\b",
    re.IGNORECASE,
)

WIKI_BAD_QUERY_RE = re.compile(
    r"\b(тагир|tagir|пожалуйста|плиз|плз|найди|поищи|загугли|посмотри|прочитай|расскажи|объясни|"
    r"в\s+интернете|в\s+сети|на\s+вики|в\s+вики|википедия|википедии|википедию|wiki|wikipedia|"
    r"кто\s+такой|кто\s+такая|кто\s+такое|кто\s+такие|что\s+такое|что\s+за|статья|статью|про|о)\b",
    re.IGNORECASE,
)


@dataclass(slots=True)
class LiveDataResult:
    source: str
    text: str


def wants_currency_rate(text: str) -> bool:
    return bool(CURRENCY_TRIGGER_RE.search(text or ""))


def wants_wikipedia(text: str) -> bool:
    return bool(WIKIPEDIA_TRIGGER_RE.search(text or ""))


def detect_currency(text: str) -> str:
    lower = (text or "").lower()
    if "евро" in lower or "eur" in lower:
        return "EUR"
    if "юан" in lower or "cny" in lower:
        return "CNY"
    return "USD"


def extract_wikipedia_query(text: str) -> str:
    text = SPACE_RE.sub(" ", (text or "").strip())
    text = re.sub(r"^\s*(?:тагир|tagir)\s*[,.:;!?\-—]*\s*", "", text, flags=re.IGNORECASE)
    text = WIKI_BAD_QUERY_RE.sub(" ", text)
    text = re.sub(r"[\"'«»`*_~|<>\[\]{}()]+", " ", text)
    text = re.sub(r"\s*[,.:;!?\-—]+\s*", " ", text)
    text = SPACE_RE.sub(" ", text).strip()
    return text[:160]


async def fetch_live_context(text: str, *, timeout_seconds: int = 10) -> LiveDataResult | None:
    """Fetch deterministic live data for queries where search snippets are too weak.

    Free web search often returns only page titles/snippets. For some popular cases,
    use direct machine-readable sources, then pass the result to the LLM as context.
    """
    if wants_currency_rate(text):
        currency = detect_currency(text)
        rate = await fetch_cbr_currency_rate(currency, timeout_seconds=timeout_seconds)
        if rate:
            return rate

    if wants_wikipedia(text):
        query = extract_wikipedia_query(text)
        if query:
            wiki = await fetch_wikipedia_summary(query, timeout_seconds=timeout_seconds)
            if wiki:
                return wiki

    return None


async def fetch_cbr_currency_rate(currency: str, *, timeout_seconds: int = 10) -> LiveDataResult | None:
    currency = currency.upper().strip()
    url = "https://www.cbr.ru/scripts/XML_daily.asp"
    try:
        timeout = aiohttp.ClientTimeout(total=timeout_seconds, connect=min(6, timeout_seconds), sock_read=timeout_seconds)
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; tagir-channel-bot/1.0)",
            "Accept": "application/xml,text/xml,*/*",
        }
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(url) as response:
                raw = await response.read()
                if response.status >= 400:
                    log.warning("CBR HTTP %s: %s", response.status, raw[:200])
                    return None
    except Exception as exc:  # noqa: BLE001
        log.warning("CBR request error: %s: %s", type(exc).__name__, exc)
        return None

    try:
        root = ET.fromstring(raw)
    except Exception as exc:  # noqa: BLE001
        log.warning("CBR XML parse error: %s: %s", type(exc).__name__, exc)
        return None

    date_raw = root.attrib.get("Date", "")
    date_text = date_raw
    if date_raw:
        try:
            date_text = datetime.strptime(date_raw, "%d.%m.%Y").strftime("%d.%m.%Y")
        except ValueError:
            pass

    for valute in root.findall("Valute"):
        char_code = (valute.findtext("CharCode") or "").upper().strip()
        if char_code != currency:
            continue

        name = (valute.findtext("Name") or currency).strip()
        nominal_raw = (valute.findtext("Nominal") or "1").strip()
        value_raw = (valute.findtext("Value") or "").strip().replace(",", ".")
        previous_raw = (valute.findtext("Previous") or "").strip().replace(",", ".")

        try:
            nominal = int(nominal_raw)
            value = float(value_raw)
        except ValueError:
            return None

        per_one = value / nominal if nominal else value
        change_text = ""
        try:
            previous = float(previous_raw)
            previous_one = previous / nominal if nominal else previous
            delta = per_one - previous_one
            if abs(delta) >= 0.0001:
                sign = "+" if delta > 0 else ""
                change_text = f", изменение к прошлому курсу: {sign}{delta:.4f} ₽"
        except ValueError:
            pass

        result_text = (
            "Точные данные из прямого источника: "
            f"официальный курс ЦБ РФ на {date_text or 'сегодня'}: "
            f"1 {currency} ({name}) = {per_one:.4f} ₽{change_text}. "
            "Это официальный курс ЦБ, не наличный обменник и не внутридневная биржевая котировка."
        )
        return LiveDataResult(source="cbr.ru", text=result_text)

    return None


async def fetch_wikipedia_summary(query: str, *, timeout_seconds: int = 10, lang: str = "ru") -> LiveDataResult | None:
    query = SPACE_RE.sub(" ", (query or "").strip())
    if not query:
        return None

    base = f"https://{lang}.wikipedia.org"
    timeout = aiohttp.ClientTimeout(total=timeout_seconds, connect=min(6, timeout_seconds), sock_read=timeout_seconds)
    headers = {
        "User-Agent": "tagir-channel-bot/1.0 (Telegram bot; educational/info queries)",
        "Accept": "application/json",
    }

    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            title = await _wiki_search_title(session, base, query)
            if not title:
                title = query
            summary = await _wiki_page_summary(session, base, title)
            if not summary and title != query:
                summary = await _wiki_page_summary(session, base, query)
    except Exception as exc:  # noqa: BLE001
        log.warning("Wikipedia request error: %s: %s", type(exc).__name__, exc)
        return None

    if not summary:
        return None

    title = str(summary.get("title") or query).strip()
    extract = str(summary.get("extract") or "").strip()
    page_url = ""
    content_urls = summary.get("content_urls")
    if isinstance(content_urls, dict):
        desktop = content_urls.get("desktop")
        if isinstance(desktop, dict):
            page_url = str(desktop.get("page") or "").strip()
    if not page_url:
        page_url = f"{base}/wiki/{quote(title.replace(' ', '_'))}"

    if not extract:
        return None

    if len(extract) > 1400:
        extract = extract[:1400].rsplit(" ", 1)[0].rstrip(" ,.;:") + "…"

    result_text = (
        "Точные данные из прямого источника: Wikipedia. "
        f"Статья: {title}. "
        f"Краткое содержание: {extract} "
        f"Ссылка: {page_url}"
    )
    return LiveDataResult(source="wikipedia.org", text=result_text)


async def _wiki_search_title(session: aiohttp.ClientSession, base: str, query: str) -> str | None:
    params = {
        "action": "query",
        "format": "json",
        "list": "search",
        "srsearch": query,
        "srlimit": "1",
        "utf8": "1",
    }
    async with session.get(f"{base}/w/api.php", params=params) as response:
        if response.status >= 400:
            raw = await response.text()
            log.warning("Wikipedia search HTTP %s: %s", response.status, raw[:200])
            return None
        data: dict[str, Any] = await response.json(content_type=None)

    search = data.get("query", {}).get("search", [])
    if isinstance(search, list) and search:
        first = search[0]
        if isinstance(first, dict) and isinstance(first.get("title"), str):
            return first["title"].strip() or None
    return None


async def _wiki_page_summary(session: aiohttp.ClientSession, base: str, title: str) -> dict[str, Any] | None:
    encoded_title = quote(title.replace(" ", "_"), safe="")
    async with session.get(f"{base}/api/rest_v1/page/summary/{encoded_title}") as response:
        raw = await response.text()
        if response.status >= 400:
            log.warning("Wikipedia summary HTTP %s for %r: %s", response.status, title, raw[:200])
            return None
        data = await response.json(content_type=None)
    return data if isinstance(data, dict) else None
