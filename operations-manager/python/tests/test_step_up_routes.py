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
from opi.api.otp_step_up import STEP_UP_PROBLEM_MEDIA_TYPE
from opi.core.auth_decorators import REQUIRES_STEP_UP
from opi.core.step_up import StepUpAction
from opi.server import create_app

#: (methode, pad) -> de handeling die de route verklaart.
ROUTES_MET_STEP_UP = {
    ("POST", "/projects/delete/{project_name}"): StepUpAction.PROJECT_DELETE,
    ("POST", "/projects/{project_name}/delete-deployment/{deployment_name}"): StepUpAction.DEPLOYMENT_DELETE,
    ("POST", "/projects/{project_name}/delete-component/{component_name}"): StepUpAction.COMPONENT_DELETE,
    ("DELETE", "/api/projects/{project_name}"): StepUpAction.PROJECT_DELETE,
    ("DELETE", "/api/projects/{project_name}/{deployment_name}"): StepUpAction.DEPLOYMENT_DELETE,
    ("DELETE", "/api/v2/projects/{project_name}/{deployment_name}"): StepUpAction.DEPLOYMENT_DELETE,
    ("DELETE", "/api/v2/projects/{project_name}/components/{component_name}"): StepUpAction.COMPONENT_DELETE,
    ("POST", "/api/projects/{project_name}/:upsert-deployment"): StepUpAction.MARKING_SET,
    ("POST", "/api/v2/projects/{project_name}/:upsert-deployment"): StepUpAction.MARKING_SET,
    # Platformbeheer (RC-377): elke beheerroute die iets verandert. De twee leesroutes
    # (GET /marked-for-deletion en GET /orphans/report) staan er met opzet niet bij.
    ("POST", "/api/v2/admin/cleanup/trigger"): StepUpAction.CLEANUP_START,
    ("POST", "/api/v2/admin/reconciliation/trigger"): StepUpAction.RECONCILIATION_START,
    ("DELETE", "/api/v2/admin/marked-for-deletion/{mark_id}"): StepUpAction.MARKING_REVOKE,
    ("POST", "/api/v2/admin/orphans/confirm"): StepUpAction.ORPHANS_CONFIRM,
    ("POST", "/api/v2/admin/projects/:reconcile"): StepUpAction.PROJECTS_RELOAD,
    ("POST", "/api/v2/projects/{project_name}/:rotate-project-key"): StepUpAction.PROJECT_KEY_REPLACE,
    # Leden en tokens (RC-387): elke handeling die een bewijsmiddel geeft of afneemt, in
    # de API en in het portaal. De leesroute (GET .../leden) staat er met opzet niet bij.
    ("POST", "/api/v2/projects/{project_name}/leden"): StepUpAction.SERVICE_ACCOUNT_ADD,
    ("DELETE", "/api/v2/projects/{project_name}/leden/{naam}"): StepUpAction.SERVICE_ACCOUNT_REMOVE,
    ("PUT", "/api/v2/projects/{project_name}/leden/{naam}/rol"): StepUpAction.ROLE_CHANGE,
    ("POST", "/api/v2/projects/{project_name}/leden/{naam}/tokens"): StepUpAction.TOKEN_ISSUE,
    ("POST", "/api/v2/projects/{project_name}/leden/{naam}/tokens/{tokennaam}/:roteer"): StepUpAction.TOKEN_ROTATE,
    ("DELETE", "/api/v2/projects/{project_name}/leden/{naam}/tokens/{tokennaam}"): StepUpAction.TOKEN_REVOKE,
    ("POST", "/projects/{project_name}/leden/toevoegen"): StepUpAction.SERVICE_ACCOUNT_ADD,
    ("POST", "/projects/{project_name}/leden/{naam}/verwijderen"): StepUpAction.SERVICE_ACCOUNT_REMOVE,
    ("POST", "/projects/{project_name}/leden/{naam}/rol"): StepUpAction.ROLE_CHANGE,
    ("POST", "/projects/{project_name}/leden/{naam}/tokens"): StepUpAction.TOKEN_ISSUE,
    ("POST", "/projects/{project_name}/leden/{naam}/tokens/{tokennaam}/roteren"): StepUpAction.TOKEN_ROTATE,
    ("POST", "/projects/{project_name}/leden/{naam}/tokens/{tokennaam}/intrekken"): StepUpAction.TOKEN_REVOKE,
    # Omgevingsvariabelen (RC-388): al het lezen en schrijven van waarden, dus ook de
    # GET die het venster opent.
    ("GET", "/projects/{project_name}/componenten/{component_name}/env-vars"): StepUpAction.ENV_VARS_MANAGE,
    ("POST", "/projects/{project_name}/componenten/{component_name}/env-vars"): StepUpAction.ENV_VARS_MANAGE,
    (
        "GET",
        "/projects/{project_name}/deployments/{deployment_name}/componenten/{component_name}/env-vars",
    ): StepUpAction.ENV_VARS_MANAGE,
    (
        "POST",
        "/projects/{project_name}/deployments/{deployment_name}/componenten/{component_name}/env-vars",
    ): StepUpAction.ENV_VARS_MANAGE,
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
        antwoord = operatie.get("responses", {}).get("403", {})
        schema = antwoord.get("content", {}).get(STEP_UP_PROBLEM_MEDIA_TYPE, {}).get("schema", {})
        if schema.get("$ref") != "#/components/schemas/StepUpProblem":
            ongedocumenteerd.append(f"{methode} {pad}")

    assert ongedocumenteerd == [], (
        "Deze step-up-routes documenteren de 403 niet. Een client die op "
        "dit document afgaat verliest daardoor een aanroep zonder te weten waarom:\n  " + "\n  ".join(ongedocumenteerd)
    )
