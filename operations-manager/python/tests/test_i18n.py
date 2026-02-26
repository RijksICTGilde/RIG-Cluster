"""Tests for the i18n/Babel integration."""

from unittest.mock import MagicMock

from opi.core.i18n import (
    DEFAULT_LANGUAGE,
    get_requested_language,
    get_supported_language,
    get_translation,
)


class TestGetSupportedLanguage:
    def test_supported_nl(self):
        assert get_supported_language("nl") == "nl"

    def test_supported_en(self):
        assert get_supported_language("en") == "en"

    def test_unsupported_falls_back(self):
        assert get_supported_language("de") == DEFAULT_LANGUAGE

    def test_empty_falls_back(self):
        assert get_supported_language("") == DEFAULT_LANGUAGE

    def test_case_insensitive(self):
        assert get_supported_language("NL") == "nl"

    def test_long_code_trimmed(self):
        assert get_supported_language("nl-NL") == "nl"


class TestGetRequestedLanguage:
    def _make_request(self, cookie=None, accept_language=None):
        request = MagicMock()
        request.cookies = {}
        request.headers = {}
        if cookie:
            request.cookies["lang"] = cookie
        if accept_language:
            request.headers["accept-language"] = accept_language
        return request

    def test_cookie_takes_priority(self):
        request = self._make_request(cookie="en", accept_language="nl-NL,nl;q=0.9")
        assert get_requested_language(request) == "en"

    def test_accept_language_fallback(self):
        request = self._make_request(accept_language="en-US,en;q=0.9")
        assert get_requested_language(request) == "en"

    def test_no_cookie_no_header_defaults(self):
        request = self._make_request()
        assert get_requested_language(request) == DEFAULT_LANGUAGE

    def test_unsupported_cookie_falls_back(self):
        request = self._make_request(cookie="de")
        assert get_requested_language(request) == DEFAULT_LANGUAGE


class TestGetTranslation:
    def test_returns_translation_object(self):
        t = get_translation("nl")
        assert t is not None
        # Should have gettext method
        assert hasattr(t, "gettext")

    def test_cached(self):
        t1 = get_translation("nl")
        t2 = get_translation("nl")
        assert t1 is t2

    def test_unsupported_lang_falls_back(self):
        t = get_translation("de")
        # Should still return a valid translation object (default lang)
        assert hasattr(t, "gettext")
