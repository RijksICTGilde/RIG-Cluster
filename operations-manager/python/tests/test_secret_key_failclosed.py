"""
Tests for the fail-closed SECRET_KEY validation.

These exercise the pure ``validate_secret_key`` function directly. They do not
boot the FastAPI app or import ``opi.core.config`` (which pulls in the broken
fastapi/pydantic-settings import chain in this environment). Run with:

    uv run pytest --noconftest tests/test_secret_key_failclosed.py
"""

import pytest
from opi.core.secret_key import (
    DEV_DEFAULT_SECRET_KEY,
    MIN_SECRET_KEY_LENGTH,
    InsecureSecretKeyError,
    validate_secret_key,
)

STRONG_KEY = "x" * MIN_SECRET_KEY_LENGTH


class TestProductionFailsClosed:
    """In production-like mode (debug=False) unsafe keys must refuse to boot."""

    def test_unset_key_raises(self) -> None:
        with pytest.raises(InsecureSecretKeyError, match="not set"):
            validate_secret_key(None, debug=False)

    def test_empty_key_raises(self) -> None:
        with pytest.raises(InsecureSecretKeyError, match="not set"):
            validate_secret_key("", debug=False)

    def test_dev_default_raises(self) -> None:
        with pytest.raises(InsecureSecretKeyError, match="development default"):
            validate_secret_key(DEV_DEFAULT_SECRET_KEY, debug=False)

    def test_short_key_raises(self) -> None:
        short_key = "a" * (MIN_SECRET_KEY_LENGTH - 1)
        with pytest.raises(InsecureSecretKeyError, match="shorter than"):
            validate_secret_key(short_key, debug=False)

    def test_strong_key_passes(self) -> None:
        validate_secret_key(STRONG_KEY, debug=False)

    def test_strong_key_at_exact_minimum_length_passes(self) -> None:
        validate_secret_key("k" * MIN_SECRET_KEY_LENGTH, debug=False)


class TestDevelopmentAllowsDefault:
    """In DEBUG mode the dev-default is tolerated (with a loud warning)."""

    def test_dev_default_allowed_in_debug(self) -> None:
        validate_secret_key(DEV_DEFAULT_SECRET_KEY, debug=True)

    def test_unset_allowed_in_debug(self) -> None:
        validate_secret_key(None, debug=True)

    def test_short_key_allowed_in_debug(self) -> None:
        validate_secret_key("short", debug=True)

    def test_dev_default_in_debug_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level("WARNING"):
            validate_secret_key(DEV_DEFAULT_SECRET_KEY, debug=True)
        assert any("INSECURE" in record.message for record in caplog.records)

    def test_strong_key_passes_in_debug(self) -> None:
        validate_secret_key(STRONG_KEY, debug=True)
