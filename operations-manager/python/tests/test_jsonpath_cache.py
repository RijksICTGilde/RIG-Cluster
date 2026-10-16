"""``compiled_jsonpath`` deelt expressies zonder data te delen.

WAAROM DIT EEN EIGEN TEST HEEFT

De compiler is gedeeld over alle projecten en alle gelijktijdige taken. Zou een
gecompileerde expressie iets van een ``find()`` onthouden, dan zou project B de waarden
van project A kunnen zien: een lek tussen projecten, niet alleen een verkeerd antwoord.
De cache bewaart daarom alleen de expressie, met de padstring als enige sleutel. Deze
toets legt dat vast voor de versie van ``jsonpath_ng`` in ``uv.lock``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from jsonpath_ng.ext import parse as jsonpath_parse
from opi.utils.yaml_util import compiled_jsonpath

_PAD_MET_FILTER = "$.deployments[?@.name=='prod'].components[*].reference"


def _project(naam: str, refs: list[str]) -> dict[str, Any]:
    return {
        "name": naam,
        "deployments": [
            {"name": "prod", "components": [{"reference": r} for r in refs]},
            {"name": "acc", "components": [{"reference": f"{naam}-acc"}]},
        ],
    }


def _zoek(data: dict[str, Any]) -> list[str]:
    return [match.value for match in compiled_jsonpath(_PAD_MET_FILTER).find(data)]


def test_dezelfde_padstring_geeft_dezelfde_expressie() -> None:
    assert compiled_jsonpath(_PAD_MET_FILTER) is compiled_jsonpath(_PAD_MET_FILTER)


def test_een_gebruikte_expressie_blijft_gelijk_aan_een_verse() -> None:
    """Een ``find()`` mag niets aan de expressie veranderen."""
    expressie = compiled_jsonpath(_PAD_MET_FILTER)
    expressie.find(_project("a", ["a-1"]))
    assert expressie == jsonpath_parse(_PAD_MET_FILTER)


def test_geen_kruisbestuiving_tussen_projecten() -> None:
    """B krijgt alleen B, en A daarna weer alleen A."""
    a = _project("a", ["a-1", "a-2"])
    b = _project("b", ["b-1"])

    assert _zoek(a) == ["a-1", "a-2"]
    assert _zoek(b) == ["b-1"]
    assert _zoek(a) == ["a-1", "a-2"]


@pytest.mark.asyncio
async def test_geen_kruisbestuiving_tussen_gelijktijdige_taken() -> None:
    """Twee taken die in threads op dezelfde expressie zoeken, zien elkaars data niet."""
    projecten = [_project(f"p{i}", [f"p{i}-{j}" for j in range(5)]) for i in range(8)]

    resultaten = await asyncio.gather(*(asyncio.to_thread(_zoek, p) for p in projecten))

    for i, gevonden in enumerate(resultaten):
        assert gevonden == [f"p{i}-{j}" for j in range(5)]


def test_de_cache_sleutel_is_alleen_het_pad() -> None:
    """Een ander pad geeft een andere expressie, hetzelfde pad niet."""
    ander = "$.deployments[?@.name=='acc'].components[*].reference"
    assert compiled_jsonpath(ander) is not compiled_jsonpath(_PAD_MET_FILTER)
    assert _zoek(_project("a", ["a-1"])) == ["a-1"]
    assert [m.value for m in compiled_jsonpath(ander).find(_project("a", ["a-1"]))] == ["a-acc"]
