"""De grendel weigert elk doel dat het harnas niet zelf heeft opgezet.

Dit is de belangrijkste proef van het harnas: zonder deze is een aanvalsproef een aanval.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest

from security_tests.doel import (
    Doel,
    OnveiligDoel,
    host_is_lokaal,
    lees_runbestand,
    main,
    verifieer_doel,
    verifieer_host,
)

if TYPE_CHECKING:
    from pathlib import Path

#: Adressen die er plausibel uitzien en die de grendel moet weigeren.
EXTERNE_DOELEN = (
    "https://zad.rijksapps.nl",
    "https://zad.sandbox.rijksapp.dev",
    "http://10.0.0.5:8311",
    "http://127.0.0.1.aanvaller.example",
)


@pytest.mark.parametrize("url", EXTERNE_DOELEN)
def test_een_extern_doel_wordt_geweigerd_zonder_een_verzoek(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Faalconditie: de grendel stuurt een verzoek naar een doel dat hij hoort te weigeren."""

    def niet_versturen(*_: object, **__: object) -> httpx.Response:
        raise AssertionError(f"de grendel stuurde een verzoek naar {url}")

    monkeypatch.setattr(httpx, "get", niet_versturen)
    with pytest.raises(OnveiligDoel, match="geen lokale sandbox"):
        verifieer_doel(url, "een-token")


@pytest.mark.parametrize("url", EXTERNE_DOELEN)
def test_de_grendel_als_commando_eindigt_niet_nul(
    url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """De grendel is ook los te draaien, en meldt dan waarom hij weigert."""

    def niet_versturen(*_: object, **__: object) -> httpx.Response:
        raise AssertionError(f"de grendel stuurde een verzoek naar {url}")

    monkeypatch.setattr(httpx, "get", niet_versturen)
    assert main([url]) == 2
    assert "GEWEIGERD" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("url", "lokaal"),
    [
        ("http://127.0.0.1:8311", True),
        ("http://localhost:8311", True),
        ("http://[::1]:8311", True),
        ("https://zad.rijksapps.nl", False),
        ("http://127.0.0.1.aanvaller.example", False),
        ("http://192.168.1.10", False),
    ],
)
def test_alleen_de_eigen_machine_is_een_toegestane_host(url: str, lokaal: bool) -> None:
    assert host_is_lokaal(url) is lokaal


def test_zonder_runbestand_is_er_geen_doel(tmp_path: Path) -> None:
    """Zonder een sandbox die het harnas zelf opzette, is er niets om te beproeven."""
    with pytest.raises(OnveiligDoel, match="geen draaiende proefsandbox"):
        lees_runbestand(tmp_path / "bestaat-niet.json")


def test_een_instantie_zonder_het_juiste_token_wordt_geweigerd(doel: Doel) -> None:
    """Faalconditie: een lokale instantie die het token niet terugkaatst, wordt toch beproefd."""
    with pytest.raises(OnveiligDoel, match="kaatst token"):
        verifieer_doel(doel.url, "een-ander-token")


def test_de_eigen_sandbox_komt_er_wel_door(doel: Doel) -> None:
    """De tegenproef: zonder deze zou een altijd-weigeren de proeven hierboven stil groen houden."""
    verifieer_host(doel.url)
    verifieer_doel(doel.url, doel.token)
