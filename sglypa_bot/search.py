from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import aiohttp

from .config import Config

log = logging.getLogger(__name__)

TAGIR_PREFIX_RE = re.compile(r"^\s*(тагир|tagir)\s*[,.:;!?\-—]*\s*", re.IGNORECASE)
SEARCH_TRIGGER_RE = re.compile(
    r"(поищи|найди|загугли|проверь|посмотри|в интернете|инет|гугл|гугле|"
    r"курс|доллар|евро|юань|usd|eur|cny|рубл|"
    r"погода|температур|дожд|снег|"
    r"новост|сегодня|сейчас|актуальн|последн|свеж|что случилось|"
    r"кто выиграл|счет|счёт|матч|турнир|"
    r"цена|стоимость|сколько стоит|биткоин|bitcoin|btc|эфир|ethereum|eth)",
    re.IGNORECASE,
)
TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")


@dataclass(slots=True)
class SearchResult:
    title: str
    url: str
    snippet: str = ""


def wants_web_search(text: str, *, always: bool = False) -> bool:
    if always:
        return True
    return bool(SEARCH_TRIGGER_RE.search(text or ""))


def make_search_query(text: str, tagir_name: str = "тагир") -> str:
    query = (text or "").strip()
    names = {tagir_name.lower().strip() or "тагир", "тагир", "tagir"}
    escaped = "|".join(re.escape(item) for item in sorted(names, key=len, reverse=True) if item)
    if escaped:
        query = re.sub(rf"^\s*(?:{escaped})\s*[,.:;!?\-—]*\s*", "", query, flags=re.IGNORECASE)
    query = SPACE_RE.sub(" ", query).strip()
    return query[:280]


def format_search_context(results: list[SearchResult]) -> str:
    if not results:
        return ""
    lines = ["Актуальные данные из веб-поиска. Используй их для ответа, не выдумывай свежие факты:"]
    for index, item in enumerate(results, start=1):
        line = f"{index}. {item.title}"
        if item.snippet:
            line += f" — {item.snippet}"
        if item.url:
            line += f" ({item.url})"
        lines.append(line[:850])
    return "\n".join(lines)


class FreeSearchClient:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.session: aiohttp.ClientSession | None = None
        self.last_error: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.config.search_enabled)

    async def open(self) -> None:
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(
                total=self.config.search_timeout_seconds,
                connect=min(8, self.config.search_timeout_seconds),
                sock_read=self.config.search_timeout_seconds,
            )
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                ),
                "Accept-Language": "ru,en;q=0.8",
            }
            self.session = aiohttp.ClientSession(timeout=timeout, headers=headers)

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()

    async def search(self, query: str) -> list[SearchResult]:
        self.last_error = None
        query = SPACE_RE.sub(" ", (query or "").strip())[:280]
        if not self.enabled or not query:
            return []

        provider = (self.config.search_provider or "auto").lower().strip()
        if provider not in {"auto", "duckduckgo", "ddg", "searxng"}:
            provider = "auto"

        if provider in {"auto", "duckduckgo", "ddg"}:
            results = await self._search_duckduckgo(query)
            if results:
                return results[: self.config.search_max_results]
            if provider in {"duckduckgo", "ddg"}:
                return []

        if provider in {"auto", "searxng"}:
            results = await self._search_searxng(query)
            if results:
                return results[: self.config.search_max_results]

        return []

    async def _search_duckduckgo(self, query: str) -> list[SearchResult]:
        await self.open()
        assert self.session is not None
        url = f"https://duckduckgo.com/html/?q={quote_plus(query)}&kl=ru-ru"
        try:
            async with self.session.get(url) as response:
                raw = await response.text(errors="ignore")
                if response.status >= 400:
                    self.last_error = f"DuckDuckGo HTTP {response.status}: {raw[:200]}"
                    return []
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"DuckDuckGo error: {type(exc).__name__}: {exc}"
            log.debug("%s", self.last_error)
            return []

        return self._parse_duckduckgo_html(raw)

    def _parse_duckduckgo_html(self, raw: str) -> list[SearchResult]:
        results: list[SearchResult] = []
        blocks = re.split(r'<div[^>]+class="[^"]*result[^"]*"[^>]*>', raw)
        for block in blocks[1:]:
            title_match = re.search(r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
            if not title_match:
                continue
            href = html.unescape(title_match.group(1))
            title = self._clean_html(title_match.group(2))
            url = self._clean_duckduckgo_url(href)
            snippet_match = re.search(r'<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>', block, re.S)
            if not snippet_match:
                snippet_match = re.search(r'<div[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</div>', block, re.S)
            snippet = self._clean_html(snippet_match.group(1)) if snippet_match else ""
            if title and url:
                results.append(SearchResult(title=title, url=url, snippet=snippet))
            if len(results) >= self.config.search_max_results:
                break

        # Lite/alternate layout fallback.
        if not results:
            for href, title_html in re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', raw, re.S):
                title = self._clean_html(title_html)
                url = self._clean_duckduckgo_url(html.unescape(href))
                if title and url and not url.startswith("/html") and "duckduckgo.com" not in title.lower():
                    results.append(SearchResult(title=title, url=url))
                if len(results) >= self.config.search_max_results:
                    break
        return results

    async def _search_searxng(self, query: str) -> list[SearchResult]:
        await self.open()
        assert self.session is not None
        bases = self.config.search_searxng_urls or []
        if not bases:
            bases = [
                "https://searx.be/search",
                "https://search.inetol.net/search",
                "https://searx.tiekoetter.com/search",
            ]

        for base in bases:
            url = f"{base}?q={quote_plus(query)}&format=json&language=ru-RU"
            try:
                async with self.session.get(url) as response:
                    raw = await response.text(errors="ignore")
                    if response.status >= 400:
                        self.last_error = f"SearXNG HTTP {response.status}: {base}"
                        continue
                    data: dict[str, Any] = await response.json(content_type=None)
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"SearXNG error {base}: {type(exc).__name__}: {exc}"
                log.debug("%s", self.last_error)
                continue

            parsed: list[SearchResult] = []
            for item in data.get("results", []) or []:
                if not isinstance(item, dict):
                    continue
                title = self._clean_text(str(item.get("title") or ""))
                url_item = str(item.get("url") or "").strip()
                snippet = self._clean_text(str(item.get("content") or item.get("snippet") or ""))
                if title and url_item:
                    parsed.append(SearchResult(title=title, url=url_item, snippet=snippet))
                if len(parsed) >= self.config.search_max_results:
                    break
            if parsed:
                return parsed
        return []

    def _clean_duckduckgo_url(self, url: str) -> str:
        url = html.unescape(url).strip()
        if url.startswith("//"):
            url = "https:" + url
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        if "uddg" in query and query["uddg"]:
            return unquote(query["uddg"][0])
        return url

    def _clean_html(self, value: str) -> str:
        value = TAG_RE.sub(" ", value)
        return self._clean_text(value)

    def _clean_text(self, value: str) -> str:
        value = html.unescape(value or "")
        value = SPACE_RE.sub(" ", value).strip()
        return value
