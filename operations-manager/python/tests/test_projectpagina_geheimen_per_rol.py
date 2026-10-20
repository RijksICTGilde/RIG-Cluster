"""De projectpagina ontsleutelt projectgeheimen alleen voor admin en owner.

De vier geheimen van een project (de AGE-private-key, de API-key, het
Keycloak-adminwachtwoord en de OTP-code) werden voor ELK lid ontsleuteld en in de
rendercontext gezet; alleen het sjabloon hield ze achter een rolcontrole. Een nieuw
fragment of een dump van ``config`` kwam er dus bij. De selectie van gegevens trekt de
grens nu zelf (BIO2 5.15 en 8.03).

Wat hier gemeten wordt is de CONTEXT en niet alleen de HTML: de HTML is wat het sjabloon
vandaag toont, de context is wat de pagina in handen heeft. De rolpoort van
``KeycloakService.realm_block`` wordt daarbij weggepatcht, zodat de toets ziet wat de HANDLER
doorgeeft en niet wat de dienst er daarna nog voor tegenhoudt.
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

import pytest
from opi.services.catalog.keycloak import KeycloakService
from opi.services.project_service import ProjectSummary, ProjectUser
from opi.utils.age import encrypt_age_content
from opi.utils.sops import generate_sops_key_pair
from starlette.requests import Request

API_KEY = "api-key-van-de-toets"
KEYCLOAK_WACHTWOORD = "keycloak-wachtwoord-van-de-toets"
TOTP_SEED = "otpseedvandetoets"


async def _projectbestand(globale_publieke_sleutel: str) -> tuple[dict[str, Any], str]:
    """Een projectbestand met de vier geheimen versleuteld, plus de private key erbij."""
    project_prive, project_publiek = generate_sops_key_pair()
    data = {
        "schema-version": 2,
        "name": "geheimtoets",
        "display-name": "Geheimtoets",
        "clusters": ["odcn-production"],
        "users": [{"email": "baas@rijksoverheid.nl", "role": "admin"}],
        "services": [
            {
                "name": "keycloak",
                "config": {
                    "realms": [
                        {
                            "host": "https://keycloak.example.invalid",
                            "realm": "geheimtoets",
                            "username": "geheimtoets_admin",
                            "password": await encrypt_age_content(KEYCLOAK_WACHTWOORD, project_publiek),
                            "totp_secret": await encrypt_age_content(TOTP_SEED, project_publiek),
                        }
                    ]
                },
            }
        ],
        "config": {
            "age-public-key": project_publiek,
            "age-private-key": await encrypt_age_content(project_prive, globale_publieke_sleutel),
            "api-key": await encrypt_age_content(API_KEY, project_publiek),
        },
    }
    return data, project_prive


def _waarden(knoop: Any, gezien: set[int] | None = None) -> list[str]:
    """Elke tekstwaarde in de context, hoe diep hij ook zit: ook in de velden van een
    object, want de dienstblokken komen binnen als ``DetailPageSection``."""
    gezien = set() if gezien is None else gezien
    if isinstance(knoop, str):
        return [knoop]
    if id(knoop) in gezien:
        return []
    gezien.add(id(knoop))
    if isinstance(knoop, dict):
        return [waarde for kind in knoop.values() for waarde in _waarden(kind, gezien)]
    if isinstance(knoop, list | tuple | set):
        return [waarde for kind in knoop for waarde in _waarden(kind, gezien)]
    velden = getattr(knoop, "__dict__", None)
    if velden is not None and not isinstance(knoop, type) and not callable(knoop):
        return [waarde for kind in velden.values() for waarde in _waarden(kind, gezien)]
    return []


def _contextwaarden(context: dict[str, Any]) -> list[str]:
    """Zonder ``request``: dat is het verzoek dat de toets zelf bouwde, en daarlangs loopt
    de hele applicatie mee de zoektocht in."""
    return _waarden({sleutel: waarde for sleutel, waarde in context.items() if sleutel != "request"})


async def _pagina(monkeypatch: pytest.MonkeyPatch, rol: str) -> tuple[dict[str, Any], str, str]:
    """Rendert de detailpagina voor een lid met deze rol: (context, html, private key)."""
    from opi.web import lotc_switch, router

    globale_prive, globale_publiek = generate_sops_key_pair()
    data, project_prive = await _projectbestand(globale_publiek)
    naam = data["name"]

    # Zonder dit levert realm_block voor een member geen blok en valt er voor die rol
    # niets te meten aan het wachtwoord en de OTP-code die de handler doorgeeft.
    echte_realm_block = KeycloakService.realm_block
    monkeypatch.setattr(
        KeycloakService,
        "realm_block",
        lambda zelf, ctx: echte_realm_block(zelf, replace(ctx, user_role="admin")),
    )

    monkeypatch.setattr(router, "get_global_private_key", lambda: globale_prive)
    monkeypatch.setattr(router, "is_user_authorized_for_project", lambda *_: True)
    monkeypatch.setattr(router, "get_user_role_for_project", lambda *_: rol)
    monkeypatch.setattr(
        router,
        "get_project_store",
        lambda: _Store(
            ProjectSummary(
                name=naam,
                api_key="niet-gebruikt",
                filename=f"{naam}.yaml",
                users=[ProjectUser(email="lid@rijksoverheid.nl", role=rol)],
                data=data,
            )
        ),
    )

    opgevangen: dict[str, Any] = {}

    def _render(request: Request, *, template: str, context: dict[str, Any]) -> Any:
        opgevangen.update(context)
        return lotc_switch.render(request, template=template, context=context)

    monkeypatch.setattr(router, "render", _render)

    antwoord = await router.project_details(_verzoek(naam), naam)
    assert antwoord.status_code == 200
    return opgevangen, antwoord.body.decode(), project_prive


class _Store:
    def __init__(self, project: ProjectSummary) -> None:
        self._project = project

    def get(self, name: str) -> ProjectSummary | None:
        return self._project if name == self._project.name else None


def _verzoek(project_naam: str) -> Request:
    """Een echt Request, want het sjabloon wordt ook echt gerenderd."""
    from opi.server import app

    scope = {
        "type": "http",
        "method": "GET",
        "path": f"/projects/{project_naam}/details",
        "raw_path": f"/projects/{project_naam}/details".encode(),
        "query_string": b"",
        "headers": [],
        "app": app,
        "router": app.router,
        "session": {},
        "state": {},
    }
    verzoek = Request(scope)
    verzoek.state.user = {"email": "lid@rijksoverheid.nl", "name": "Lid"}
    # Wat CSRFMiddleware normaal op elk verzoek zet.
    verzoek.state.csrf_token = "csrf-van-de-toets"
    return verzoek


@pytest.mark.parametrize("rol", ["member", "developer"])
async def test_een_lid_krijgt_de_geheimen_niet(monkeypatch: pytest.MonkeyPatch, rol: str) -> None:
    context, html, project_prive = await _pagina(monkeypatch, rol)

    assert context["project"]["config"]["api-key"] is None
    assert context["project"]["config"]["age-private-key"] is None
    for geheim in (API_KEY, KEYCLOAK_WACHTWOORD, TOTP_SEED, project_prive):
        assert geheim not in html
        assert geheim not in _contextwaarden(context), "staat nog ergens in de rendercontext"

    # De OTP-code is afgeleid van de seed, dus een zoektocht op tekst vindt hem niet.
    realms = _realms(context)
    assert realms, "de rolpoort van realm_block hoort weggepatcht te zijn"
    for realm in realms:
        assert realm["password"] is None
        assert realm["totp_code"] is None


@pytest.mark.parametrize("rol", ["admin", "owner"])
async def test_een_eigenaar_krijgt_ze_wel(monkeypatch: pytest.MonkeyPatch, rol: str) -> None:
    context, html, project_prive = await _pagina(monkeypatch, rol)

    assert context["project"]["config"]["api-key"] == API_KEY
    assert context["project"]["config"]["age-private-key"] == project_prive
    assert API_KEY in html
    realm = _realms(context)[0]
    assert realm["password"] == KEYCLOAK_WACHTWOORD
    assert re.fullmatch(r"\d{6}", realm["totp_code"])
    assert realm["totp_secret"] is None, "de seed blijft op de server"


def _realms(context: dict[str, Any]) -> list[dict[str, Any]]:
    """De realms zoals de dienst Keycloak ze in zijn eigen blok meegeeft."""
    for sectie in context["service_detail_sections"]:
        realms = sectie.context.get("realms")
        if realms:
            return realms
    return []
