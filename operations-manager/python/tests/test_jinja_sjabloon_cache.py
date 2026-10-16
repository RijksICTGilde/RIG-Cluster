"""Een manifestsjabloon wordt een keer gecompileerd, niet per manifest.

WAAROM DIT EEN EIGEN TEST HEEFT

``render_template`` en ``ManifestGenerator.template_manifest`` bouwden per manifest een
nieuwe ``Environment`` en compileerden met ``from_string``. ``deployment.yaml.jinja``
compileren kost alleen al 12 ms, ongeveer 30 ms per component, en dat gebeurde synchroon
op de event loop die FastAPI, de TaskWorker en alle schedulers deelt.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from opi.core.config import settings
from opi.generation.manifests import ManifestGenerator, _gecompileerd, _omgeving, render_template

_SJABLOON = "namespace.yaml.jinja"
_WAARDEN = {"name": "rig-demo", "project_name": "demo", "deployment_name": "productie"}


@pytest.fixture(autouse=True)
def _lege_cache():
    _gecompileerd.cache_clear()
    yield
    _gecompileerd.cache_clear()


def _bron() -> str:
    return (Path(settings.MANIFESTS_PATH) / _SJABLOON).read_text()


def test_twee_renders_kosten_een_compilatie() -> None:
    bron = _bron()
    omgeving = _omgeving()

    with patch.object(omgeving, "from_string", wraps=omgeving.from_string) as compileer:
        eerste = _gecompileerd(bron).render(**_WAARDEN)
        tweede = _gecompileerd(bron).render(**_WAARDEN)

    assert compileer.call_count == 1
    assert eerste == tweede


def test_de_uitvoer_is_gelijk_aan_een_verse_compilatie() -> None:
    """De gedeelde omgeving mag de uitvoer niet veranderen: byte-gelijk."""
    bron = _bron()

    via_cache = render_template(_SJABLOON, _WAARDEN)
    vers = _omgeving().from_string(bron).render(**_WAARDEN)

    assert via_cache == vers


def test_render_template_en_template_manifest_delen_de_compilatie() -> None:
    """Beide paden hadden een eigen Environment; dat zijn er nu geen twee meer."""
    bron = _bron()
    omgeving = _omgeving()

    with patch.object(omgeving, "from_string", wraps=omgeving.from_string) as compileer:
        via_pad = render_template(_SJABLOON, _WAARDEN)
        via_inhoud = ManifestGenerator().template_manifest(bron, _WAARDEN)

    assert compileer.call_count == 1
    assert via_pad == via_inhoud


def test_een_gewijzigd_sjabloon_wordt_opnieuw_gecompileerd() -> None:
    """De sleutel is de brontekst, dus een wijziging op schijf telt gewoon mee."""
    generator = ManifestGenerator()

    eerste = generator.template_manifest("naam: {{ name }}\n", {"name": "een"})
    tweede = generator.template_manifest("naam: {{ name }}!\n", {"name": "een"})

    assert eerste == "naam: een\n"
    assert tweede == "naam: een!\n"
