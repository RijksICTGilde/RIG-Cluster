"""Drift guard op de sjabloonbron: geen CDN meer, en elk eigen script draagt een nonce.

De CSP staat geen externe scripthost meer toe (``opi/middleware/security_headers.py``).
Een verwijzing die daarna terugkomt levert geen foutmelding op, alleen een blok dat niet
meer werkt, dus wordt hij hier gemeten in plaats van in de browser.
"""

import re
from pathlib import Path

import pytest

OPI = Path(__file__).parent.parent / "opi"
STATIC = Path(__file__).parent.parent / "static"

#: De mappen met sjablonen die de applicatie rendert.
SJABLOONMAPPEN = (OPI / "templates_lotc", OPI / "services" / "catalog")

#: Met de dubbele slash ervoor, zodat een LAADADRES wordt gemeten en niet de uitleg in
#: base_lotc.html.j2 die de hostnaam noemt om te zeggen dat hij er niet meer is.
EXTERNE_HOSTS = ("//cdn.jsdelivr.net", "//unpkg.com")

#: Wat uit static/ komt in plaats van van een CDN; static_url() geeft een niet-bestaand
#: bestand stil een URL zonder ?v=, dus het ontbreken valt pas in de browser op.
MEEGELEVERDE_BIBLIOTHEKEN = (
    "js/chart.min.js",
    "js/chartjs-plugin-annotation.min.js",
    "js/mermaid.min.js",
    "swagger-ui/swagger-ui-bundle.js",
    "swagger-ui/swagger-ui.css",
    "redoc/redoc.standalone.js",
)


def _sjablonen() -> list[Path]:
    return sorted(pad for map_ in SJABLOONMAPPEN for pad in map_.rglob("*.j2"))


@pytest.mark.parametrize("host", EXTERNE_HOSTS)
def test_geen_sjabloon_laadt_van_een_externe_host(host: str) -> None:
    treffers = [str(pad.relative_to(OPI)) for pad in _sjablonen() if host in pad.read_text()]

    assert not treffers, f"{host} staat nog in: {', '.join(treffers)}"


def test_elk_eigen_script_draagt_een_nonce() -> None:
    """Een inline ``<script>`` zonder nonce is een schending onder het Report-Only-beleid,
    en dus ruis in de meting waar de baseline op staat."""
    opening = re.compile(r'<script(?=[ >])((?:[^>"]|"[^"]*")*?)>')

    zonder = [
        f"{pad.relative_to(OPI)}: <script{attrs}>"
        for pad in _sjablonen()
        for attrs in opening.findall(pad.read_text())
        if "nonce=" not in attrs
    ]

    assert not zonder, "script zonder nonce:\n" + "\n".join(zonder)


@pytest.mark.parametrize("bestand", MEEGELEVERDE_BIBLIOTHEKEN)
def test_de_meegeleverde_bibliotheek_staat_er(bestand: str) -> None:
    pad = STATIC / bestand

    assert pad.is_file(), f"{bestand} ontbreekt in static/; zie static/HERKOMST.md"
