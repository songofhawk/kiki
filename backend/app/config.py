"""Configuration management for Kiki V1 backend."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = REPO_ROOT / ".env"

if ENV_FILE.exists():
    load_dotenv(dotenv_path=ENV_FILE, override=False)


@dataclass(frozen=True)
class Settings:
    gemini_api_key: str
    gemini_model: str
    workspace_root: Path
    database_path: Path
    artifacts_dir: Path
    gemini_fallback_model: str = "gemini-3.1-flash-lite"
    default_heavy_agent: str = "gemini_agent"

    @property
    def api_key_configured(self) -> bool:
        key = self.gemini_api_key.strip()
        return bool(key) and key != "your_ai_studio_api_key"


def load_settings() -> Settings:
    if ENV_FILE.exists():
        load_dotenv(dotenv_path=ENV_FILE, override=False)

    workspace_root = Path(os.getenv("KIKI_WORKSPACE_ROOT", str(REPO_ROOT))).resolve()

    raw_db_path = os.getenv("KIKI_DATABASE_PATH") or os.getenv("DATABASE_PATH") or "data/kiki.db"
    db_path = Path(raw_db_path)
    if not db_path.is_absolute():
        db_path = (workspace_root / db_path).resolve()

    raw_artifacts_dir = os.getenv("KIKI_ARTIFACTS_DIR") or "data/artifacts"
    artifacts_dir = Path(raw_artifacts_dir)
    if not artifacts_dir.is_absolute():
        artifacts_dir = (workspace_root / artifacts_dir).resolve()

    return Settings(
        gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"),
        gemini_fallback_model=os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.1-flash-lite"),
        default_heavy_agent=os.getenv("KIKI_DEFAULT_HEAVY_AGENT", "gemini_agent"),
        workspace_root=workspace_root,
        database_path=db_path,
        artifacts_dir=artifacts_dir,
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
