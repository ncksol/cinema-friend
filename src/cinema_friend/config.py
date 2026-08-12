"""Application settings loaded from environment variables or a mapping."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from cinema_friend.domain.errors import InputError


@dataclass(frozen=True, slots=True)
class Settings:
    telegram_bot_token: str
    allowed_user_ids: frozenset[int]
    database_path: Path
    log_level: str = "INFO"
    bfi_impersonate_profile: str = "chrome"
    minimum_watch_interval: timedelta = timedelta(minutes=15)
    bfi_max_concurrency: int = 2
    bfi_min_spacing_seconds: float = 1.0
    bfi_cache_seconds: float = 60.0

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> Settings:
        token = values.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise InputError("TELEGRAM_BOT_TOKEN is required")
        raw_ids = values.get("TELEGRAM_ALLOWED_USER_IDS", "")
        try:
            ids = frozenset(int(v.strip()) for v in raw_ids.split(",") if v.strip())
        except ValueError as exc:
            raise InputError("TELEGRAM_ALLOWED_USER_IDS must contain integers") from exc
        if not ids:
            raise InputError("TELEGRAM_ALLOWED_USER_IDS must not be empty")
        database = values.get("DATABASE_PATH", "").strip()
        if not database:
            raise InputError("DATABASE_PATH is required")
        return cls(
            telegram_bot_token=token,
            allowed_user_ids=ids,
            database_path=Path(database).expanduser(),
            log_level=values.get("LOG_LEVEL", "INFO").upper(),
            bfi_impersonate_profile=values.get("BFI_IMPERSONATE_PROFILE", "chrome"),
        )
