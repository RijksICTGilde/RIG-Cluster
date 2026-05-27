"""
Tests for the SECRET_KEY safety design.

Two layers:

1. The pure ``validate_secret_key`` / ``generate_secret_key`` logic.
2. The real ``Settings`` model_validator wiring, so a regression in how the
   validator is hooked up (or a production-shaped environment) is actually
   caught here rather than silently booting with a forgeable key.

Design: no hard-coded dev default. If SECRET_KEY is unset, a fresh random key
is generated per process (sessions invalidate on restart). If SECRET_KEY is
set, it must be at least MIN_SECRET_KEY_LENGTH characters.

Run with:

    uv run pytest --noconftest tests/test_secret_key_failclosed.py
"""

import importlib

import pytest
from opi.core.secret_key import (
    MIN_SECRET_KEY_LENGTH,
    InsecureSecretKeyError,
    generate_secret_key,
    validate_secret_key,
)

STRONG_KEY = "x" * MIN_SECRET_KEY_LENGTH


class TestGenerateSecretKey:
    """The default factory must produce a key that passes its own validator."""

    def test_generated_key_meets_minimum_length(self) -> None:
        key = generate_secret_key()
        assert len(key) >= MIN_SECRET_KEY_LENGTH

    def test_generated_keys_are_unique(self) -> None:
        # Two calls must not collide -- this is the whole point of secrets.token_urlsafe.
        assert generate_secret_key() != generate_secret_key()

    def test_generated_key_passes_validator(self) -> None:
        # The factory output must never fail the validator -- otherwise the
        # default code path raises at startup, which would be a regression.
        validate_secret_key(generate_secret_key())

    def test_generate_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level("WARNING"):
            generate_secret_key()
        assert any("SECRET_KEY not set" in record.message for record in caplog.records)


class TestValidateSecretKey:
    """A short or missing key must raise; a sufficiently long key must pass."""

    def test_empty_raises(self) -> None:
        with pytest.raises(InsecureSecretKeyError, match="at least"):
            validate_secret_key("")

    def test_short_key_raises(self) -> None:
        short_key = "a" * (MIN_SECRET_KEY_LENGTH - 1)
        with pytest.raises(InsecureSecretKeyError, match="at least"):
            validate_secret_key(short_key)

    def test_strong_key_passes(self) -> None:
        validate_secret_key(STRONG_KEY)

    def test_key_at_exact_minimum_length_passes(self) -> None:
        validate_secret_key("k" * MIN_SECRET_KEY_LENGTH)


def _load_settings_class(monkeypatch: pytest.MonkeyPatch):
    """
    Import opi.core.config and return its Settings class.

    config.py instantiates a module-level ``settings = Settings()`` on import,
    so we make sure no SECRET_KEY is set first -- the factory will then run
    and the module-level instantiation succeeds.

    If the import fails for a reason unrelated to this fix (a stale installed
    package mismatch such as ``setup_logging() got an unexpected keyword
    argument`` that also breaks origin/main in this environment), the test is
    skipped rather than reported as a SECRET_KEY regression.
    """
    monkeypatch.delenv("SECRET_KEY", raising=False)
    try:
        config = importlib.import_module("opi.core.config")
        config = importlib.reload(config)
    except InsecureSecretKeyError:
        raise
    except (TypeError, ImportError) as exc:  # pre-existing unrelated env breakage
        pytest.skip(f"opi.core.config import broken by unrelated environment issue: {exc}")
    return config.Settings


class TestSettingsModelValidatorWiring:
    """
    Exercise the real Settings model_validator so a wiring regression (or a
    production-shaped env) fails the test instead of silently booting with a
    forgeable key. Also guards the `-> Settings` class-body NameError.
    """

    def test_config_module_imports(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Regression guard for the `-> Settings` NameError at class-body eval.
        # Reaching this line means the class body evaluated and the module-level
        # Settings() succeeded with the factory-generated key.
        _load_settings_class(monkeypatch)

    def test_unset_env_uses_random_factory_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings_cls = _load_settings_class(monkeypatch)
        monkeypatch.delenv("SECRET_KEY", raising=False)
        settings = settings_cls(_env_file=None)
        assert len(settings.SECRET_KEY) >= MIN_SECRET_KEY_LENGTH
        # Second instantiation must produce a different key -- proves the
        # factory ran fresh and we are not pinned to a constant default.
        other = settings_cls(_env_file=None)
        assert settings.SECRET_KEY != other.SECRET_KEY

    def test_short_env_key_refuses_to_boot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings_cls = _load_settings_class(monkeypatch)
        monkeypatch.setenv("SECRET_KEY", "short")
        with pytest.raises(InsecureSecretKeyError):
            settings_cls(_env_file=None)

    def test_strong_env_key_boots(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings_cls = _load_settings_class(monkeypatch)
        monkeypatch.setenv("SECRET_KEY", STRONG_KEY)
        settings = settings_cls(_env_file=None)
        assert settings.SECRET_KEY == STRONG_KEY
