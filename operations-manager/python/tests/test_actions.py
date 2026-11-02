"""Elke token-bereikbare route noemt een handeling, en de oude sleutel wordt er niet ruimer van.

WAAROM DEZE TEST BESTAAT

Een scope die een lijst PADEN noemt veroudert bij de eerstvolgende route. Een scope die
HANDELINGEN noemt niet, want een nieuwe route moet een handeling kiezen. Dat werkt alleen
als "een route zonder handeling" onmogelijk is, en dat is wat hier wordt vastgelegd:

1. de opstartcontrole faalt op een token-bereikbare route zonder handeling;
2. elke action die een route vraagt heeft een regel in het register, en een leesrol komt
   niet verder dan de leesacties.

Punt 2 is de plek waar een verruiming binnensluipt: een action die per ongeluk met
``read=True`` in het register staat opent voor iedereen.
"""

from __future__ import annotations

from typing import ClassVar

import pytest
from opi.api.endpoint_util import REQUIRES_PROJECT_NAME, controleer_handelingen
from opi.api.user_token_auth import REQUIRES_USER_TOKEN
from opi.core.auth_decorators import REQUIRES_STEP_UP
from opi.core.authz.actions import ACTION_ATTR, Action, action, action_of
from opi.core.authz.engine import Principal, PrincipalKind, Resource, Role, RuleError, all_rules, decide, rule_for
from opi.core.step_up import StepUpAction
from opi.server import app
from opi.services.project_roles import roles_from_value

#: Welke handeling elke route achter de PROJECTSLEUTEL vraagt. Plan stap 5: niet alleen de
#: verzameling handelingen staat vast maar de koppeling per route, want die koppeling IS de
#: grens tussen een admin- en een developer-token. Zonder deze tabel kon
#: ``DELETE /api/projects/{project_name}`` van ``project.delete`` naar ``project.read``
#: gezet worden zonder dat een poort rood werd, en dan verwijdert een developer een project.
#:
#: Loopt dit uit de pas, dan is er een route bij gekomen, verdwenen, of van handeling
#: veranderd. Verander deze tabel alleen samen met die route, en kijk dan of de nieuwe
#: handeling bij de methode past: een route die iets WIJZIGT hoort geen leeshandeling te
#: vragen (``test_een_leesrol_komt_niet_verder_dan_lezen`` legt die kant vast).
SLEUTELROUTES: dict[tuple[str, str], Action] = {
    ("GET", "/api/logs/{project_name}"): Action.LOGS_READ,
    ("DELETE", "/api/projects/{project_name}"): Action.PROJECT_DELETE,
    ("GET", "/api/projects/{project_name}/:refresh"): Action.PROJECT_REFRESH,
    ("POST", "/api/projects/{project_name}/:upsert-deployment"): Action.DEPLOYMENT_WRITE,
    ("POST", "/api/projects/{project_name}/components"): Action.COMPONENT_ADD,
    ("PATCH", "/api/projects/{project_name}/components/{component_name}"): Action.COMPONENT_UPDATE,
    (
        "POST",
        "/api/projects/{project_name}/deployments/{deployment_name}/:clone-bucket-from-external",
    ): Action.BUCKET_CLONE,
    (
        "POST",
        "/api/projects/{project_name}/deployments/{deployment_name}/:clone-database-from-external",
    ): Action.DATABASE_CLONE,
    ("GET", "/api/projects/{project_name}/deployments/{deployment_name}/:refresh"): Action.DEPLOYMENT_REFRESH,
    ("POST", "/api/projects/{project_name}/deployments/{deployment_name}/:validate-clone"): Action.CLONE_VALIDATE,
    ("POST", "/api/projects/{project_name}/deployments/{deployment_name}/components"): Action.COMPONENT_LINK,
    ("PUT", "/api/projects/{project_name}/deployments/{deployment_name}/image"): Action.DEPLOYMENT_IMAGE_REPLACE,
    ("POST", "/api/projects/{project_name}/registries/by-credentials"): Action.REGISTRY_ADD,
    ("POST", "/api/projects/{project_name}/registries/by-secret"): Action.REGISTRY_ADD,
    ("POST", "/api/projects/{project_name}/services"): Action.SERVICE_ADD,
    ("DELETE", "/api/projects/{project_name}/{deployment_name}"): Action.DEPLOYMENT_DELETE,
    ("POST", "/api/resources/{project_name}/sanitize"): Action.RESOURCES_CLEANUP,
    ("POST", "/api/resources/{project_name}/tune"): Action.RESOURCES_ADJUST,
    ("GET", "/api/subdomains"): Action.SUBDOMAIN_READ,
    ("POST", "/api/v1/backup/project/{project_name}/deployment/{deployment_name}"): Action.BACKUP_CREATE,
    ("GET", "/api/v1/backup/runs/{project_name}/{deployment_name}"): Action.BACKUP_READ,
    ("DELETE", "/api/v1/backup/snapshot/{project_name}/{deployment_name}/{snapshot_id}"): Action.BACKUP_DELETE,
    ("POST", "/api/v1/projects/{project_name}/images/push"): Action.IMAGE_PUSH,
    ("POST", "/api/v1/restore/bucket/{cluster}/{namespace}/{reference_name}"): Action.RESTORE_RUN,
    ("POST", "/api/v1/restore/database/{cluster}/{namespace}/{reference_name}"): Action.RESTORE_RUN,
    ("POST", "/api/v1/restore/project/{project_name}"): Action.RESTORE_RUN,
    ("POST", "/api/v1/restore/project/{project_name}/deployment/{deployment_name}"): Action.RESTORE_RUN,
    (
        "POST",
        "/api/v1/restore/project/{project_name}/deployment/{deployment_name}/run/{backup_run_id}",
    ): Action.RESTORE_RUN,
    ("POST", "/api/v1/restore/pvc/{cluster}/{namespace}/{pvc_name}"): Action.RESTORE_RUN,
    ("GET", "/api/v1/restore/snapshots/{cluster}/{namespace}"): Action.RESTORE_READ,
    ("GET", "/api/v1/restore/snapshots/{cluster}/{namespace}/{pvc_name}"): Action.RESTORE_READ,
    ("GET", "/api/v2/projects/{project_name}"): Action.PROJECT_READ,
    ("POST", "/api/v2/projects/{project_name}/:refresh"): Action.PROJECT_REFRESH,
    ("POST", "/api/v2/projects/{project_name}/:upsert-deployment"): Action.DEPLOYMENT_WRITE,
    ("GET", "/api/v2/projects/{project_name}/clusters"): Action.CLUSTER_READ,
    ("GET", "/api/v2/projects/{project_name}/components"): Action.COMPONENT_READ,
    ("POST", "/api/v2/projects/{project_name}/components"): Action.COMPONENT_ADD,
    ("DELETE", "/api/v2/projects/{project_name}/components/{component_name}"): Action.COMPONENT_DELETE,
    ("PATCH", "/api/v2/projects/{project_name}/components/{component_name}"): Action.COMPONENT_UPDATE,
    ("GET", "/api/v2/projects/{project_name}/deployments"): Action.DEPLOYMENT_READ,
    ("GET", "/api/v2/projects/{project_name}/deployments/{deployment_name}"): Action.DEPLOYMENT_READ,
    ("POST", "/api/v2/projects/{project_name}/deployments/{deployment_name}/:clone-bucket"): Action.BUCKET_CLONE,
    (
        "POST",
        "/api/v2/projects/{project_name}/deployments/{deployment_name}/:clone-database",
    ): Action.DATABASE_CLONE,
    ("POST", "/api/v2/projects/{project_name}/deployments/{deployment_name}/:refresh"): Action.DEPLOYMENT_REFRESH,
    (
        "POST",
        "/api/v2/projects/{project_name}/deployments/{deployment_name}/:rotate-credentials",
    ): Action.SERVICE_ACTION,
    ("POST", "/api/v2/projects/{project_name}/deployments/{deployment_name}/components"): Action.COMPONENT_LINK,
    (
        "PUT",
        "/api/v2/projects/{project_name}/deployments/{deployment_name}/image",
    ): Action.DEPLOYMENT_IMAGE_REPLACE,
    ("GET", "/api/v2/projects/{project_name}/pending-rollout"): Action.PROJECT_READ,
    ("GET", "/api/v2/projects/{project_name}/services"): Action.SERVICE_READ,
    ("POST", "/api/v2/projects/{project_name}/services"): Action.SERVICE_ADD,
    (
        "DELETE",
        "/api/v2/projects/{project_name}/services/aliases/values/component/{component_name}",
    ): Action.SERVICE_WRITE_VALUES,
    (
        "GET",
        "/api/v2/projects/{project_name}/services/aliases/values/component/{component_name}",
    ): Action.SERVICE_READ,
    (
        "PATCH",
        "/api/v2/projects/{project_name}/services/aliases/values/component/{component_name}",
    ): Action.SERVICE_WRITE_VALUES,
    (
        "POST",
        "/api/v2/projects/{project_name}/services/aliases/values/component/{component_name}",
    ): Action.SERVICE_WRITE_VALUES,
    (
        "POST",
        "/api/v2/projects/{project_name}/services/aliases/values/component/{component_name}/:delete",
    ): Action.SERVICE_WRITE_VALUES,
    (
        "DELETE",
        "/api/v2/projects/{project_name}/services/aliases/values/component/{component_name}/{value_key}",
    ): Action.SERVICE_WRITE_VALUES,
    ("POST", "/api/v2/projects/{project_name}/services/attachments/attachment"): Action.SERVICE_ACTION,
    (
        "DELETE",
        "/api/v2/projects/{project_name}/services/attachments/attachment/{attachment_id}",
    ): Action.SERVICE_ACTION,
    ("PUT", "/api/v2/projects/{project_name}/services/attachments/attachment/{attachment_id}"): Action.SERVICE_ACTION,
    (
        "POST",
        "/api/v2/projects/{project_name}/services/attachments/component/{component_name}/attachment",
    ): Action.SERVICE_ACTION,
    (
        "PUT",
        "/api/v2/projects/{project_name}/services/attachments/component/{component_name}/attachment/{attachment_id}",
    ): Action.SERVICE_ACTION,
    (
        "DELETE",
        "/api/v2/projects/{project_name}/services/attachments/config/component/{component_name}",
    ): Action.SERVICE_CONFIGURE,
    (
        "PATCH",
        "/api/v2/projects/{project_name}/services/attachments/config/component/{component_name}",
    ): Action.SERVICE_CONFIGURE,
    (
        "PUT",
        "/api/v2/projects/{project_name}/services/attachments/config/component/{component_name}",
    ): Action.SERVICE_CONFIGURE,
    (
        "DELETE",
        "/api/v2/projects/{project_name}/services/authorization-wall/config/project",
    ): Action.SERVICE_CONFIGURE,
    (
        "PUT",
        "/api/v2/projects/{project_name}/services/authorization-wall/config/project",
    ): Action.SERVICE_CONFIGURE,
    (
        "DELETE",
        "/api/v2/projects/{project_name}/services/cross-domain-access/config/deployment/{deployment_name}",
    ): Action.SERVICE_CONFIGURE,
    (
        "PUT",
        "/api/v2/projects/{project_name}/services/cross-domain-access/config/deployment/{deployment_name}",
    ): Action.SERVICE_CONFIGURE,
    (
        "PATCH",
        "/api/v2/projects/{project_name}/services/cross-domain-access/config/deployment/{deployment_name}/inbound",
    ): Action.SERVICE_CONFIGURE,
    (
        "PATCH",
        "/api/v2/projects/{project_name}/services/cross-domain-access/config/deployment/{deployment_name}/outbound",
    ): Action.SERVICE_CONFIGURE,
    (
        "DELETE",
        "/api/v2/projects/{project_name}/services/cross-domain-access/config/project",
    ): Action.SERVICE_CONFIGURE,
    (
        "PUT",
        "/api/v2/projects/{project_name}/services/cross-domain-access/config/project",
    ): Action.SERVICE_CONFIGURE,
    (
        "PATCH",
        "/api/v2/projects/{project_name}/services/cross-domain-access/config/project/inbound",
    ): Action.SERVICE_CONFIGURE,
    (
        "PATCH",
        "/api/v2/projects/{project_name}/services/cross-domain-access/config/project/outbound",
    ): Action.SERVICE_CONFIGURE,
    (
        "DELETE",
        "/api/v2/projects/{project_name}/services/health-check/config/component/{component_name}",
    ): Action.SERVICE_CONFIGURE,
    (
        "PUT",
        "/api/v2/projects/{project_name}/services/health-check/config/component/{component_name}",
    ): Action.SERVICE_CONFIGURE,
    (
        "DELETE",
        "/api/v2/projects/{project_name}/services/image-registries/config/component/{component_name}",
    ): Action.SERVICE_CONFIGURE,
    (
        "PUT",
        "/api/v2/projects/{project_name}/services/image-registries/config/component/{component_name}",
    ): Action.SERVICE_CONFIGURE,
    (
        "DELETE",
        "/api/v2/projects/{project_name}/services/image-registries/config/project",
    ): Action.SERVICE_CONFIGURE,
    ("PUT", "/api/v2/projects/{project_name}/services/image-registries/config/project"): Action.SERVICE_CONFIGURE,
    ("DELETE", "/api/v2/projects/{project_name}/services/invite/config/project"): Action.SERVICE_CONFIGURE,
    ("PUT", "/api/v2/projects/{project_name}/services/invite/config/project"): Action.SERVICE_CONFIGURE,
    ("PATCH", "/api/v2/projects/{project_name}/services/invite/config/project/active"): Action.SERVICE_CONFIGURE,
    ("DELETE", "/api/v2/projects/{project_name}/services/keycloak/config/project"): Action.SERVICE_CONFIGURE,
    ("PUT", "/api/v2/projects/{project_name}/services/keycloak/config/project"): Action.SERVICE_CONFIGURE,
    (
        "DELETE",
        "/api/v2/projects/{project_name}/services/metrics-scraper/config/component/{component_name}",
    ): Action.SERVICE_CONFIGURE,
    (
        "PUT",
        "/api/v2/projects/{project_name}/services/metrics-scraper/config/component/{component_name}",
    ): Action.SERVICE_CONFIGURE,
    (
        "DELETE",
        "/api/v2/projects/{project_name}/services/minio-storage/config/deployment/{deployment_name}",
    ): Action.SERVICE_CONFIGURE,
    (
        "PUT",
        "/api/v2/projects/{project_name}/services/minio-storage/config/deployment/{deployment_name}",
    ): Action.SERVICE_CONFIGURE,
    ("DELETE", "/api/v2/projects/{project_name}/services/minio-storage/config/project"): Action.SERVICE_CONFIGURE,
    ("PUT", "/api/v2/projects/{project_name}/services/minio-storage/config/project"): Action.SERVICE_CONFIGURE,
    (
        "DELETE",
        "/api/v2/projects/{project_name}/services/namespace-postgresql-database/config/project",
    ): Action.SERVICE_CONFIGURE,
    (
        "PUT",
        "/api/v2/projects/{project_name}/services/namespace-postgresql-database/config/project",
    ): Action.SERVICE_CONFIGURE,
    (
        "DELETE",
        "/api/v2/projects/{project_name}/services/persistent-storage/config/component/{component_name}",
    ): Action.SERVICE_CONFIGURE,
    (
        "PATCH",
        "/api/v2/projects/{project_name}/services/persistent-storage/config/component/{component_name}",
    ): Action.SERVICE_CONFIGURE,
    (
        "PUT",
        "/api/v2/projects/{project_name}/services/persistent-storage/config/component/{component_name}",
    ): Action.SERVICE_CONFIGURE,
    (
        "DELETE",
        "/api/v2/projects/{project_name}/services/postgresql-database/config/deployment/{deployment_name}",
    ): Action.SERVICE_CONFIGURE,
    (
        "PUT",
        "/api/v2/projects/{project_name}/services/postgresql-database/config/deployment/{deployment_name}",
    ): Action.SERVICE_CONFIGURE,
    (
        "DELETE",
        "/api/v2/projects/{project_name}/services/postgresql-database/config/project",
    ): Action.SERVICE_CONFIGURE,
    (
        "PUT",
        "/api/v2/projects/{project_name}/services/postgresql-database/config/project",
    ): Action.SERVICE_CONFIGURE,
    ("GET", "/api/v2/projects/{project_name}/services/postgresql-database/schemas"): Action.DATABASE_SCHEMA_READ,
    ("POST", "/api/v2/projects/{project_name}/services/postgresql-database/schemas"): Action.DATABASE_SCHEMA_MANAGE,
    (
        "DELETE",
        "/api/v2/projects/{project_name}/services/postgresql-database/schemas/{postfix}",
    ): Action.DATABASE_SCHEMA_MANAGE,
    (
        "DELETE",
        "/api/v2/projects/{project_name}/services/publish-on-web/config/component/{component_name}",
    ): Action.SERVICE_CONFIGURE,
    (
        "PUT",
        "/api/v2/projects/{project_name}/services/publish-on-web/config/component/{component_name}",
    ): Action.SERVICE_CONFIGURE,
    (
        "DELETE",
        "/api/v2/projects/{project_name}/services/publish-on-web/config/deployment/{deployment_name}",
    ): Action.SERVICE_CONFIGURE,
    (
        "PUT",
        "/api/v2/projects/{project_name}/services/publish-on-web/config/deployment/{deployment_name}",
    ): Action.SERVICE_CONFIGURE,
    ("DELETE", "/api/v2/projects/{project_name}/services/redis/config/project"): Action.SERVICE_CONFIGURE,
    ("PUT", "/api/v2/projects/{project_name}/services/redis/config/project"): Action.SERVICE_CONFIGURE,
    (
        "DELETE",
        "/api/v2/projects/{project_name}/services/send-email/config/deployment/{deployment_name}",
    ): Action.SERVICE_CONFIGURE,
    (
        "PUT",
        "/api/v2/projects/{project_name}/services/send-email/config/deployment/{deployment_name}",
    ): Action.SERVICE_CONFIGURE,
    ("DELETE", "/api/v2/projects/{project_name}/services/send-email/config/project"): Action.SERVICE_CONFIGURE,
    ("PUT", "/api/v2/projects/{project_name}/services/send-email/config/project"): Action.SERVICE_CONFIGURE,
    (
        "DELETE",
        "/api/v2/projects/{project_name}/services/seaweedfs-storage/config/deployment/{deployment_name}",
    ): Action.SERVICE_CONFIGURE,
    (
        "PUT",
        "/api/v2/projects/{project_name}/services/seaweedfs-storage/config/deployment/{deployment_name}",
    ): Action.SERVICE_CONFIGURE,
    (
        "DELETE",
        "/api/v2/projects/{project_name}/services/seaweedfs-storage/config/project",
    ): Action.SERVICE_CONFIGURE,
    (
        "PUT",
        "/api/v2/projects/{project_name}/services/seaweedfs-storage/config/project",
    ): Action.SERVICE_CONFIGURE,
    ("DELETE", "/api/v2/projects/{project_name}/services/sleep-mode/config/project"): Action.SERVICE_CONFIGURE,
    ("PUT", "/api/v2/projects/{project_name}/services/sleep-mode/config/project"): Action.SERVICE_CONFIGURE,
    (
        "PATCH",
        "/api/v2/projects/{project_name}/services/sleep-mode/config/project/match",
    ): Action.SERVICE_CONFIGURE,
    (
        "DELETE",
        "/api/v2/projects/{project_name}/services/temp-storage/config/component/{component_name}",
    ): Action.SERVICE_CONFIGURE,
    (
        "PATCH",
        "/api/v2/projects/{project_name}/services/temp-storage/config/component/{component_name}",
    ): Action.SERVICE_CONFIGURE,
    (
        "PUT",
        "/api/v2/projects/{project_name}/services/temp-storage/config/component/{component_name}",
    ): Action.SERVICE_CONFIGURE,
    (
        "DELETE",
        "/api/v2/projects/{project_name}/services/user-env-vars/values/component/{component_name}",
    ): Action.SERVICE_WRITE_VALUES,
    (
        "GET",
        "/api/v2/projects/{project_name}/services/user-env-vars/values/component/{component_name}",
    ): Action.SERVICE_READ,
    (
        "PATCH",
        "/api/v2/projects/{project_name}/services/user-env-vars/values/component/{component_name}",
    ): Action.SERVICE_WRITE_VALUES,
    (
        "POST",
        "/api/v2/projects/{project_name}/services/user-env-vars/values/component/{component_name}",
    ): Action.SERVICE_WRITE_VALUES,
    (
        "POST",
        "/api/v2/projects/{project_name}/services/user-env-vars/values/component/{component_name}/:delete",
    ): Action.SERVICE_WRITE_VALUES,
    (
        "DELETE",
        "/api/v2/projects/{project_name}/services/user-env-vars/values/component/{component_name}/{value_key}",
    ): Action.SERVICE_WRITE_VALUES,
    (
        "DELETE",
        "/api/v2/projects/{project_name}/services/user-env-vars/values/deployment/{deployment_name}/component/{component_name}",
    ): Action.SERVICE_WRITE_VALUES,
    (
        "GET",
        "/api/v2/projects/{project_name}/services/user-env-vars/values/deployment/{deployment_name}/component/{component_name}",
    ): Action.SERVICE_READ,
    (
        "PATCH",
        "/api/v2/projects/{project_name}/services/user-env-vars/values/deployment/{deployment_name}/component/{component_name}",
    ): Action.SERVICE_WRITE_VALUES,
    (
        "POST",
        "/api/v2/projects/{project_name}/services/user-env-vars/values/deployment/{deployment_name}/component/{component_name}",
    ): Action.SERVICE_WRITE_VALUES,
    (
        "POST",
        "/api/v2/projects/{project_name}/services/user-env-vars/values/deployment/{deployment_name}/component/{component_name}/:delete",
    ): Action.SERVICE_WRITE_VALUES,
    (
        "DELETE",
        "/api/v2/projects/{project_name}/services/user-env-vars/values/deployment/{deployment_name}/component/{component_name}/{value_key}",
    ): Action.SERVICE_WRITE_VALUES,
    ("GET", "/api/v2/projects/{project_name}/services/{service_name}/config"): Action.SERVICE_READ,
    ("GET", "/api/v2/projects/{project_name}/subdomains/check/{subdomain}"): Action.SUBDOMAIN_READ,
    ("DELETE", "/api/v2/projects/{project_name}/{deployment_name}"): Action.DEPLOYMENT_DELETE,
}


#: De routers die hun sleutelcontrole met de HAND doen en dus geen markering dragen
#: (inventarisatie paragraaf 5). Ze zijn niet uit de routetabel te vinden, dus ze staan hier.
#: Loopt dit uit de pas, dan is er een handgeschreven controle bij gekomen of verdwenen.
HANDGESCHREVEN = {
    ("GET", "/api/tasks/{task_id}"): Action.TASK_READ,
    ("GET", "/api/tasks"): Action.TASK_READ,
    ("POST", "/api/tasks/{task_id}/:cancel"): Action.TASK_CANCEL,
    ("POST", "/api/sleep-mode/{project_name}/{deployment_name}/wake"): Action.SLEEP_WAKE,
    ("GET", "/api/sleep-mode/{project_name}/{deployment_name}/status"): Action.SLEEP_READ,
}

#: Wat de bearerroutes vragen. Ze nemen een SSO-bearer en geen projectsleutel: bij het
#: projectoverzicht bestaat het project nog niet, en een token hoort geen token te maken.
BEARERROUTES = {
    ("GET", "/api/v2/projects"): Action.PROJECT_LIST,
    ("POST", "/api/v2/projects"): Action.PROJECT_CREATE,
    ("GET", "/api/v2/projects/{project_name}/api-key"): Action.PROJECT_KEY_READ,
    ("GET", "/api/v2/projects/{project_name}/leden"): Action.TOKEN_READ,
    # Een lid toevoegen, weghalen of zijn rollen wijzigen is member.manage; een TOKEN
    # van dat lid beheren is token.manage. Twee rechten, allebei admin met step-up.
    ("POST", "/api/v2/projects/{project_name}/leden"): Action.MEMBER_MANAGE,
    ("DELETE", "/api/v2/projects/{project_name}/leden/{naam}"): Action.MEMBER_MANAGE,
    ("PUT", "/api/v2/projects/{project_name}/leden/{naam}/rol"): Action.MEMBER_MANAGE,
    ("POST", "/api/v2/projects/{project_name}/leden/{naam}/tokens"): Action.TOKEN_MANAGE,
    ("POST", "/api/v2/projects/{project_name}/leden/{naam}/tokens/{tokennaam}/:roteer"): Action.TOKEN_MANAGE,
    ("DELETE", "/api/v2/projects/{project_name}/leden/{naam}/tokens/{tokennaam}"): Action.TOKEN_MANAGE,
}


def _gemarkeerde_routes(markering: str) -> dict[tuple[str, str], Action | None]:
    gevonden: dict[tuple[str, str], Action | None] = {}
    for route in app.routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None or not getattr(endpoint, markering, False):
            continue
        for methode in getattr(route, "methods", None) or []:
            gevonden[(methode, route.path)] = action_of(endpoint)
    return gevonden


def test_elke_sleutelroute_noemt_een_handeling() -> None:
    """Elke route achter de projectsleutel draagt een handeling."""
    zonder = [sleutel for sleutel, h in _gemarkeerde_routes(REQUIRES_PROJECT_NAME).items() if h is None]
    assert not zonder, f"deze routes nemen een projectsleutel maar noemen geen handeling: {zonder}"


def test_elke_sleutelroute_vraagt_de_handeling_uit_de_tabel() -> None:
    """Per route, niet per verzameling: wie een route een ruimere handeling geeft valt hier op."""
    assert _gemarkeerde_routes(REQUIRES_PROJECT_NAME) == SLEUTELROUTES


def test_elke_bearerroute_noemt_een_handeling() -> None:
    assert _gemarkeerde_routes(REQUIRES_USER_TOKEN) == BEARERROUTES


def test_de_handgeschreven_controles_noemen_een_handeling() -> None:
    """De vijf routes die hun sleutel met de hand toetsen, en wat ze vragen.

    Ze dragen geen markering, dus de opstartcontrole vindt ze niet. Deze lijst is wat
    daarvoor in de plaats komt.
    """
    paden: dict[tuple[str, str], Action | None] = {}
    for route in app.routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None:
            continue
        for methode in getattr(route, "methods", None) or []:
            paden[(methode, route.path)] = action_of(endpoint)

    for sleutel, verwacht in HANDGESCHREVEN.items():
        assert paden.get(sleutel) == verwacht, f"{sleutel} hoort {verwacht.value} te vragen"


def test_de_opstartcontrole_laat_de_echte_routetabel_door() -> None:
    controleer_handelingen(app.routes)


def test_de_opstartcontrole_faalt_op_een_route_zonder_handeling() -> None:
    """Dit is de hele waarde van de controle: hij stopt de start in plaats van stil door te laten."""

    async def nieuwe_route(project_name: str) -> None: ...

    setattr(nieuwe_route, REQUIRES_PROJECT_NAME, True)

    class _Route:
        endpoint = nieuwe_route
        path = "/api/v2/projects/{project_name}/iets-nieuws"
        methods: ClassVar[set[str]] = {"POST"}

    with pytest.raises(RuntimeError, match="noemen geen action"):
        controleer_handelingen([_Route()])


def test_elke_route_vraagt_een_handeling_die_een_rol_kan_openen() -> None:
    """Geen route vraagt iets dat geen enkele rol opent; dat zou een dode route zijn."""
    gevraagd = {h for h in _gemarkeerde_routes(REQUIRES_PROJECT_NAME).values() if h is not None}
    gevraagd |= set(HANDGESCHREVEN.values())
    gevraagd |= set(BEARERROUTES.values())

    zonder_regel = []
    for h in sorted(gevraagd, key=str):
        try:
            rule_for(h)
        except RuleError:
            zonder_regel.append(h.value)
    assert not zonder_regel, f"deze actions hebben geen regel in het register: {zonder_regel}"


def test_een_leesrol_komt_niet_verder_dan_lezen() -> None:
    """De rol van het lid bepaalt wat zijn token mag, en een developer wijzigt niets.

    Dezelfde grens die ``require_project_edit_access`` in de interface trekt. Loopt deze
    test rood, dan opent een token meer dan zijn eigenaar op het scherm kan.
    """
    viewer = Principal(kind=PrincipalKind.HUMAN, name="kijker", project="demo", roles=frozenset({Role.VIEWER}))
    resource = Resource(project="demo", path="project/demo")
    # Andersom geformuleerd dan "viewer mag elke leesactie": ``audit.read`` is een
    # leesactie die alleen een admin mag (ontwerp 6.3). Wat vast moet staan is dat een
    # viewer NIETS mag wat geen leesactie is.
    for regel in all_rules():
        if decide(viewer, regel.action, resource, step_up_fresh=True).allowed:
            assert regel.read, f"{regel.action.value} is geen leesactie maar staat wel open voor een viewer"

    assert rule_for(Action.PROJECT_DELETE).read is False
    assert rule_for(Action.RESTORE_RUN).read is False


def test_een_onbekende_rol_opent_niets() -> None:
    """Een rol die iemand in het bestand verzint is geen reden om iets toe te laten."""
    assert roles_from_value("beheerder") == frozenset()


def test_tokens_beheren_vraagt_een_mens() -> None:
    """Een token aanmaken vraagt een geverifieerde persoon, en een pijplijn is er geen."""
    assert all(grant.needs_human for grant in rule_for(Action.TOKEN_MANAGE).grants)
    assert not any(grant.needs_human for grant in rule_for(Action.TOKEN_READ).grants)


#: Welke step-upactie van RC-332 op welke handeling van deze catalogus zit, voor de routes
#: die met een token bereikbaar zijn. De beheerroutes van RC-377 staan er niet bij: die
#: nemen ``validate_platform_admin_token`` en zijn met geen ``zad_``-token te bereiken.
#:
#: Deze tabel bestaat omdat het twee catalogi zijn die hetzelfde LIJKEN. Ze beslissen een
#: andere vraag, en de noot bij ``Requirement.HUMAN_ONLY`` zegt waarom ze niet gelijkgetrokken zijn.
#: Wat wel vast moet staan is de koppeling: komt er een step-up-route bij of verschuift er
#: een handeling, dan valt dat hier op in plaats van bij een aanroeper die hem kwijt is.
STAP_OP_PER_HANDELING = {
    StepUpAction.PROJECT_DELETE: Action.PROJECT_DELETE,
    StepUpAction.DEPLOYMENT_DELETE: Action.DEPLOYMENT_DELETE,
    StepUpAction.COMPONENT_DELETE: Action.COMPONENT_DELETE,
    StepUpAction.MARKING_SET: Action.DEPLOYMENT_WRITE,
}


def test_elke_step_up_route_met_een_token_hoort_bij_zijn_handeling() -> None:
    """De koppeling tussen de twee catalogi, per route."""
    gevonden = {
        getattr(route.endpoint, REQUIRES_STEP_UP): action_of(route.endpoint)
        for route in app.routes
        if getattr(route, "endpoint", None) is not None
        and getattr(route.endpoint, REQUIRES_PROJECT_NAME, False)
        and getattr(route.endpoint, REQUIRES_STEP_UP, None) is not None
    }

    assert gevonden == STAP_OP_PER_HANDELING


#: De action waarvoor de regel hieronder BEWUST niet geldt. Ontwerp paragraaf 4, rij 19:
#: een project verwijderen vraagt altijd een admin met een verse step-up, ook zonder
#: productie. Een pijplijn hoort geen project te kunnen opheffen.
ALTIJD_EEN_MENS = {Action.PROJECT_DELETE}


def test_een_step_up_op_een_doelwit_sluit_een_pijplijn_niet_overal_uit() -> None:
    """Een step-up die het DOELWIT weegt, laat het andere geval open.

    Een deployment op productie vraagt een verse factor, dezelfde op acceptatie niet. Zou
    zo'n action ALLEEN grants hebben die een mens vragen, dan was een pijplijn ook
    uitgesloten van het niet-productiegeval, en dat is een verruiming van de weigering
    die niemand gevraagd heeft. ``ALTIJD_EEN_MENS`` noemt waar dat wel de bedoeling is.
    """
    onvoorwaardelijk = [
        h.value
        for h in set(STAP_OP_PER_HANDELING.values()) - ALTIJD_EEN_MENS
        if all(g.needs_human for g in rule_for(h).grants)
    ]
    assert not onvoorwaardelijk


def test_de_markering_wikkelt_de_functie_niet() -> None:
    """``handeling`` zet een attribuut en geeft dezelfde functie terug.

    Zou hij wikkelen, dan verliest FastAPI de handtekening waar hij zijn parameters uit leest.
    """

    async def route() -> None: ...

    gemarkeerd = action(Action.PROJECT_READ)(route)
    assert gemarkeerd is route
    assert getattr(route, ACTION_ATTR) is Action.PROJECT_READ
