"""Elke route die een verse step-up vraagt draagt hem ook (RC-332, RC-377, RC-387).

Drie soorten: een verwijderroute die productie kan raken, sinds RC-377 elke beheerroute
die iets verandert, en sinds RC-387 elke handeling die een token of een lidmaatschap
geeft of afneemt.

Een eigen lijst en niet de route-enumeratie van RC-295: die is nog niet gemerged. Komt hij
binnen, dan kan deze test erop aansluiten.

Het tweede deel toetst de documentatie: een client hoort de header en de 403 in de
OpenAPI te vinden in plaats van ze te ontdekken door een aanroep te verliezen.
"""

from __future__ import annotations

from typing import Any

import pytest
from opi.api.otp_step_up import OTP_CODE_HEADER, STEP_UP_PROBLEM_MEDIA_TYPE
from opi.core.auth_decorators import REQUIRES_STEP_UP
from opi.core.step_up import StepUpActie
from opi.server import create_app

#: (methode, pad) -> de handeling die de route verklaart.
ROUTES_MET_STEP_UP = {
    ("POST", "/projects/delete/{project_name}"): StepUpActie.PROJECT_VERWIJDEREN,
    ("POST", "/projects/{project_name}/delete-deployment/{deployment_name}"): StepUpActie.DEPLOYMENT_VERWIJDEREN,
    ("POST", "/projects/{project_name}/delete-component/{component_name}"): StepUpActie.COMPONENT_VERWIJDEREN,
    ("DELETE", "/api/projects/{project_name}"): StepUpActie.PROJECT_VERWIJDEREN,
    ("DELETE", "/api/projects/{project_name}/{deployment_name}"): StepUpActie.DEPLOYMENT_VERWIJDEREN,
    ("DELETE", "/api/v2/projects/{project_name}/{deployment_name}"): StepUpActie.DEPLOYMENT_VERWIJDEREN,
    ("DELETE", "/api/v2/projects/{project_name}/components/{component_name}"): StepUpActie.COMPONENT_VERWIJDEREN,
    ("POST", "/api/projects/{project_name}/:upsert-deployment"): StepUpActie.MARKERING_VERLAGEN,
    ("POST", "/api/v2/projects/{project_name}/:upsert-deployment"): StepUpActie.MARKERING_VERLAGEN,
    # Platformbeheer (RC-377): elke beheerroute die iets verandert. De twee leesroutes
    # (GET /marked-for-deletion en GET /orphans/report) staan er met opzet niet bij.
    ("POST", "/api/v2/admin/cleanup/trigger"): StepUpActie.OPRUIMING_STARTEN,
    ("POST", "/api/v2/admin/reconciliation/trigger"): StepUpActie.RECONCILIATIE_STARTEN,
    ("DELETE", "/api/v2/admin/marked-for-deletion/{mark_id}"): StepUpActie.MARKERING_INTREKKEN,
    ("POST", "/api/v2/admin/orphans/confirm"): StepUpActie.WEZEN_BEVESTIGEN,
    ("POST", "/api/v2/admin/projects/:reconcile"): StepUpActie.PROJECTEN_HERINLEZEN,
    ("POST", "/api/v2/projects/{project_name}/:rotate-project-key"): StepUpActie.PROJECTSLEUTEL_VERVANGEN,
    # Leden en tokens (RC-387): elke handeling die een bewijsmiddel geeft of afneemt, in
    # de API en in het portaal. De leesroute (GET .../leden) staat er met opzet niet bij.
    ("POST", "/api/v2/projects/{project_name}/leden"): StepUpActie.SERVICE_ACCOUNT_TOEVOEGEN,
    ("DELETE", "/api/v2/projects/{project_name}/leden/{naam}"): StepUpActie.SERVICE_ACCOUNT_VERWIJDEREN,
    ("PUT", "/api/v2/projects/{project_name}/leden/{naam}/rol"): StepUpActie.ROL_WIJZIGEN,
    ("POST", "/api/v2/projects/{project_name}/leden/{naam}/tokens"): StepUpActie.TOKEN_UITGEVEN,
    ("POST", "/api/v2/projects/{project_name}/leden/{naam}/tokens/{tokennaam}/:roteer"): StepUpActie.TOKEN_ROTEREN,
    ("DELETE", "/api/v2/projects/{project_name}/leden/{naam}/tokens/{tokennaam}"): StepUpActie.TOKEN_INTREKKEN,
    ("POST", "/projects/{project_name}/leden/toevoegen"): StepUpActie.SERVICE_ACCOUNT_TOEVOEGEN,
    ("POST", "/projects/{project_name}/leden/{naam}/verwijderen"): StepUpActie.SERVICE_ACCOUNT_VERWIJDEREN,
    ("POST", "/projects/{project_name}/leden/{naam}/rol"): StepUpActie.ROL_WIJZIGEN,
    ("POST", "/projects/{project_name}/leden/{naam}/tokens"): StepUpActie.TOKEN_UITGEVEN,
    ("POST", "/projects/{project_name}/leden/{naam}/tokens/{tokennaam}/roteren"): StepUpActie.TOKEN_ROTEREN,
    ("POST", "/projects/{project_name}/leden/{naam}/tokens/{tokennaam}/intrekken"): StepUpActie.TOKEN_INTREKKEN,
}

#: De routes die ALLEEN lezen en dus geen step-up dragen (RC-377, RC-387).
BEHEERROUTES_ZONDER_STEP_UP = {
    ("GET", "/api/v2/admin/marked-for-deletion"),
    ("GET", "/api/v2/admin/orphans/report"),
    ("GET", "/api/v2/projects/{project_name}/leden"),
}


@pytest.fixture(scope="module")
def app() -> Any:
    return create_app()


def test_elke_route_uit_de_lijst_draagt_de_step_up(app: Any) -> None:
    gevonden = {
        (methode, route.path): getattr(route.endpoint, REQUIRES_STEP_UP, None)
        for route in app.routes
        if hasattr(route, "methods") and hasattr(route, "endpoint")
        for methode in route.methods
    }
    zonder = [sleutel for sleutel in ROUTES_MET_STEP_UP if gevonden.get(sleutel) is not ROUTES_MET_STEP_UP[sleutel]]
    assert zonder == [], (
        "Deze routes raken productie maar verklaren geen step-up meer. Zet "
        "@requires_step_up terug, of pas de lijst aan als de route verhuisd is:\n  "
        + "\n  ".join(f"{m} {p}" for m, p in zonder)
    )


def test_een_leesroute_van_platformbeheer_draagt_geen_step_up(app: Any) -> None:
    """Alleen lezen vraagt alleen het token: een OTP erop zou een handeling verzinnen."""
    met_step_up = [
        (methode, route.path)
        for route in app.routes
        if hasattr(route, "methods") and hasattr(route, "endpoint")
        for methode in route.methods
        if (methode, route.path) in BEHEERROUTES_ZONDER_STEP_UP
        and getattr(route.endpoint, REQUIRES_STEP_UP, None) is not None
    ]
    assert met_step_up == [], f"Deze leesroutes vragen een step-up: {met_step_up}"


def test_elke_step_up_route_in_de_api_is_gedocumenteerd(app: Any) -> None:
    """De header en de 403 staan in de OpenAPI, afgeleid uit de markering op de route.

    Portaalroutes staan niet in dit document: die wisselen zichzelf in voor een dialoog.
    """
    document = app.openapi()
    assert "StepUpProblem" in document["components"]["schemas"]

    ongedocumenteerd = []
    for methode, pad in ROUTES_MET_STEP_UP:
        if not pad.startswith("/api/"):
            continue
        operatie = document["paths"][pad][methode.lower()]
        headers = {p["name"] for p in operatie.get("parameters", []) if p.get("in") == "header"}
        antwoord = operatie.get("responses", {}).get("403", {})
        schema = antwoord.get("content", {}).get(STEP_UP_PROBLEM_MEDIA_TYPE, {}).get("schema", {})
        if OTP_CODE_HEADER not in headers or schema.get("$ref") != "#/components/schemas/StepUpProblem":
            ongedocumenteerd.append(f"{methode} {pad}")

    assert ongedocumenteerd == [], (
        f"Deze step-up-routes documenteren '{OTP_CODE_HEADER}' of de 403 niet. Een client die op "
        "dit document afgaat verliest daardoor een aanroep zonder te weten waarom:\n  " + "\n  ".join(ongedocumenteerd)
    )
