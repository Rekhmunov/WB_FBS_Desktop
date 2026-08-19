from __future__ import annotations

import os
from dataclasses import dataclass


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


@dataclass(frozen=True)
class AppConfig:
    app_env: str
    db_url: str | None
    self_registration_enabled: bool
    # When False: skip marketplace chat sync and hide chat UI refresh.
    # Reviews and questions are unaffected. Re-enable via FEEDPILOT_SYNC_CHATS_ENABLED=1.
    sync_chats_enabled: bool

    @property
    def is_production(self) -> bool:
        return self.app_env.strip().lower() == "production"


def sync_chats_enabled() -> bool:
    """Runtime check for chat channel sync (default off)."""
    return _env_bool("FEEDPILOT_SYNC_CHATS_ENABLED", False)


def load_app_config() -> AppConfig:
    app_env = (os.getenv("APP_ENV") or "development").strip().lower() or "development"
    db_url_raw = (os.getenv("APP_DB_URL") or "").strip()
    db_url = db_url_raw or None
    # PostgreSQL is required in all environments except the test runner
    # (tests pass db_path directly to ReviewRepository and never call
    # load_app_config, so this guard does not break them).
    if not db_url and os.getenv("FEEDPILOT_TEST_MODE") != "1":
        raise RuntimeError(
            "APP_DB_URL is required. Set it to a PostgreSQL DSN "
            "(postgresql://user:password@host:5432/dbname)."
        )
    self_registration_enabled = _env_bool("APP_SELF_REGISTRATION_ENABLED", False)
    return AppConfig(
        app_env=app_env,
        db_url=db_url,
        self_registration_enabled=self_registration_enabled,
        sync_chats_enabled=sync_chats_enabled(),
    )
