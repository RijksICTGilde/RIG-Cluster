"""Tests for the content-hashed static URL helper (opi.core.templates.static_url)."""

import os
import re
from pathlib import Path

import pytest

from opi.core import templates as templates_module
from opi.core.templates import static_url, templates

VERSIONED_URL = re.compile(r"^/static/js/wizard\.js\?v=[0-9a-f]{8}$")


def test_static_url_has_eight_character_hash() -> None:
    """An existing file gets /static/<path>?v=<8 hex chars>."""
    assert VERSIONED_URL.match(static_url("js/wizard.js"))


def test_static_url_accepts_leading_slash() -> None:
    """Callers may pass "js/wizard.js" or "/js/wizard.js"."""
    assert static_url("/js/wizard.js") == static_url("js/wizard.js")


def test_static_url_is_stable_for_unchanged_file() -> None:
    """Repeated calls return the same URL as long as the file does not change."""
    assert static_url("css/wizard.css") == static_url("css/wizard.css")


def test_static_url_differs_per_file() -> None:
    """Different contents produce different hashes."""
    assert static_url("js/wizard.js") != static_url("css/wizard.css")


def test_static_url_unversioned_for_missing_file() -> None:
    """A missing file falls back to an unversioned URL, which means no-cache, not a year."""
    assert static_url("js/does-not-exist.js") == "/static/js/does-not-exist.js"


def test_static_url_changes_when_file_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Changing the contents changes the URL - this is the whole point of the helper.

    Also covers the skaffold loop: a synced file gets a new mtime and size, the cache key
    misses, and the hash is recomputed without a restart.
    """
    monkeypatch.setattr(templates_module, "STATIC_DIR", tmp_path)
    monkeypatch.setattr(templates_module, "_STATIC_HASHES", {})
    static_file = tmp_path / "app.js"
    static_file.write_text("console.log('one');")

    before = static_url("app.js")

    static_file.write_text("console.log('two - a different length');")
    os.utime(static_file, (1, 1))

    after = static_url("app.js")
    assert before != after
    assert after.startswith("/static/app.js?v=")


def test_static_url_registered_as_jinja_global() -> None:
    """Templates can call static_url() directly."""
    assert templates.env.globals["static_url"] is static_url
    rendered = templates.env.from_string("{{ static_url('js/wizard.js') }}").render()
    assert VERSIONED_URL.match(rendered)
