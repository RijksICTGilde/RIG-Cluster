"""De bestemmingskeuze van een uitnodiging moet te onderscheiden zijn.

Gemeten op productie (toets-hn7, 18 augustus 2026): de lijst toonde elke bestemming
twee keer, "production / frontend" boven "production / frontend", "pr-19 / frontend"
boven "pr-19 / frontend", enzovoort. Je moest er een kiezen zonder te kunnen zien
waarin ze verschilden.

Het waren geen dubbelingen. Een component mag meerdere paden publiceren en dat zijn
evenzoveel adressen; de ontdubbeling in de provider werkt op de URL en die verschilden
echt. Alleen het label noemde het pad niet, en dat is precies het veld waarin ze
verschilden.

Het pad komt er daarom bij waar het iets oplost, en niet waar het ruis is: bij een
component met een enkel pad zegt "(/)" achter de naam niets.
"""

from __future__ import annotations

from typing import Any

from opi.forms.visualizers.providers import InviteApplicationUrlOptionsProvider


class _Urls:
    """Levert de rijen die public_urls_for_project zou opleveren."""

    def __init__(self, rijen: list[dict[str, str]]) -> None:
        self.rijen = rijen

    def __call__(self, *_args: Any, **_kwargs: Any) -> list[dict[str, str]]:
        return self.rijen


def _opties(monkeypatch: Any, rijen: list[dict[str, str]], huidig: str | None = None) -> list[dict[str, Any]]:
    monkeypatch.setattr(
        "opi.services.catalog.publish_on_web.urls.public_urls_for_project",
        _Urls(rijen),
    )
    return InviteApplicationUrlOptionsProvider(yaml_data={"name": "toets-hn7"}, current_value=huidig).get_options()


def _labels(opties: list[dict[str, Any]]) -> list[str]:
    return [o["label"] for o in opties]


def test_twee_paden_op_een_component_zijn_te_onderscheiden(monkeypatch: Any) -> None:
    """Dit is het geval van de melding: twee keer dezelfde regel."""
    opties = _opties(
        monkeypatch,
        [
            {"deployment_name": "production", "component_name": "frontend", "path": "/", "url": "https://a/"},
            {"deployment_name": "production", "component_name": "frontend", "path": "/api", "url": "https://a/api"},
        ],
    )

    labels = _labels(opties)
    assert "production / frontend (/)" in labels
    assert "production / frontend (/api)" in labels
    assert len(labels) == len(set(labels)), f"nog steeds niet te onderscheiden: {labels}"


def test_een_enkel_pad_krijgt_geen_ruis_achter_de_naam(monkeypatch: Any) -> None:
    """Zonder keuze valt er niets te verduidelijken, dus blijft het label kaal."""
    opties = _opties(
        monkeypatch,
        [
            {"deployment_name": "production", "component_name": "frontend", "path": "/", "url": "https://a/"},
            {"deployment_name": "pr-19", "component_name": "frontend", "path": "/", "url": "https://b/"},
        ],
    )

    labels = _labels(opties)
    assert "production / frontend" in labels
    assert "pr-19 / frontend" in labels
    assert not [x for x in labels if "(/)" in x]


def test_dezelfde_url_blijft_een_keer_in_de_lijst(monkeypatch: Any) -> None:
    """De bestaande ontdubbeling op URL mag niet sneuvelen door de padtoevoeging."""
    opties = _opties(
        monkeypatch,
        [
            {"deployment_name": "production", "component_name": "frontend", "path": "/", "url": "https://a/"},
            {"deployment_name": "production", "component_name": "frontend", "path": "/", "url": "https://a/"},
        ],
    )

    assert _labels(opties) == ["Geen knop tonen", "production / frontend"]


def test_een_opgeslagen_waarde_die_niet_meer_bestaat_blijft_kiesbaar(monkeypatch: Any) -> None:
    """Anders laat opslaan van het formulier de bestemming stilletjes vallen."""
    opties = _opties(
        monkeypatch,
        [{"deployment_name": "production", "component_name": "frontend", "path": "/", "url": "https://a/"}],
        huidig="https://weg.example/",
    )

    assert "https://weg.example/ (niet meer afleidbaar)" in _labels(opties)


def test_de_lege_keuze_staat_vooraan(monkeypatch: Any) -> None:
    """Een uitnodiging zonder bestemming is geldig en toont simpelweg geen knop."""
    opties = _opties(monkeypatch, [])

    assert opties[0] == {"value": "", "label": "Geen knop tonen"}
