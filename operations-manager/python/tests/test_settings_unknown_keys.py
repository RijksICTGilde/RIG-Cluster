"""An unknown configuration key must be reported, not fatal.

pydantic-settings forbids extras by default, which means an OPI image refuses to start on
a config file mentioning a setting newer than itself. That is exactly what a rollback or
an upgrade test does, and it cost the upgrade-safety run twice: the baseline image
crash-looped on SLEEP_MODE_*, the operator stripped those lines from the live ConfigMap by
hand, and on putting them back one line was missed (KEYCLOAK_ENFORCE_ADMIN_OTP) so the new
side ran without OTP and could prove nothing about it.
"""

from __future__ import annotations

import logging

from opi.core.config import Settings


def test_a_setting_this_build_does_not_know_does_not_stop_it_from_starting():
    settings = Settings(SLEEP_MODE_SWEEP_INTERVAL="60")

    assert "SLEEP_MODE_SWEEP_INTERVAL" in (settings.model_extra or {})


def test_known_settings_still_load_normally():
    settings = Settings(KEYCLOAK_ENFORCE_ADMIN_OTP=True, SOMETHING_UNKNOWN="x")

    assert settings.KEYCLOAK_ENFORCE_ADMIN_OTP is True


def test_every_unknown_key_is_named_in_the_warning(caplog):
    """Naming them is what keeps a typo visible now that it is no longer fatal."""
    with caplog.at_level(logging.WARNING, logger="opi.core.config"):
        Settings(KEYLCOAK_TYPO="x", SLEEP_MODE_FUTURE="y")

    assert "KEYLCOAK_TYPO" in caplog.text
    assert "SLEEP_MODE_FUTURE" in caplog.text


def test_a_clean_config_warns_about_nothing(caplog):
    with caplog.at_level(logging.WARNING, logger="opi.core.config"):
        Settings()

    assert "unknown configuration key" not in caplog.text
