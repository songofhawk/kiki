"""Multi-provider web search and safe webpage fetch service for Kiki."""

from __future__ import annotations

import asyncio
import html
import ipaddress
import logging
import re
import socket
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlparse

import httpx

from .config import Settings

logger = logging.getLogger(__name__)

DEFAULT_PROVIDER_ORDER: tuple[str, ...] = (
    "tavily",
    "serper",
    "exa",
    "jina",
    "brave",
    "bocha",
    "serpapi",
)

PROVIDER_LABELS: dict[str, str] = {
    "tavily": "Tavily",
    "serper": "Serper (Google)",
    "exa": "Exa",
    "brave": "Brave Search",
    "bocha": "Bocha (博查)",
    "jina": "Jina Search",
    "serpapi": "SerpApi",
}

_PLACEHOLDER_PREFIXES = ("your_", "replace_", "example_", "sk-xxx", "tvly-xxx")


def _is_configured_key(value: Optional[str]) -> bool:
    if not value:
        return False
    cleaned = value.strip()
    if not cleaned:
        return False
    lower = cleaned.lower()
    return not any(lower.startswith(prefix) for prefix in _PLACEHOLDER_PREFIXES)


def _strip_html(raw_html: str) -> str:
    text = re.sub(r"(?is)<(script|style|noscript|svg).*?>.*?</\1>", " ", raw_html)
    text = re.sub(r"(?i)</(p|div|li|tr|h[1-6]|br|section|article)>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


_PROXY_FAKE_IP_NET = ipaddress.ip_network("198.18.0.0/15")


def _is_disallowed_ip(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address, *, allow_fake_ip: bool) -> bool:
    if allow_fake_ip and ip_obj.version == 4 and ip_obj in _PROXY_FAKE_IP_NET:
        return False
    return (
        ip_obj.is_private
        or ip_obj.is_loopback
        or ip_obj.is_link_local
        or ip_obj.is_multicast
        or ip_obj.is_reserved
        or ip_obj.is_unspecified
    )


def _is_public_http_url(url: str) -> tuple[bool, str]:
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False, "仅支持 http:// 或 https:// 公网 URL。"

    host = parsed.hostname.lower()
    if host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"} or host.endswith(".local"):
        return False, "安全策略禁止访问本地或内网地址。"

    try:
        ip_obj = ipaddress.ip_address(host)
        if _is_disallowed_ip(ip_obj, allow_fake_ip=False):
            return False, "安全策略禁止访问私有或保留网段地址。"
        return True, ""
    except ValueError:
        pass

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        # Let httpx handle DNS errors cleanly during request execution
        return True, ""

    for info in infos:
        addr = info[4][0]
        try:
            ip_obj = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if _is_disallowed_ip(ip_obj, allow_fake_ip=True):
            return False, "安全策略禁止访问解析到内网或保留地址的域名。"
    return True, ""


@dataclass(frozen=True)
class SearchResultItem:
    title: str
    url: str
    snippet: str
    score: Optional[float] = None
    published_date: Optional[str] = None


@dataclass
class SearchResponse:
    query: str
    provider: str
    results: list[SearchResultItem] = field(default_factory=list)
    answer: Optional[str] = None
    fallback_errors: list[str] = field(default_factory=list)

    def format_markdown(self) -> str:
        label = PROVIDER_LABELS.get(self.provider, self.provider)
        lines: list[str] = [f"搜索结果（引擎：{label}，关键词：{self.query}）："]
        if self.answer:
            lines.append(f"\n摘要参考：{self.answer.strip()}")
        if not self.results:
            lines.append("\n（未找到匹配的网页结果）")
        for idx, item in enumerate(self.results, start=1):
            lines.append(f"\n{idx}. **{item.title or '无标题'}**")
            if item.url:
                lines.append(f"   URL: {item.url}")
            if item.published_date:
                lines.append(f"   发布时间: {item.published_date}")
            if item.snippet:
                lines.append(f"   摘要: {item.snippet}")
        if self.fallback_errors:
            lines.append("\n[降级记录] " + "；".join(self.fallback_errors))
        return "\n".join(lines)


class WebSearchService:
    """Executes web searches across multiple configured API providers with automatic failover."""

    def __init__(
        self,
        settings: Settings,
        *,
        timeout_sec: float = 30.0,
        client_factory: Optional[Callable[[], httpx.AsyncClient]] = None,
    ) -> None:
        self.settings = settings
        self.timeout_sec = timeout_sec
        self._client_factory = client_factory

    def _provider_keys(self) -> dict[str, str]:
        return {
            "tavily": self.settings.tavily_api_key.strip(),
            "serper": self.settings.serper_api_key.strip(),
            "exa": self.settings.exa_api_key.strip(),
            "brave": self.settings.brave_search_api_key.strip(),
            "bocha": self.settings.bocha_api_key.strip(),
            "jina": self.settings.jina_api_key.strip(),
            "serpapi": self.settings.serpapi_api_key.strip(),
        }

    def provider_order(self) -> list[str]:
        raw = getattr(self.settings, "search_provider_order", "") or ""
        custom = [p.strip().lower() for p in raw.split(",") if p.strip()]
        ordered: list[str] = []
        for name in custom + list(DEFAULT_PROVIDER_ORDER):
            if name in PROVIDER_LABELS and name not in ordered:
                ordered.append(name)
        return ordered

    def configured_providers(self) -> list[str]:
        keys = self._provider_keys()
        return [name for name in self.provider_order() if _is_configured_key(keys.get(name))]

    def status(self) -> dict[str, Any]:
        configured = self.configured_providers()
        all_order = self.provider_order()
        return {
            "configured_providers": configured,
            "configured_labels": [PROVIDER_LABELS[p] for p in configured],
            "available": bool(configured),
            "provider_order": all_order,
            "missing_providers": [p for p in all_order if p not in configured],
        }

    def _make_client(self) -> httpx.AsyncClient:
        if self._client_factory is not None:
            return self._client_factory()
        return httpx.AsyncClient(timeout=self.timeout_sec, follow_redirects=True)

    async def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        provider: Optional[str] = None,
        search_depth: str = "basic",
    ) -> SearchResponse:
        clean_query = (query or "").strip()
        if not clean_query:
            raise ValueError("搜索关键词 query 不能为空。")

        capped_results = max(1, min(int(max_results or 5), 10))
        depth = "advanced" if search_depth == "advanced" else "basic"

        keys = self._provider_keys()
        configured = self.configured_providers()
        if not configured:
            raise RuntimeError(
                "尚未配置任何搜索 API Key。请在根目录 .env 中至少配置以下之一："
                "TAVILY_API_KEY, SERPER_API_KEY, EXA_API_KEY, BRAVE_SEARCH_API_KEY, "
                "BOCHA_API_KEY, JINA_API_KEY, SERPAPI_API_KEY。"
            )

        requested = (provider or "auto").strip().lower()
        if requested and requested != "auto":
            if requested not in PROVIDER_LABELS:
                raise ValueError(f"不支持的搜索引擎 '{provider}'。")
            if requested not in configured:
                raise RuntimeError(f"搜索引擎 '{requested}' 尚未配置 API Key。")
            candidates = [requested]
        else:
            candidates = configured

        dispatch: dict[
            str,
            Callable[[httpx.AsyncClient, str, str, int, str], Awaitable[SearchResponse]],
        ] = {
            "tavily": self._search_tavily,
            "serper": self._search_serper,
            "exa": self._search_exa,
            "brave": self._search_brave,
            "bocha": self._search_bocha,
            "jina": self._search_jina,
            "serpapi": self._search_serpapi,
        }

        fallback_errors: list[str] = []
        async with self._make_client() as client:
            for name in candidates:
                api_key = keys[name]
                try:
                    resp = await dispatch[name](client, api_key, clean_query, capped_results, depth)
                    resp.fallback_errors = list(fallback_errors)
                    return resp
                except Exception as exc:
                    detail = str(exc).strip() or type(exc).__name__
                    msg = f"{name}: {detail}"
                    fallback_errors.append(msg)
                    logger.warning("Web search provider %s failed: %s", name, detail)

        raise RuntimeError("所有已配置的搜索引擎均调用失败：" + "；".join(fallback_errors))

    async def fetch_webpage(self, url: str, *, max_chars: int = 6000) -> str:
        target = (url or "").strip()
        allowed, reason = await asyncio.to_thread(_is_public_http_url, target)
        if not allowed:
            raise PermissionError(reason)

        capped = max(500, min(int(max_chars or 6000), 20000))
        keys = self._provider_keys()
        jina_key = keys.get("jina", "")

        async with self._make_client() as client:
            # Prefer Jina Reader when JINA_API_KEY is configured for clean markdown extraction
            if _is_configured_key(jina_key):
                try:
                    resp = await client.get(
                        f"https://r.jina.ai/{target}",
                        headers={
                            "Authorization": f"Bearer {jina_key}",
                            "Accept": "text/plain",
                        },
                    )
                    resp.raise_for_status()
                    text = resp.text.strip()
                    if text:
                        return text[:capped] + ("\n... (truncated)" if len(text) > capped else "")
                except Exception as exc:
                    logger.warning("Jina Reader failed for %s (%s), falling back to direct fetch", target, exc)

            resp = await client.get(
                target,
                headers={
                    "User-Agent": "KikiPersonalAgent/1.0 (+https://localhost)",
                    "Accept": "text/html,application/xhtml+xml,text/plain,application/json",
                },
            )
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "").lower()
            raw_text = resp.text
            if "html" in content_type or "<html" in raw_text[:400].lower():
                cleaned = _strip_html(raw_text)
            else:
                cleaned = raw_text.strip()

        if not cleaned:
            return "(网页返回空内容)"
        if len(cleaned) > capped:
            return cleaned[:capped] + "\n... (truncated)"
        return cleaned

    async def _search_tavily(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        query: str,
        max_results: int,
        depth: str,
    ) -> SearchResponse:
        resp = await client.post(
            "https://api.tavily.com/search",
            json={
                "api_key": api_key,
                "query": query,
                "max_results": max_results,
                "search_depth": depth,
                "include_answer": False,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        items = [
            SearchResultItem(
                title=str(r.get("title") or ""),
                url=str(r.get("url") or ""),
                snippet=str(r.get("content") or r.get("snippet") or "")[:600],
                score=r.get("score"),
                published_date=r.get("published_date"),
            )
            for r in (data.get("results") or [])[:max_results]
        ]
        return SearchResponse(query=query, provider="tavily", results=items, answer=data.get("answer"))

    async def _search_serper(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        query: str,
        max_results: int,
        _depth: str,
    ) -> SearchResponse:
        resp = await client.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": query, "num": max_results},
        )
        resp.raise_for_status()
        data = resp.json()
        answer_box = data.get("answerBox") or {}
        answer = answer_box.get("answer") or answer_box.get("snippet")
        items = [
            SearchResultItem(
                title=str(r.get("title") or ""),
                url=str(r.get("link") or ""),
                snippet=str(r.get("snippet") or "")[:600],
                published_date=r.get("date"),
            )
            for r in (data.get("organic") or [])[:max_results]
        ]
        return SearchResponse(query=query, provider="serper", results=items, answer=answer)

    async def _search_exa(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        query: str,
        max_results: int,
        depth: str,
    ) -> SearchResponse:
        resp = await client.post(
            "https://api.exa.ai/search",
            headers={"x-api-key": api_key, "Content-Type": "application/json"},
            json={
                "query": query,
                "numResults": max_results,
                "type": "auto" if depth == "basic" else "deep",
                "contents": {"text": {"maxCharacters": 600}},
            },
        )
        resp.raise_for_status()
        data = resp.json()
        items = [
            SearchResultItem(
                title=str(r.get("title") or ""),
                url=str(r.get("url") or ""),
                snippet=str(r.get("text") or r.get("summary") or "")[:600],
                score=r.get("score"),
                published_date=r.get("publishedDate"),
            )
            for r in (data.get("results") or [])[:max_results]
        ]
        return SearchResponse(query=query, provider="exa", results=items)

    async def _search_brave(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        query: str,
        max_results: int,
        _depth: str,
    ) -> SearchResponse:
        resp = await client.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers={
                "X-Subscription-Token": api_key,
                "Accept": "application/json",
            },
            params={"q": query, "count": max_results},
        )
        resp.raise_for_status()
        data = resp.json()
        web_items = (data.get("web") or {}).get("results") or []
        items = [
            SearchResultItem(
                title=str(r.get("title") or ""),
                url=str(r.get("url") or ""),
                snippet=str(r.get("description") or "")[:600],
                published_date=r.get("age"),
            )
            for r in web_items[:max_results]
        ]
        return SearchResponse(query=query, provider="brave", results=items)

    async def _search_bocha(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        query: str,
        max_results: int,
        _depth: str,
    ) -> SearchResponse:
        resp = await client.post(
            "https://api.bochaai.com/v1/web-search",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={"query": query, "count": max_results, "summary": True},
        )
        resp.raise_for_status()
        payload = resp.json()
        data = payload.get("data") or payload
        web_items = (data.get("webPages") or {}).get("value") or []
        items = [
            SearchResultItem(
                title=str(r.get("name") or r.get("title") or ""),
                url=str(r.get("url") or ""),
                snippet=str(r.get("summary") or r.get("snippet") or "")[:600],
                published_date=r.get("datePublished"),
            )
            for r in web_items[:max_results]
        ]
        return SearchResponse(query=query, provider="bocha", results=items)

    async def _search_jina(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        query: str,
        max_results: int,
        _depth: str,
    ) -> SearchResponse:
        resp = await client.get(
            "https://s.jina.ai/",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
            },
            params={"q": query},
        )
        resp.raise_for_status()
        data = resp.json()
        raw_items = data.get("data") or []
        items = [
            SearchResultItem(
                title=str(r.get("title") or ""),
                url=str(r.get("url") or ""),
                snippet=str(r.get("description") or r.get("content") or "")[:600],
            )
            for r in raw_items[:max_results]
        ]
        return SearchResponse(query=query, provider="jina", results=items)

    async def _search_serpapi(
        self,
        client: httpx.AsyncClient,
        api_key: str,
        query: str,
        max_results: int,
        _depth: str,
    ) -> SearchResponse:
        resp = await client.get(
            "https://serpapi.com/search.json",
            params={"q": query, "api_key": api_key, "num": max_results},
        )
        resp.raise_for_status()
        data = resp.json()
        answer_box = data.get("answer_box") or {}
        answer = answer_box.get("answer") or answer_box.get("snippet")
        items = [
            SearchResultItem(
                title=str(r.get("title") or ""),
                url=str(r.get("link") or ""),
                snippet=str(r.get("snippet") or "")[:600],
                published_date=r.get("date"),
            )
            for r in (data.get("organic_results") or [])[:max_results]
        ]
        return SearchResponse(query=query, provider="serpapi", results=items, answer=answer)
