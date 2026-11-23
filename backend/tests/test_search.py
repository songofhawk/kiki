"""Unit and API tests for Kiki multi-provider web search and webpage fetch tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from app.config import Settings, load_settings
from app.db import Database
from app.main import create_app
from app.search import WebSearchService
from app.sse import EventBus
from app.tools import WorkspaceToolExecutor


def _make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "gemini_api_key": "test-gemini",
        "gemini_model": "gemini-3.5-flash-lite",
        "workspace_root": tmp_path,
        "database_path": tmp_path / "kiki.db",
        "artifacts_dir": tmp_path / "artifacts",
    }
    defaults.update(overrides)
    return Settings(**defaults)


def test_search_status_excludes_placeholders(tmp_path: Path) -> None:
    cfg = _make_settings(
        tmp_path,
        tavily_api_key="tvly-xxx",
        serper_api_key="real-serper-key",
        exa_api_key="your_exa_api_key",
        bocha_api_key="sk-bocha-real",
        search_provider_order="bocha,serper,tavily",
    )
    svc = WebSearchService(cfg)
    status = svc.status()

    assert status["available"] is True
    assert status["configured_providers"] == ["bocha", "serper"]
    assert "tavily" in status["missing_providers"]
    assert "exa" in status["missing_providers"]


@pytest.mark.asyncio
async def test_search_automatic_failover_and_format(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "api.tavily.com" in str(request.url):
            return httpx.Response(429, json={"error": "rate limited"}, request=request)
        if "google.serper.dev" in str(request.url):
            assert request.headers.get("X-API-KEY") == "serper-secret"
            return httpx.Response(
                200,
                json={
                    "answerBox": {"snippet": "Kiki is a personal AI agent."},
                    "organic": [
                        {
                            "title": "Kiki Agent Docs",
                            "link": "https://example.com/kiki",
                            "snippet": "Personal agent documentation.",
                            "date": "2026-11-20",
                        }
                    ],
                },
                request=request,
            )
        raise AssertionError(f"Unexpected URL: {request.url}")

    transport = httpx.MockTransport(handler)
    cfg = _make_settings(
        tmp_path,
        tavily_api_key="tvly-real",
        serper_api_key="serper-secret",
    )
    svc = WebSearchService(cfg, client_factory=lambda: httpx.AsyncClient(transport=transport))
    resp = await svc.search("Kiki agent", max_results=3)

    assert len(calls) == 2
    assert resp.provider == "serper"
    assert len(resp.fallback_errors) == 1
    assert "tavily:" in resp.fallback_errors[0]
    assert len(resp.results) == 1
    assert resp.results[0].url == "https://example.com/kiki"

    md = resp.format_markdown()
    assert "Serper (Google)" in md
    assert "Kiki Agent Docs" in md
    assert "https://example.com/kiki" in md


@pytest.mark.asyncio
async def test_search_bocha_and_exa_and_brave(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "api.bochaai.com" in url:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "webPages": {
                            "value": [
                                {
                                    "name": "博查结果",
                                    "url": "https://bocha.example.com/1",
                                    "summary": "中文检索摘要",
                                    "datePublished": "2026-11-21",
                                }
                            ]
                        }
                    }
                },
                request=request,
            )
        if "api.exa.ai" in url:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "title": "Exa Result",
                            "url": "https://exa.example.com/1",
                            "text": "Semantic search snippet",
                        }
                    ]
                },
                request=request,
            )
        if "api.search.brave.com" in url:
            return httpx.Response(
                200,
                json={
                    "web": {
                        "results": [
                            {
                                "title": "Brave Result",
                                "url": "https://brave.example.com/1",
                                "description": "Brave snippet",
                            }
                        ]
                    }
                },
                request=request,
            )
        raise AssertionError(f"Unexpected URL: {url}")

    transport = httpx.MockTransport(handler)
    cfg = _make_settings(
        tmp_path,
        bocha_api_key="bocha-key",
        exa_api_key="exa-key",
        brave_search_api_key="brave-key",
    )
    svc = WebSearchService(cfg, client_factory=lambda: httpx.AsyncClient(transport=transport))

    r_bocha = await svc.search("测试", provider="bocha")
    assert r_bocha.results[0].title == "博查结果"

    r_exa = await svc.search("test", provider="exa")
    assert r_exa.results[0].title == "Exa Result"

    r_brave = await svc.search("test", provider="brave")
    assert r_brave.results[0].title == "Brave Result"


@pytest.mark.asyncio
async def test_fetch_webpage_blocks_private_addresses_and_extracts_html(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text="<html><head><style>body{}</style></head><body><h1>Hello Kiki</h1><p>Content &amp; details.</p></body></html>",
            request=request,
        )

    transport = httpx.MockTransport(handler)
    cfg = _make_settings(tmp_path)
    svc = WebSearchService(cfg, client_factory=lambda: httpx.AsyncClient(transport=transport))

    with pytest.raises(PermissionError):
        await svc.fetch_webpage("http://127.0.0.1:8000/secret")

    with pytest.raises(PermissionError):
        await svc.fetch_webpage("http://192.168.1.10/admin")

    with pytest.raises(PermissionError):
        await svc.fetch_webpage("http://198.18.0.1/internal")

    text = await svc.fetch_webpage("https://93.184.216.34/page")
    assert "Hello Kiki" in text
    assert "Content & details." in text
    assert "body{}" not in text


@pytest.mark.asyncio
async def test_tool_executor_and_api_search_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-live-mock")
    monkeypatch.setenv("SERPER_API_KEY", "serper-live-mock")
    monkeypatch.setenv("KIKI_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("KIKI_DATABASE_PATH", str(tmp_path / "api.db"))
    monkeypatch.setenv("KIKI_ARTIFACTS_DIR", str(tmp_path / "artifacts"))

    cfg = load_settings()
    assert cfg.tavily_api_key == "tvly-live-mock"
    assert cfg.serper_api_key == "serper-live-mock"

    db = Database(cfg.database_path)
    db.init_db()
    bus = EventBus()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {"title": "Tavily Hit", "url": "https://tavily.com/hit", "content": "Hit content"}
                ]
            },
            request=request,
        )

    svc = WebSearchService(
        cfg,
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    executor = WorkspaceToolExecutor(
        workspace_root=tmp_path,
        artifacts_dir=tmp_path / "artifacts",
        db=db,
        event_bus=bus,
        search_service=svc,
    )
    out, is_err = await executor.execute("run_1", "web_search", {"query": "kiki test"})
    assert is_err is False
    assert "Tavily Hit" in out

    app = create_app(settings=cfg, db=db, event_bus=bus)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        res = await client.get("/api/search/status")
        assert res.status_code == 200
        payload = res.json()
        assert payload["available"] is True
        assert "tavily" in payload["configured_providers"]
        assert "serper" in payload["configured_providers"]
