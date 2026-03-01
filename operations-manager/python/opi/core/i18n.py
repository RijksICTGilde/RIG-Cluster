"""Internationalization support using Babel.

Follows the TAD project pattern: per-request translation installation
in Jinja2 with cached Translation objects.
"""

from __future__ import annotations

from functools import lru_cache
from gettext import NullTranslations
from pathlib import Path
from typing import TYPE_CHECKING

from babel.support import Translations

if TYPE_CHECKING:
    from starlette.requests import Request

SUPPORTED_LANGUAGES = ("nl", "en")
DEFAULT_LANGUAGE = "nl"

LOCALE_DIR = Path(__file__).parent.parent / "locale"


def get_supported_language(lang: str) -> str:
    """Return a supported language code, falling back to default."""
    lang = lang.strip().lower()[:2]
    if lang in SUPPORTED_LANGUAGES:
        return lang
    return DEFAULT_LANGUAGE


def _browser_language(request: Request) -> str:
    """Extract preferred language from Accept-Language header."""
    accept = request.headers.get("accept-language", "")
    if not accept:
        return DEFAULT_LANGUAGE
    # Parse first language tag (e.g., "nl-NL,nl;q=0.9,en;q=0.8")
    for part in accept.split(","):
        tag = part.split(";")[0].strip()
        lang = tag[:2].lower()
        if lang in SUPPORTED_LANGUAGES:
            return lang
    return DEFAULT_LANGUAGE


def get_requested_language(request: Request) -> str:
    """Determine the language for this request.

    Priority: cookie 'lang' > Accept-Language header > default.
    """
    cookie_lang = request.cookies.get("lang", "")
    if cookie_lang:
        return get_supported_language(cookie_lang)
    return _browser_language(request)


@lru_cache(maxsize=len(SUPPORTED_LANGUAGES))
def get_translation(lang: str) -> NullTranslations:
    """Load compiled Babel translations for the given language.

    Results are cached per language code. Falls back to NullTranslations
    if no .mo file exists yet.
    """
    lang = get_supported_language(lang)
    locale_str = f"{lang}_NL" if lang == "nl" else f"{lang}_US"
    try:
        return Translations.load(str(LOCALE_DIR), locales=locale_str)
    except FileNotFoundError:
        return NullTranslations()


def get_current_translation(request: Request) -> NullTranslations:
    """Get the translation object for the current request."""
    return get_translation(get_requested_language(request))
