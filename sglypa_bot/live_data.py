from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

SPACE_RE = re.compile(r"\s+")
CURRENCY_TRIGGER_RE = re.compile(
    r"(курс|сколько|поч[её]м|стоимост|цена|валют|рубл|rub|rur).*(доллар|долларов|usd|евро|eur|юан|юань|cny)|"
    r"(доллар|долларов|usd|евро|eur|юан|юань|cny).*(курс|сколько|поч[её]м|стоимост|цена|рубл|rub|rur)",
    re.IGNORECASE,
)


@dataclass(slots=True)
class LiveDataResult:
    source: str
    text: str


def wants_currency_rate(text: str) -> bool:
    return bool(CURRENCY_TRIGGER_RE.search(text or ""))


def detect_currency(text: str) -> str:
    lower = (text or "").lower()
    if "евро" in lower or "eur" in lower:
        return "EUR"
    if "юан" in lower or "cny" in lower:
        return "CNY"
    return "USD"


async def fetch_live_context(text: str, *, timeout_seconds: int = 10) -> LiveDataResult | None:
    """Fetch deterministic live data for queries where search snippets are too weak.

    Free web search often returns only page titles for exchange-rate queries. For these,
    use a direct machine-readable source, then pass the result to the LLM as context.
    """
    if wants_currency_rate(text):
        currency = detect_currency(text)
        rate = await fetch_cbr_currency_rate(currency, timeout_seconds=timeout_seconds)
        if rate:
            return rate
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

        text = (
            "Точные данные из прямого источника: "
            f"официальный курс ЦБ РФ на {date_text or 'сегодня'}: "
            f"1 {currency} ({name}) = {per_one:.4f} ₽{change_text}. "
            "Это официальный курс ЦБ, не наличный обменник и не внутридневная биржевая котировка."
        )
        return LiveDataResult(source="cbr.ru", text=text)

    return None
