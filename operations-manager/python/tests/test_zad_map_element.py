"""Het ingecheckte kaartelement past binnen de CSP van OPI.

``script-src`` heeft geen ``unsafe-eval``, dus een bundel met ``eval(`` of ``new Function(``
erin draait wel in de browser van de bouwer en niet in het portaal: de kaart blijft dan leeg
zonder foutmelding op het scherm. Deze poort leest het bestand dat echt uitgeleverd wordt.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from opi.services.zad_map.contract import Glyph

ROOT = Path(__file__).resolve().parents[3]
BUNDLE = Path(__file__).resolve().parent.parent / "static" / "js" / "zad-map.js"
ICONS = ROOT / "operations-manager" / "zad-map" / "src" / "lib" / "icons.js"


def test_bundle_is_in_the_repo() -> None:
    assert BUNDLE.exists(), "static/js/zad-map.js ontbreekt. Bouwen: task zad-map:build"


@pytest.mark.parametrize("forbidden", ["eval(", "new Function("])
def test_bundle_uses_no_eval(forbidden: str) -> None:
    assert forbidden not in BUNDLE.read_text(encoding="utf-8", errors="ignore")


def test_every_glyph_exists_in_the_element() -> None:
    """De glyphs in het contract zijn die van het element, niet die van NLDD.

    Een naam die het element niet kent rendert als niets, zonder foutmelding, en komt dus
    nergens anders aan het licht.
    """
    provided = set(re.findall(r"^\s*([a-z]+):\s*'", ICONS.read_text(encoding="utf-8"), re.MULTILINE))
    assert {glyph.value for glyph in Glyph} <= provided
