"""De grendel weigert elk doel dat het harnas niet zelf heeft opgezet.

Dit is de belangrijkste proef van het harnas: zonder deze is een aanvalsproef een aanval.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest

from security_tests.doel import (
    LOKAAL,
    SANDBOX,
    Doel,
    OnveiligDoel,
    lees_runbestand,
    main,
    soort_doel,
    verifieer_doel,
    verifieer_host,
)

if TYPE_CHECKING:
    from pathlib import Path

#: Adressen die er plausibel uitzien en die de grendel moet weigeren. Staging en productie
#: staan erbij: die zijn geen doel van dit harnas, hoe erop het runbestand ook wijst.
EXTERNE_DOELEN = (
    "https://zad.rijksapps.nl",
    "https://zad.staging.rijksapp.dev",
    "http://10.0.0.5:8311",
    "http://127.0.0.1.aanvaller.example",
    "https://zad.sandbox.rijksapp.dev.aanvaller.example",
)


@pytest.mark.parametrize("url", EXTERNE_DOELEN)
def test_een_extern_doel_wordt_geweigerd_zonder_een_verzoek(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Faalconditie: de grendel stuurt een verzoek naar een doel dat hij hoort te weigeren."""

    def niet_versturen(*_: object, **__: object) -> httpx.Response:
        raise AssertionError(f"de grendel stuurde een verzoek naar {url}")

    monkeypatch.setattr(httpx, "get", niet_versturen)
    with pytest.raises(OnveiligDoel, match="geen beproefbare sandbox"):
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
    ("url", "verwacht"),
    [
        ("http://127.0.0.1:8311", LOKAAL),
        ("http://localhost:8311", LOKAAL),
        ("http://[::1]:8311", LOKAAL),
        ("https://zad.sandbox.rijksapp.dev", SANDBOX),
        ("https://forgejo.sandbox.rijksapp.dev", SANDBOX),
        ("https://zad.rijksapps.nl", None),
        ("https://zad.staging.rijksapp.dev", None),
        ("http://127.0.0.1.aanvaller.example", None),
        ("http://192.168.1.10", None),
        # Op naam en niet op bereikbaarheid: het IP van de sandbox is geen toegestaan doel.
        ("http://172.18.0.2", None),
    ],
)
def test_alleen_de_eigen_machine_en_het_sandboxdomein_zijn_een_doel(url: str, verwacht: str | None) -> None:
    assert soort_doel(url) == verwacht


def test_een_sandboxhost_die_het_token_niet_terugkaatst_wordt_geweigerd(monkeypatch: pytest.MonkeyPatch) -> None:
    """Het derde slot geldt ook voor de sandbox: zonder het token van deze run is het geen doel."""

    def zonder_token(*_: object, **__: object) -> httpx.Response:
        return httpx.Response(200, headers={})

    monkeypatch.setattr(httpx, "get", zonder_token)
    with pytest.raises(OnveiligDoel, match="kaatst token"):
        verifieer_doel("https://zad.sandbox.rijksapp.dev", "het-token-van-deze-run")


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
