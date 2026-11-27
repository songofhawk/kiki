"""Tests for static-config login authentication, password hashing, and session cookies."""

from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.auth import (
    AuthManager,
    SESSION_COOKIE_NAME,
    hash_password,
    is_valid_password_hash,
    verify_password,
)
from app.config import Settings
from app.db import Database
from app.desktop import MockDesktopDriver
from app.main import create_app


def _make_settings(tmp_path: Path, **overrides) -> Settings:
    workspace = tmp_path / "workspace"
    artifacts = workspace / "artifacts"
    db_path = tmp_path / "test.db"
    defaults = dict(
        gemini_api_key="",
        gemini_model="gemini-3.5-flash-lite",
        workspace_root=workspace,
        database_path=db_path,
        artifacts_dir=artifacts,
        desktop_driver="mock",
    )
    defaults.update(overrides)
    return Settings(**defaults)


def test_password_hash_and_verify() -> None:
    h1 = hash_password("S3cur3!Pass")
    h2 = hash_password("S3cur3!Pass")

    assert h1 != h2  # Random salt per hash
    assert "$" not in h1  # Safe in .env without variable interpolation
    assert is_valid_password_hash(h1)
    assert verify_password("S3cur3!Pass", h1) is True
    assert verify_password("wrong-pass", h1) is False
    assert verify_password("S3cur3!Pass", "not-a-valid-hash") is False
    assert verify_password("", h1) is False

    with pytest.raises(ValueError):
        hash_password("")


def test_session_token_invalidated_on_password_change_and_expiry() -> None:
    h1 = hash_password("first-pass")
    mgr1 = AuthManager(username="alice", password_hash=h1, session_ttl_seconds=3600)
    token = mgr1.create_session_token("alice")
    assert mgr1.verify_session_token(token) == "alice"

    # Tampered token rejected
    assert mgr1.verify_session_token(token + "x") is None
    assert mgr1.verify_session_token("malformed") is None

    # Password changed -> old token immediately rejected
    h2 = hash_password("second-pass")
    mgr2 = AuthManager(username="alice", password_hash=h2, session_ttl_seconds=3600)
    assert mgr2.verify_session_token(token) is None

    # Expired token rejected
    mgr_short = AuthManager(username="alice", password_hash=h1, session_ttl_seconds=300)
    expired_token = mgr_short.create_session_token("alice")
    # Simulate time past expiry
    orig_time = time.time
    try:
        time.time = lambda: orig_time() + 1000  # type: ignore[assignment]
        assert mgr_short.verify_session_token(expired_token) is None
    finally:
        time.time = orig_time  # type: ignore[assignment]


@pytest.mark.asyncio
async def test_auth_disabled_by_default(tmp_path: Path) -> None:
    cfg = _make_settings(tmp_path)
    app = create_app(settings=cfg, db=Database(cfg.database_path))

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            me = await client.get("/api/auth/me")
            assert me.status_code == 200
            assert me.json() == {
                "auth_enabled": False,
                "authenticated": True,
                "username": None,
            }
            topics = await client.get("/api/topics")
            assert topics.status_code == 200


@pytest.mark.asyncio
async def test_login_logout_and_cookie_protection(tmp_path: Path) -> None:
    pw_hash = hash_password("my-secret-password")
    cfg = _make_settings(
        tmp_path,
        auth_username="admin",
        auth_password_hash=pw_hash,
    )
    app = create_app(settings=cfg, db=Database(cfg.database_path))

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # Public endpoints remain accessible
            health = await client.get("/api/health")
            assert health.status_code == 200

            me_before = await client.get("/api/auth/me")
            assert me_before.status_code == 200
            assert me_before.json() == {
                "auth_enabled": True,
                "authenticated": False,
                "username": None,
            }

            # Protected endpoints reject unauthenticated requests
            assert (await client.get("/api/topics")).status_code == 401
            assert (await client.get("/api/messages")).status_code == 401

            # Wrong password rejected
            bad = await client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "wrong"},
            )
            assert bad.status_code == 401
            assert SESSION_COOKIE_NAME not in bad.cookies

            # Correct login sets HttpOnly cookie
            ok = await client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "my-secret-password"},
            )
            assert ok.status_code == 200
            assert ok.json() == {
                "auth_enabled": True,
                "authenticated": True,
                "username": "admin",
            }
            set_cookie = ok.headers.get("set-cookie", "").lower()
            assert f"{SESSION_COOKIE_NAME}=" in set_cookie
            assert "httponly" in set_cookie
            assert "samesite=lax" in set_cookie

            # Authenticated requests succeed
            me_after = await client.get("/api/auth/me")
            assert me_after.json() == {
                "auth_enabled": True,
                "authenticated": True,
                "username": "admin",
            }
            topics = await client.get("/api/topics")
            assert topics.status_code == 200

            # Logout clears session
            out = await client.post("/api/auth/logout")
            assert out.status_code == 200
            assert (await client.get("/api/topics")).status_code == 401
            me_logged_out = await client.get("/api/auth/me")
            assert me_logged_out.json()["authenticated"] is False


@pytest.mark.asyncio
async def test_multi_user_and_api_token_coexistence(tmp_path: Path) -> None:
    h_alice = hash_password("alice-pass")
    h_bob = hash_password("bob-pass")
    cfg = _make_settings(
        tmp_path,
        auth_users=f"alice={h_alice};bob={h_bob}",
        api_token="static-cli-token",
    )
    app = create_app(settings=cfg, db=Database(cfg.database_path))

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # Unauthenticated fails
            assert (await client.get("/api/topics")).status_code == 401

            # Bearer API token still works for programmatic access
            res_token = await client.get(
                "/api/topics",
                headers={"Authorization": "Bearer static-cli-token"},
            )
            assert res_token.status_code == 200

            # Bob can log in via multi-user config
            res_bob = await client.post(
                "/api/auth/login",
                json={"username": "bob", "password": "bob-pass"},
            )
            assert res_bob.status_code == 200
            assert res_bob.json()["username"] == "bob"


@pytest.mark.asyncio
async def test_misconfigured_auth_fails_closed(tmp_path: Path) -> None:
    cfg = _make_settings(
        tmp_path,
        auth_username="admin",
        auth_password_hash="plaintext_not_allowed",
    )
    app = create_app(settings=cfg, db=Database(cfg.database_path))

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/api/topics")).status_code == 401
            res = await client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "plaintext_not_allowed"},
            )
            assert res.status_code == 401


@pytest.mark.asyncio
async def test_login_rate_limiting(tmp_path: Path) -> None:
    cfg = _make_settings(
        tmp_path,
        auth_username="admin",
        auth_password_hash=hash_password("correct-pass"),
    )
    app = create_app(settings=cfg, db=Database(cfg.database_path))
    app.state.auth_manager._max_failed_attempts = 3
    app.state.auth_manager._lockout_seconds = 30

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            for _ in range(3):
                r = await client.post(
                    "/api/auth/login",
                    json={"username": "admin", "password": "wrong"},
                )
                assert r.status_code == 401
            # 4th attempt is locked out
            locked = await client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "correct-pass"},
            )
            assert locked.status_code == 429


def test_desktop_websocket_requires_session_cookie(tmp_path: Path) -> None:
    cfg = _make_settings(
        tmp_path,
        auth_username="admin",
        auth_password_hash=hash_password("ws-secret"),
    )
    app = create_app(
        settings=cfg,
        db=Database(cfg.database_path),
        desktop_driver=MockDesktopDriver(),
    )
    with TestClient(app) as client:
        # Login first to create a desktop session
        r_login = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "ws-secret"},
        )
        assert r_login.status_code == 200
        r_sess = client.post("/api/desktop/sessions", json={})
        assert r_sess.status_code == 200
        sess = r_sess.json()["session"]
        sess_id = sess["id"]
        ws_token = sess["ws_token"]

        # Authenticated WS connection succeeds
        with client.websocket_connect(
            f"/api/desktop/sessions/{sess_id}/ws?token={ws_token}"
        ) as ws:
            hello = ws.receive_json()
            assert hello["type"] == "session"

        # Clear cookies -> WS connection rejected with 4401
        client.cookies.clear()
        with pytest.raises(Exception):
            with client.websocket_connect(
                f"/api/desktop/sessions/{sess_id}/ws?token={ws_token}"
            ) as ws:
                ws.receive_json()
