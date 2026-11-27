"""Configuration management for Kiki V1 backend."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = REPO_ROOT / ".env"


DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"
DEFAULT_GEMINI_FALLBACK_MODELS: tuple[str, ...] = (
    "gemini-3.1-flash-lite",
    "gemini-3.6-flash",
    "gemini-3.8-flash",
    "gemini-4-hydrogen",
)
DEFAULT_DEEPSEEK_MODEL = "deepseek-flash"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_ZHIPU_MODEL = "glm-4-flash"
DEFAULT_ZHIPU_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
DEFAULT_JEV_MODEL = "jev-latest"
DEFAULT_JEV_BASE_URL = "https://api.typesafe.ai/v1"
DEFAULT_CORS_ORIGINS: tuple[str, ...] = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
)


def _parse_model_list(raw: str) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for item in raw.split(","):
        name = item.strip()
        if name and name not in seen:
            seen.add(name)
            result.append(name)
    return tuple(result)


@dataclass(frozen=True)
class Settings:
    gemini_api_key: str
    gemini_model: str
    workspace_root: Path
    database_path: Path
    artifacts_dir: Path
    gemini_fallback_model: str = DEFAULT_GEMINI_FALLBACK_MODELS[0]
    gemini_fallback_models: tuple[str, ...] = DEFAULT_GEMINI_FALLBACK_MODELS
    gemini_cooldown_seconds: float = 120.0
    deepseek_api_key: str = ""
    deepseek_model: str = DEFAULT_DEEPSEEK_MODEL
    deepseek_base_url: str = DEFAULT_DEEPSEEK_BASE_URL
    zhipu_api_key: str = ""
    zhipu_model: str = DEFAULT_ZHIPU_MODEL
    zhipu_base_url: str = DEFAULT_ZHIPU_BASE_URL
    jev_api_key: str = ""
    jev_model: str = DEFAULT_JEV_MODEL
    jev_base_url: str = DEFAULT_JEV_BASE_URL
    default_heavy_agent: str = "internal_agent"
    tavily_api_key: str = ""
    serper_api_key: str = ""
    exa_api_key: str = ""
    brave_search_api_key: str = ""
    bocha_api_key: str = ""
    jina_api_key: str = ""
    serpapi_api_key: str = ""
    search_provider_order: str = "tavily,serper,exa,jina,brave,bocha,serpapi"
    cors_origins: tuple[str, ...] = DEFAULT_CORS_ORIGINS
    api_token: str = ""
    auth_username: str = ""
    auth_password_hash: str = ""
    auth_users: str = ""
    auth_secret: str = ""
    auth_session_ttl_seconds: int = 7 * 24 * 3600
    auth_cookie_secure: bool = False
    desktop_takeover_enabled: bool = True
    desktop_driver: str = "auto"
    desktop_idle_timeout_seconds: int = 900
    desktop_default_fps: int = 4
    desktop_max_width: int = 1280
    agent_max_steps: int = 25
    computer_use_enabled: bool = True
    computer_use_max_steps: int = 80
    computer_use_screenshot_width: int = 1280
    computer_use_screenshot_quality: int = 70

    @property
    def api_key_configured(self) -> bool:
        key = self.gemini_api_key.strip()
        return bool(key) and key != "your_ai_studio_api_key"

    @property
    def deepseek_configured(self) -> bool:
        key = self.deepseek_api_key.strip()
        return bool(key) and key != "your_deepseek_api_key"

    @property
    def zhipu_configured(self) -> bool:
        key = self.zhipu_api_key.strip()
        return bool(key) and key != "your_zhipu_api_key"

    @property
    def jev_configured(self) -> bool:
        key = self.jev_api_key.strip()
        return bool(key) and key != "your_jev_api_key"

    @property
    def any_llm_configured(self) -> bool:
        return (
            self.api_key_configured
            or self.deepseek_configured
            or self.zhipu_configured
            or self.jev_configured
        )

    @property
    def resolved_gemini_models(self) -> list[str]:
        """Ordered, deduplicated list of primary + fallback Gemini models."""
        candidates: list[str] = []
        seen: set[str] = set()
        raw_chain: list[str] = [self.gemini_model]
        if self.gemini_fallback_model and (
            not self.gemini_fallback_models
            or self.gemini_fallback_model not in self.gemini_fallback_models
        ):
            raw_chain.append(self.gemini_fallback_model)
        if self.gemini_fallback_models:
            raw_chain.extend(self.gemini_fallback_models)
        for item in raw_chain:
            name = item.strip()
            if name and name not in seen:
                seen.add(name)
                candidates.append(name)
        return candidates


def load_settings() -> Settings:
    if ENV_FILE.exists():
        load_dotenv(dotenv_path=ENV_FILE, override=False)

    raw_workspace = os.getenv("KIKI_WORKSPACE_ROOT", "").strip()
    if raw_workspace:
        workspace_root = Path(os.path.expandvars(os.path.expanduser(raw_workspace))).resolve()
    else:
        workspace_root = (Path.home() / ".kiki" / "workspace").expanduser().resolve()

    raw_db_path = os.getenv("KIKI_DATABASE_PATH") or os.getenv("DATABASE_PATH") or "data/kiki.db"
    db_path = Path(os.path.expandvars(os.path.expanduser(raw_db_path)))
    if not db_path.is_absolute():
        db_path = (REPO_ROOT / db_path).resolve()

    raw_artifacts_dir = os.getenv("KIKI_ARTIFACTS_DIR") or ""
    if raw_artifacts_dir.strip():
        artifacts_dir = Path(os.path.expandvars(os.path.expanduser(raw_artifacts_dir.strip())))
        if not artifacts_dir.is_absolute():
            artifacts_dir = (workspace_root / artifacts_dir).resolve()
    else:
        artifacts_dir = (workspace_root / "artifacts").resolve()

    raw_fallback_models = os.getenv("GEMINI_FALLBACK_MODELS", "").strip()
    raw_single_fallback = os.getenv("GEMINI_FALLBACK_MODEL", "").strip()
    if raw_fallback_models:
        fallback_models = _parse_model_list(raw_fallback_models)
    elif raw_single_fallback:
        fallback_models = _parse_model_list(raw_single_fallback)
    else:
        fallback_models = DEFAULT_GEMINI_FALLBACK_MODELS

    primary_fallback = fallback_models[0] if fallback_models else ""

    raw_cooldown = os.getenv("GEMINI_MODEL_COOLDOWN_SECONDS", "120").strip()
    try:
        cooldown_seconds = max(0.0, float(raw_cooldown))
    except ValueError:
        cooldown_seconds = 120.0

    raw_cors = os.getenv("KIKI_CORS_ORIGINS", "").strip()
    if raw_cors:
        cors_origins = tuple(
            origin for origin in _parse_model_list(raw_cors) if origin != "*"
        ) or DEFAULT_CORS_ORIGINS
    else:
        cors_origins = DEFAULT_CORS_ORIGINS

    raw_desktop_enabled = os.getenv("KIKI_DESKTOP_TAKEOVER_ENABLED", "true").strip().lower()
    desktop_enabled = raw_desktop_enabled not in ("0", "false", "no", "off")
    desktop_driver = os.getenv("KIKI_DESKTOP_DRIVER", "auto").strip().lower() or "auto"

    def _parse_int_env(name: str, default: int, minimum: int, maximum: int) -> int:
        raw_val = os.getenv(name, "").strip()
        if not raw_val:
            return default
        try:
            return max(minimum, min(maximum, int(raw_val)))
        except ValueError:
            return default

    desktop_idle_timeout = _parse_int_env("KIKI_DESKTOP_IDLE_TIMEOUT_SECONDS", 900, 30, 7200)
    desktop_default_fps = _parse_int_env("KIKI_DESKTOP_DEFAULT_FPS", 4, 1, 15)
    desktop_max_width = _parse_int_env("KIKI_DESKTOP_MAX_WIDTH", 1280, 640, 2560)
    raw_computer_use_enabled = os.getenv("KIKI_COMPUTER_USE_ENABLED", "true").strip().lower()
    computer_use_enabled = raw_computer_use_enabled not in ("0", "false", "no", "off")
    agent_max_steps = _parse_int_env("KIKI_AGENT_MAX_STEPS", 25, 10, 100)
    computer_use_max_steps = _parse_int_env("KIKI_COMPUTER_USE_MAX_STEPS", 80, 15, 200)
    computer_use_screenshot_width = _parse_int_env("KIKI_COMPUTER_USE_SCREENSHOT_WIDTH", 1280, 640, 1920)
    computer_use_screenshot_quality = _parse_int_env("KIKI_COMPUTER_USE_SCREENSHOT_QUALITY", 70, 35, 95)
    auth_session_ttl = _parse_int_env("KIKI_AUTH_SESSION_TTL_SECONDS", 7 * 24 * 3600, 300, 90 * 24 * 3600)
    raw_auth_cookie_secure = os.getenv("KIKI_AUTH_COOKIE_SECURE", "false").strip().lower()
    auth_cookie_secure = raw_auth_cookie_secure in ("1", "true", "yes", "on")

    return Settings(
        gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
        gemini_model=os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL),
        gemini_fallback_model=primary_fallback,
        gemini_fallback_models=fallback_models,
        gemini_cooldown_seconds=cooldown_seconds,
        deepseek_api_key=os.getenv("DEEPSEEK_API_KEY", ""),
        deepseek_model=os.getenv("DEEPSEEK_MODEL", DEFAULT_DEEPSEEK_MODEL),
        deepseek_base_url=os.getenv("DEEPSEEK_BASE_URL", DEFAULT_DEEPSEEK_BASE_URL).rstrip("/"),
        zhipu_api_key=os.getenv("ZHIPU_API_KEY", ""),
        zhipu_model=os.getenv("ZHIPU_MODEL", DEFAULT_ZHIPU_MODEL),
        zhipu_base_url=os.getenv("ZHIPU_BASE_URL", DEFAULT_ZHIPU_BASE_URL).rstrip("/"),
        jev_api_key=os.getenv("JEV_KEY") or os.getenv("JEV_API_KEY", ""),
        jev_model=os.getenv("JEV_MODEL", DEFAULT_JEV_MODEL),
        jev_base_url=os.getenv("JEV_BASE_URL", DEFAULT_JEV_BASE_URL).rstrip("/"),
        default_heavy_agent=os.getenv("KIKI_DEFAULT_HEAVY_AGENT", "internal_agent"),
        workspace_root=workspace_root,
        database_path=db_path,
        artifacts_dir=artifacts_dir,
        tavily_api_key=os.getenv("TAVILY_API_KEY", ""),
        serper_api_key=os.getenv("SERPER_API_KEY", ""),
        exa_api_key=os.getenv("EXA_API_KEY", ""),
        brave_search_api_key=os.getenv("BRAVE_SEARCH_API_KEY", ""),
        bocha_api_key=os.getenv("BOCHA_API_KEY", ""),
        jina_api_key=os.getenv("JINA_API_KEY", ""),
        serpapi_api_key=os.getenv("SERPAPI_API_KEY", ""),
        search_provider_order=os.getenv(
            "KIKI_SEARCH_PROVIDER_ORDER",
            "tavily,serper,exa,jina,brave,bocha,serpapi",
        ),
        cors_origins=cors_origins,
        api_token=os.getenv("KIKI_API_TOKEN", "").strip(),
        auth_username=os.getenv("KIKI_AUTH_USERNAME", "").strip(),
        auth_password_hash=os.getenv("KIKI_AUTH_PASSWORD_HASH", "").strip(),
        auth_users=os.getenv("KIKI_AUTH_USERS", "").strip(),
        auth_secret=os.getenv("KIKI_AUTH_SECRET", "").strip(),
        auth_session_ttl_seconds=auth_session_ttl,
        auth_cookie_secure=auth_cookie_secure,
        desktop_takeover_enabled=desktop_enabled,
        desktop_driver=desktop_driver,
        desktop_idle_timeout_seconds=desktop_idle_timeout,
        desktop_default_fps=desktop_default_fps,
        desktop_max_width=desktop_max_width,
        agent_max_steps=agent_max_steps,
        computer_use_enabled=computer_use_enabled,
        computer_use_max_steps=computer_use_max_steps,
        computer_use_screenshot_width=computer_use_screenshot_width,
        computer_use_screenshot_quality=computer_use_screenshot_quality,
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
