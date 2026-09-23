"""The rules that must never be relaxed to make something else work."""

from __future__ import annotations

import pytest

from echoes.config import DurationPolicy, RenderPolicy, load_settings
from echoes.errors import ConfigError
from echoes.logging import redact
from echoes.providers.images.base import acceptable_licence


def test_defaults_are_safe():
    """A deployment that configures nothing must not publish anything."""
    settings = load_settings()
    assert settings.dry_run is True
    assert settings.publish_mode == "private"
    assert settings.scheduler_enabled is False


@pytest.mark.parametrize("field,value", [
    ("youtube_client_id", None),
    ("youtube_client_secret", None),
    ("youtube_refresh_token", None),
])
def test_live_run_requires_youtube_credentials(settings, field, value):
    """Leaving DRY_RUN off with missing credentials must fail by name."""
    live = settings.with_(dry_run=False, gemini_api_key="k", llm_provider="gemini",
                          youtube_client_id="a", youtube_client_secret="b",
                          youtube_refresh_token="c")
    broken = live.with_(**{field: value})
    with pytest.raises(ConfigError) as exc:
        broken.validate()
    assert field.upper() in str(exc.value)


def test_live_run_requires_an_api_key(settings):
    live = settings.with_(dry_run=False, llm_provider="gemini", gemini_api_key=None,
                          youtube_client_id="a", youtube_client_secret="b",
                          youtube_refresh_token="c")
    with pytest.raises(ConfigError, match="GEMINI_API_KEY"):
        live.validate()


def test_publish_mode_is_closed_set(settings):
    with pytest.raises(ConfigError, match="PUBLISH_MODE"):
        settings.with_(publish_mode="everyone").validate()


def test_duration_policy_must_be_ordered():
    with pytest.raises(ConfigError, match="MIN_VIDEO_MINUTES"):
        DurationPolicy(target_minutes=30, min_minutes=60, max_minutes=120).validate()


def test_plate_must_exceed_the_frame():
    """Without room to move, there is no pan -- and the renderer would crop."""
    with pytest.raises(ConfigError, match="plate"):
        RenderPolicy(plate_width=1920, plate_height=1080).validate()


@pytest.mark.parametrize("licence,allowed", [
    ("CC0", True),
    ("Public domain", True),
    ("PD-old-70", True),
    ("CC BY 4.0", True),
    ("CC BY-SA 3.0", True),
    # The trap: these all contain the substring "CC BY".
    ("CC BY-NC 4.0", False),
    ("CC BY-NC-SA 4.0", False),
    ("CC BY-ND 4.0", False),
    ("All rights reserved", False),
    ("Fair use", False),
    ("", False),
    (None, False),
])
def test_only_commercially_reusable_licences_pass(licence, allowed):
    assert acceptable_licence(licence) is allowed


def test_secrets_never_survive_a_log_line(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSyREALKEY0123456789abcdefghij")
    line = ("calling gemini with AIzaSyREALKEY0123456789abcdefghij and "
            "db postgresql://u:hunter2@host/db and bot 123456789:AAFFggHHiiJJkkLLmmNNooPPqqRRssTT")
    cleaned = redact(line)
    assert "AIzaSyREALKEY0123456789abcdefghij" not in cleaned
    assert "hunter2" not in cleaned
    assert "AAFFggHHiiJJkkLLmmNNooPPqqRRssTT" not in cleaned
