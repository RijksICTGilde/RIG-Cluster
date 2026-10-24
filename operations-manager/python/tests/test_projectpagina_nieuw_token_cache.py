"""De pagina die een vers token eenmalig toont mag niet in een cache blijven staan.

Op het tabblad Team staat een uitgegeven token precies een keer op het scherm, en dat is
een credential van 90 tot 365 dagen. Zonder ``Cache-Control: no-store`` mag een browser
die pagina op schijf zetten en toont de terug-knop hem opnieuw, ook nadat hij server-side
al is weggegooid. Dezelfde reden en dezelfde header als bij
``GET /api/v2/projects/{project_name}/api-key``.

Op elk ANDER verzoek hoort de header er niet te staan: dan draagt de pagina geen token en
is het alleen onnodig cacheverlies.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from opi.services.project_service import ProjectSummary, ProjectUser
from opi.utils.sops import generate_sops_key_pair
from opi.web import router_tokens
from starlette.requests import Request

if TYPE_CHECKING:
    import pytest

PROJECT = "cachetoets"
LID = "baas@rijksoverheid.nl"


def _vast(waarde: str):
    async def _geef(*_: Any, **__: Any) -> str:
        return waarde

    return _geef


class _Store:
    def __init__(self, project: ProjectSummary) -> None:
        self._project = project

    def get(self, name: str) -> ProjectSummary | None:
        return self._project if name == self._project.name else None


def _projectbestand(publieke_sleutel: str) -> dict[str, Any]:
    return {
        "schema-version": "2.12",
        "name": PROJECT,
        "display-name": "Cachetoets",
        "clusters": ["odcn-production"],
        "users": [{"email": LID, "role": "admin"}],
        "config": {"age-public-key": publieke_sleutel, "age-private-key": "ingepakt-door-de-kek"},
    }


def _verzoek(pad: str, sessie: dict[str, Any]) -> Request:
    from opi.server import app

    verzoek = Request(
        {
            "type": "http",
            "method": "GET",
            "path": pad,
            "raw_path": pad.encode(),
            "query_string": b"",
            "headers": [],
            "app": app,
            "router": app.router,
            "session": sessie,
            "state": {},
        }
    )
    verzoek.state.user = {"email": LID, "name": "Baas"}
    verzoek.state.csrf_token = "csrf-van-de-toets"
    return verzoek


async def _pagina(monkeypatch: pytest.MonkeyPatch, pad: str, sessie: dict[str, Any]):
    from opi.web import router

    project_prive, project_publiek = generate_sops_key_pair()
    # De sleutel uitpakken vraagt de KEK van het cluster, en die hoort niet bij wat hier
    # gemeten wordt: de header op het antwoord.
    monkeypatch.setattr(router, "unwrap_project_key", _vast(project_prive))
    monkeypatch.setattr(router, "is_user_authorized_for_project", lambda *_: True)
    monkeypatch.setattr(router, "get_user_role_for_project", lambda *_: "admin")
    project = ProjectSummary(
        name=PROJECT,
        filename=f"{PROJECT}.yaml",
        users=[ProjectUser(email=LID, role="admin")],
        data=_projectbestand(project_publiek),
    )
    monkeypatch.setattr(router, "get_project_store", lambda: _Store(project))
    monkeypatch.setattr(router, "is_projectbeheerder", lambda *_: True)
    monkeypatch.setattr(router_tokens, "leden_overzicht", lambda _: [])

    return await router.project_details(_verzoek(pad, sessie), PROJECT)


async def test_het_tabblad_met_een_vers_token_mag_niet_gecachet_worden(monkeypatch: pytest.MonkeyPatch) -> None:
    verwijzing = router_tokens._leg_klaar({"naam": "cli", "waarde": "zad_waardevandetoets"})
    antwoord = await _pagina(monkeypatch, f"/projects/{PROJECT}/team", {router_tokens.NIEUW_TOKEN_SLEUTEL: verwijzing})

    assert antwoord.status_code == 200
    assert antwoord.headers["Cache-Control"] == "no-store"


async def test_het_tabblad_zonder_vers_token_houdt_de_gewone_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    antwoord = await _pagina(monkeypatch, f"/projects/{PROJECT}/team", {})

    assert antwoord.status_code == 200
    assert "no-store" not in antwoord.headers.get("Cache-Control", "")


async def test_een_ander_tabblad_houdt_de_gewone_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Het tabblad Overzicht leest de leden niet, dus er valt daar geen token te tonen."""
    antwoord = await _pagina(monkeypatch, f"/projects/{PROJECT}/details", {})

    assert antwoord.status_code == 200
    assert "no-store" not in antwoord.headers.get("Cache-Control", "")
