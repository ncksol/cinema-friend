"""Configuration tests."""

import pytest

from cinema_friend.config import Settings
from cinema_friend.domain.errors import InputError


def test_settings_parse_allow_list_and_defaults(tmp_path):
    settings = Settings.from_mapping({
        "TELEGRAM_BOT_TOKEN": "token",
        "TELEGRAM_ALLOWED_USER_IDS": "11, 22",
        "DATABASE_PATH": str(tmp_path / "cinema.db"),
    })
    assert settings.allowed_user_ids == frozenset({11, 22})
    assert settings.bfi_impersonate_profile == "chrome"
    assert settings.minimum_watch_interval.total_seconds() == 900


def test_settings_reject_missing_token(tmp_path):
    with pytest.raises(InputError, match="TELEGRAM_BOT_TOKEN"):
        Settings.from_mapping({
            "TELEGRAM_ALLOWED_USER_IDS": "11",
            "DATABASE_PATH": str(tmp_path / "cinema.db"),
        })
