"""Elke token-bereikbare route noemt een handeling, en de oude sleutel wordt er niet ruimer van.

WAAROM DEZE TEST BESTAAT

Een scope die een lijst PADEN noemt veroudert bij de eerstvolgende route. Een scope die
HANDELINGEN noemt niet, want een nieuwe route moet een handeling kiezen. Dat werkt alleen
als "een route zonder handeling" onmogelijk is, en dat is wat hier wordt vastgelegd:

1. de opstartcontrole faalt op een token-bereikbare route zonder handeling;
2. elke rol opent een afgebakende verzameling handelingen, en een leesrol komt niet
   verder dan de leeshandelingen.

Punt 2 is de plek waar een verruiming binnensluipt: een handeling die nergens in
``ROL_HANDELINGEN`` valt opent voor niemand, en een handeling die per ongeluk in
``LEESHANDELINGEN`` belandt opent voor iedereen.
"""

from __future__ import annotations

from typing import ClassVar

import pytest
from opi.api.endpoint_util import REQUIRES_PROJECT_NAME, controleer_handelingen
from opi.api.principaal import ALLE_HANDELINGEN, MENS_VERPLICHT, ROL_HANDELINGEN
from opi.api.user_token_auth import REQUIRES_USER_TOKEN
from opi.core.auth_decorators import REQUIRES_STEP_UP
from opi.core.handelingen import HANDELING_ATTR, LEESHANDELINGEN, Handeling, handeling, handeling_van
from opi.core.step_up import StepUpActie
from opi.server import app

#: Welke handeling elke route achter de PROJECTSLEUTEL vraagt. Plan stap 5: niet alleen de
#: verzameling handelingen staat vast maar de koppeling per route, want die koppeling IS de
#: grens tussen een admin- en een developer-token. Zonder deze tabel kon
#: ``DELETE /api/projects/{project_name}`` van ``project.verwijderen`` naar ``project.lezen``
#: gezet worden zonder dat een poort rood werd, en dan verwijdert een developer een project.
#:
#: Loopt dit uit de pas, dan is er een route bij gekomen, verdwenen, of van handeling
#: veranderd. Verander deze tabel alleen samen met die route, en kijk dan of de nieuwe
#: handeling bij de methode past: een route die iets WIJZIGT hoort geen leeshandeling te
#: vragen (``test_een_leesrol_komt_niet_verder_dan_lezen`` legt die kant vast).
SLEUTELROUTES: dict[tuple[str, str], Handeling] = {
    ("GET", "/api/logs/{project_name}"): Handeling.LOGS_LEZEN,
    ("DELETE", "/api/projects/{project_name}"): Handeling.PROJECT_VERWIJDEREN,
    ("GET", "/api/projects/{project_name}/:refresh"): Handeling.PROJECT_VERVERSEN,
    ("POST", "/api/projects/{project_name}/:upsert-deployment"): Handeling.DEPLOYMENT_SCHRIJVEN,
    ("POST", "/api/projects/{project_name}/components"): Handeling.COMPONENT_TOEVOEGEN,
    ("PATCH", "/api/projects/{project_name}/components/{component_name}"): Handeling.COMPONENT_WIJZIGEN,
    (
        "POST",
        "/api/projects/{project_name}/deployments/{deployment_name}/:clone-bucket-from-external",
    ): Handeling.BUCKET_KLONEN,
    (
        "POST",
        "/api/projects/{project_name}/deployments/{deployment_name}/:clone-database-from-external",
    ): Handeling.DATABASE_KLONEN,
    ("GET", "/api/projects/{project_name}/deployments/{deployment_name}/:refresh"): Handeling.DEPLOYMENT_VERVERSEN,
    ("POST", "/api/projects/{project_name}/deployments/{deployment_name}/:validate-clone"): Handeling.KLOON_VALIDEREN,
    ("POST", "/api/projects/{project_name}/deployments/{deployment_name}/components"): Handeling.COMPONENT_KOPPELEN,
    ("PUT", "/api/projects/{project_name}/deployments/{deployment_name}/image"): Handeling.DEPLOYMENT_IMAGE_VERVANGEN,
    ("POST", "/api/projects/{project_name}/registries/by-credentials"): Handeling.REGISTRY_TOEVOEGEN,
    ("POST", "/api/projects/{project_name}/registries/by-secret"): Handeling.REGISTRY_TOEVOEGEN,
    ("POST", "/api/projects/{project_name}/services"): Handeling.DIENST_TOEVOEGEN,
    ("DELETE", "/api/projects/{project_name}/{deployment_name}"): Handeling.DEPLOYMENT_VERWIJDEREN,
    ("POST", "/api/resources/{project_name}/sanitize"): Handeling.RESOURCES_OPSCHONEN,
    ("POST", "/api/resources/{project_name}/tune"): Handeling.RESOURCES_BIJSTELLEN,
    ("GET", "/api/subdomains"): Handeling.SUBDOMEIN_LEZEN,
    ("POST", "/api/v1/backup/project/{project_name}/deployment/{deployment_name}"): Handeling.BACKUP_MAKEN,
    ("GET", "/api/v1/backup/runs/{project_name}/{deployment_name}"): Handeling.BACKUP_LEZEN,
    ("DELETE", "/api/v1/backup/snapshot/{project_name}/{deployment_name}/{snapshot_id}"): Handeling.BACKUP_VERWIJDEREN,
    ("POST", "/api/v1/projects/{project_name}/images/push"): Handeling.IMAGE_PUSHEN,
    ("POST", "/api/v1/restore/bucket/{cluster}/{namespace}/{reference_name}"): Handeling.HERSTEL_UITVOEREN,
    ("POST", "/api/v1/restore/database/{cluster}/{namespace}/{reference_name}"): Handeling.HERSTEL_UITVOEREN,
    ("POST", "/api/v1/restore/project/{project_name}"): Handeling.HERSTEL_UITVOEREN,
    ("POST", "/api/v1/restore/project/{project_name}/deployment/{deployment_name}"): Handeling.HERSTEL_UITVOEREN,
    (
        "POST",
        "/api/v1/restore/project/{project_name}/deployment/{deployment_name}/run/{backup_run_id}",
    ): Handeling.HERSTEL_UITVOEREN,
    ("POST", "/api/v1/restore/pvc/{cluster}/{namespace}/{pvc_name}"): Handeling.HERSTEL_UITVOEREN,
    ("GET", "/api/v1/restore/snapshots/{cluster}/{namespace}"): Handeling.HERSTEL_LEZEN,
    ("GET", "/api/v1/restore/snapshots/{cluster}/{namespace}/{pvc_name}"): Handeling.HERSTEL_LEZEN,
    ("GET", "/api/v2/projects/{project_name}"): Handeling.PROJECT_LEZEN,
    ("POST", "/api/v2/projects/{project_name}/:refresh"): Handeling.PROJECT_VERVERSEN,
    ("POST", "/api/v2/projects/{project_name}/:upsert-deployment"): Handeling.DEPLOYMENT_SCHRIJVEN,
    ("GET", "/api/v2/projects/{project_name}/clusters"): Handeling.CLUSTER_LEZEN,
    ("GET", "/api/v2/projects/{project_name}/components"): Handeling.COMPONENT_LEZEN,
    ("POST", "/api/v2/projects/{project_name}/components"): Handeling.COMPONENT_TOEVOEGEN,
    ("DELETE", "/api/v2/projects/{project_name}/components/{component_name}"): Handeling.COMPONENT_VERWIJDEREN,
    ("PATCH", "/api/v2/projects/{project_name}/components/{component_name}"): Handeling.COMPONENT_WIJZIGEN,
    ("GET", "/api/v2/projects/{project_name}/deployments"): Handeling.DEPLOYMENT_LEZEN,
    ("GET", "/api/v2/projects/{project_name}/deployments/{deployment_name}"): Handeling.DEPLOYMENT_LEZEN,
    ("POST", "/api/v2/projects/{project_name}/deployments/{deployment_name}/:clone-bucket"): Handeling.BUCKET_KLONEN,
    (
        "POST",
        "/api/v2/projects/{project_name}/deployments/{deployment_name}/:clone-database",
    ): Handeling.DATABASE_KLONEN,
    ("POST", "/api/v2/projects/{project_name}/deployments/{deployment_name}/:refresh"): Handeling.DEPLOYMENT_VERVERSEN,
    (
        "POST",
        "/api/v2/projects/{project_name}/deployments/{deployment_name}/:rotate-credentials",
    ): Handeling.DIENST_ACTIE,
    ("POST", "/api/v2/projects/{project_name}/deployments/{deployment_name}/components"): Handeling.COMPONENT_KOPPELEN,
    (
        "PUT",
        "/api/v2/projects/{project_name}/deployments/{deployment_name}/image",
    ): Handeling.DEPLOYMENT_IMAGE_VERVANGEN,
    ("GET", "/api/v2/projects/{project_name}/pending-rollout"): Handeling.PROJECT_LEZEN,
    ("GET", "/api/v2/projects/{project_name}/services"): Handeling.DIENST_LEZEN,
    ("POST", "/api/v2/projects/{project_name}/services"): Handeling.DIENST_TOEVOEGEN,
    (
        "DELETE",
        "/api/v2/projects/{project_name}/services/aliases/values/component/{component_name}",
    ): Handeling.DIENST_WAARDEN_SCHRIJVEN,
    (
        "GET",
        "/api/v2/projects/{project_name}/services/aliases/values/component/{component_name}",
    ): Handeling.DIENST_LEZEN,
    (
        "PATCH",
        "/api/v2/projects/{project_name}/services/aliases/values/component/{component_name}",
    ): Handeling.DIENST_WAARDEN_SCHRIJVEN,
    (
        "POST",
        "/api/v2/projects/{project_name}/services/aliases/values/component/{component_name}",
    ): Handeling.DIENST_WAARDEN_SCHRIJVEN,
    (
        "POST",
        "/api/v2/projects/{project_name}/services/aliases/values/component/{component_name}/:delete",
    ): Handeling.DIENST_WAARDEN_SCHRIJVEN,
    (
        "DELETE",
        "/api/v2/projects/{project_name}/services/aliases/values/component/{component_name}/{value_key}",
    ): Handeling.DIENST_WAARDEN_SCHRIJVEN,
    ("POST", "/api/v2/projects/{project_name}/services/attachments/attachment"): Handeling.DIENST_ACTIE,
    (
        "DELETE",
        "/api/v2/projects/{project_name}/services/attachments/attachment/{attachment_id}",
    ): Handeling.DIENST_ACTIE,
    ("PUT", "/api/v2/projects/{project_name}/services/attachments/attachment/{attachment_id}"): Handeling.DIENST_ACTIE,
    (
        "POST",
        "/api/v2/projects/{project_name}/services/attachments/component/{component_name}/attachment",
    ): Handeling.DIENST_ACTIE,
    (
        "PUT",
        "/api/v2/projects/{project_name}/services/attachments/component/{component_name}/attachment/{attachment_id}",
    ): Handeling.DIENST_ACTIE,
    (
        "DELETE",
        "/api/v2/projects/{project_name}/services/attachments/config/component/{component_name}",
    ): Handeling.DIENST_CONFIGUREREN,
    (
        "PATCH",
        "/api/v2/projects/{project_name}/services/attachments/config/component/{component_name}",
    ): Handeling.DIENST_CONFIGUREREN,
    (
        "PUT",
        "/api/v2/projects/{project_name}/services/attachments/config/component/{component_name}",
    ): Handeling.DIENST_CONFIGUREREN,
    (
        "DELETE",
        "/api/v2/projects/{project_name}/services/authorization-wall/config/project",
    ): Handeling.DIENST_CONFIGUREREN,
    (
        "PUT",
        "/api/v2/projects/{project_name}/services/authorization-wall/config/project",
    ): Handeling.DIENST_CONFIGUREREN,
    (
        "DELETE",
        "/api/v2/projects/{project_name}/services/cross-domain-access/config/deployment/{deployment_name}",
    ): Handeling.DIENST_CONFIGUREREN,
    (
        "PUT",
        "/api/v2/projects/{project_name}/services/cross-domain-access/config/deployment/{deployment_name}",
    ): Handeling.DIENST_CONFIGUREREN,
    (
        "PATCH",
        "/api/v2/projects/{project_name}/services/cross-domain-access/config/deployment/{deployment_name}/inbound",
    ): Handeling.DIENST_CONFIGUREREN,
    (
        "PATCH",
        "/api/v2/projects/{project_name}/services/cross-domain-access/config/deployment/{deployment_name}/outbound",
    ): Handeling.DIENST_CONFIGUREREN,
    (
        "DELETE",
        "/api/v2/projects/{project_name}/services/cross-domain-access/config/project",
    ): Handeling.DIENST_CONFIGUREREN,
    (
        "PUT",
        "/api/v2/projects/{project_name}/services/cross-domain-access/config/project",
    ): Handeling.DIENST_CONFIGUREREN,
    (
        "PATCH",
        "/api/v2/projects/{project_name}/services/cross-domain-access/config/project/inbound",
    ): Handeling.DIENST_CONFIGUREREN,
    (
        "PATCH",
        "/api/v2/projects/{project_name}/services/cross-domain-access/config/project/outbound",
    ): Handeling.DIENST_CONFIGUREREN,
    (
        "DELETE",
        "/api/v2/projects/{project_name}/services/health-check/config/component/{component_name}",
    ): Handeling.DIENST_CONFIGUREREN,
    (
        "PUT",
        "/api/v2/projects/{project_name}/services/health-check/config/component/{component_name}",
    ): Handeling.DIENST_CONFIGUREREN,
    (
        "DELETE",
        "/api/v2/projects/{project_name}/services/image-registries/config/component/{component_name}",
    ): Handeling.DIENST_CONFIGUREREN,
    (
        "PUT",
        "/api/v2/projects/{project_name}/services/image-registries/config/component/{component_name}",
    ): Handeling.DIENST_CONFIGUREREN,
    (
        "DELETE",
        "/api/v2/projects/{project_name}/services/image-registries/config/project",
    ): Handeling.DIENST_CONFIGUREREN,
    ("PUT", "/api/v2/projects/{project_name}/services/image-registries/config/project"): Handeling.DIENST_CONFIGUREREN,
    ("DELETE", "/api/v2/projects/{project_name}/services/invite/config/project"): Handeling.DIENST_CONFIGUREREN,
    ("PUT", "/api/v2/projects/{project_name}/services/invite/config/project"): Handeling.DIENST_CONFIGUREREN,
    ("PATCH", "/api/v2/projects/{project_name}/services/invite/config/project/active"): Handeling.DIENST_CONFIGUREREN,
    ("DELETE", "/api/v2/projects/{project_name}/services/keycloak/config/project"): Handeling.DIENST_CONFIGUREREN,
    ("PUT", "/api/v2/projects/{project_name}/services/keycloak/config/project"): Handeling.DIENST_CONFIGUREREN,
    (
        "DELETE",
        "/api/v2/projects/{project_name}/services/metrics-scraper/config/component/{component_name}",
    ): Handeling.DIENST_CONFIGUREREN,
    (
        "PUT",
        "/api/v2/projects/{project_name}/services/metrics-scraper/config/component/{component_name}",
    ): Handeling.DIENST_CONFIGUREREN,
    (
        "DELETE",
        "/api/v2/projects/{project_name}/services/minio-storage/config/deployment/{deployment_name}",
    ): Handeling.DIENST_CONFIGUREREN,
    (
        "PUT",
        "/api/v2/projects/{project_name}/services/minio-storage/config/deployment/{deployment_name}",
    ): Handeling.DIENST_CONFIGUREREN,
    ("DELETE", "/api/v2/projects/{project_name}/services/minio-storage/config/project"): Handeling.DIENST_CONFIGUREREN,
    ("PUT", "/api/v2/projects/{project_name}/services/minio-storage/config/project"): Handeling.DIENST_CONFIGUREREN,
    (
        "DELETE",
        "/api/v2/projects/{project_name}/services/namespace-postgresql-database/config/project",
    ): Handeling.DIENST_CONFIGUREREN,
    (
        "PUT",
        "/api/v2/projects/{project_name}/services/namespace-postgresql-database/config/project",
    ): Handeling.DIENST_CONFIGUREREN,
    (
        "DELETE",
        "/api/v2/projects/{project_name}/services/persistent-storage/config/component/{component_name}",
    ): Handeling.DIENST_CONFIGUREREN,
    (
        "PATCH",
        "/api/v2/projects/{project_name}/services/persistent-storage/config/component/{component_name}",
    ): Handeling.DIENST_CONFIGUREREN,
    (
        "PUT",
        "/api/v2/projects/{project_name}/services/persistent-storage/config/component/{component_name}",
    ): Handeling.DIENST_CONFIGUREREN,
    (
        "DELETE",
        "/api/v2/projects/{project_name}/services/postgresql-database/config/deployment/{deployment_name}",
    ): Handeling.DIENST_CONFIGUREREN,
    (
        "PUT",
        "/api/v2/projects/{project_name}/services/postgresql-database/config/deployment/{deployment_name}",
    ): Handeling.DIENST_CONFIGUREREN,
    (
        "DELETE",
        "/api/v2/projects/{project_name}/services/postgresql-database/config/project",
    ): Handeling.DIENST_CONFIGUREREN,
    (
        "PUT",
        "/api/v2/projects/{project_name}/services/postgresql-database/config/project",
    ): Handeling.DIENST_CONFIGUREREN,
    ("GET", "/api/v2/projects/{project_name}/services/postgresql-database/schemas"): Handeling.DATABASE_SCHEMA_LEZEN,
    ("POST", "/api/v2/projects/{project_name}/services/postgresql-database/schemas"): Handeling.DATABASE_SCHEMA_BEHEREN,
    (
        "DELETE",
        "/api/v2/projects/{project_name}/services/postgresql-database/schemas/{postfix}",
    ): Handeling.DATABASE_SCHEMA_BEHEREN,
    (
        "DELETE",
        "/api/v2/projects/{project_name}/services/publish-on-web/config/component/{component_name}",
    ): Handeling.DIENST_CONFIGUREREN,
    (
        "PUT",
        "/api/v2/projects/{project_name}/services/publish-on-web/config/component/{component_name}",
    ): Handeling.DIENST_CONFIGUREREN,
    (
        "DELETE",
        "/api/v2/projects/{project_name}/services/publish-on-web/config/deployment/{deployment_name}",
    ): Handeling.DIENST_CONFIGUREREN,
    (
        "PUT",
        "/api/v2/projects/{project_name}/services/publish-on-web/config/deployment/{deployment_name}",
    ): Handeling.DIENST_CONFIGUREREN,
    ("DELETE", "/api/v2/projects/{project_name}/services/redis/config/project"): Handeling.DIENST_CONFIGUREREN,
    ("PUT", "/api/v2/projects/{project_name}/services/redis/config/project"): Handeling.DIENST_CONFIGUREREN,
    ("DELETE", "/api/v2/projects/{project_name}/services/send-email/config/project"): Handeling.DIENST_CONFIGUREREN,
    ("PUT", "/api/v2/projects/{project_name}/services/send-email/config/project"): Handeling.DIENST_CONFIGUREREN,
    (
        "DELETE",
        "/api/v2/projects/{project_name}/services/seaweedfs-storage/config/deployment/{deployment_name}",
    ): Handeling.DIENST_CONFIGUREREN,
    (
        "PUT",
        "/api/v2/projects/{project_name}/services/seaweedfs-storage/config/deployment/{deployment_name}",
    ): Handeling.DIENST_CONFIGUREREN,
    (
        "DELETE",
        "/api/v2/projects/{project_name}/services/seaweedfs-storage/config/project",
    ): Handeling.DIENST_CONFIGUREREN,
    (
        "PUT",
        "/api/v2/projects/{project_name}/services/seaweedfs-storage/config/project",
    ): Handeling.DIENST_CONFIGUREREN,
    ("DELETE", "/api/v2/projects/{project_name}/services/sleep-mode/config/project"): Handeling.DIENST_CONFIGUREREN,
    ("PUT", "/api/v2/projects/{project_name}/services/sleep-mode/config/project"): Handeling.DIENST_CONFIGUREREN,
    (
        "PATCH",
        "/api/v2/projects/{project_name}/services/sleep-mode/config/project/match",
    ): Handeling.DIENST_CONFIGUREREN,
    (
        "DELETE",
        "/api/v2/projects/{project_name}/services/temp-storage/config/component/{component_name}",
    ): Handeling.DIENST_CONFIGUREREN,
    (
        "PATCH",
        "/api/v2/projects/{project_name}/services/temp-storage/config/component/{component_name}",
    ): Handeling.DIENST_CONFIGUREREN,
    (
        "PUT",
        "/api/v2/projects/{project_name}/services/temp-storage/config/component/{component_name}",
    ): Handeling.DIENST_CONFIGUREREN,
    (
        "DELETE",
        "/api/v2/projects/{project_name}/services/user-env-vars/values/component/{component_name}",
    ): Handeling.DIENST_WAARDEN_SCHRIJVEN,
    (
        "GET",
        "/api/v2/projects/{project_name}/services/user-env-vars/values/component/{component_name}",
    ): Handeling.DIENST_LEZEN,
    (
        "PATCH",
        "/api/v2/projects/{project_name}/services/user-env-vars/values/component/{component_name}",
    ): Handeling.DIENST_WAARDEN_SCHRIJVEN,
    (
        "POST",
        "/api/v2/projects/{project_name}/services/user-env-vars/values/component/{component_name}",
    ): Handeling.DIENST_WAARDEN_SCHRIJVEN,
    (
        "POST",
        "/api/v2/projects/{project_name}/services/user-env-vars/values/component/{component_name}/:delete",
    ): Handeling.DIENST_WAARDEN_SCHRIJVEN,
    (
        "DELETE",
        "/api/v2/projects/{project_name}/services/user-env-vars/values/component/{component_name}/{value_key}",
    ): Handeling.DIENST_WAARDEN_SCHRIJVEN,
    (
        "DELETE",
        "/api/v2/projects/{project_name}/services/user-env-vars/values/deployment/{deployment_name}/component/{component_name}",
    ): Handeling.DIENST_WAARDEN_SCHRIJVEN,
    (
        "GET",
        "/api/v2/projects/{project_name}/services/user-env-vars/values/deployment/{deployment_name}/component/{component_name}",
    ): Handeling.DIENST_LEZEN,
    (
        "PATCH",
        "/api/v2/projects/{project_name}/services/user-env-vars/values/deployment/{deployment_name}/component/{component_name}",
    ): Handeling.DIENST_WAARDEN_SCHRIJVEN,
    (
        "POST",
        "/api/v2/projects/{project_name}/services/user-env-vars/values/deployment/{deployment_name}/component/{component_name}",
    ): Handeling.DIENST_WAARDEN_SCHRIJVEN,
    (
        "POST",
        "/api/v2/projects/{project_name}/services/user-env-vars/values/deployment/{deployment_name}/component/{component_name}/:delete",
    ): Handeling.DIENST_WAARDEN_SCHRIJVEN,
    (
        "DELETE",
        "/api/v2/projects/{project_name}/services/user-env-vars/values/deployment/{deployment_name}/component/{component_name}/{value_key}",
    ): Handeling.DIENST_WAARDEN_SCHRIJVEN,
    ("GET", "/api/v2/projects/{project_name}/services/{service_name}/config"): Handeling.DIENST_LEZEN,
    ("GET", "/api/v2/projects/{project_name}/subdomains/check/{subdomain}"): Handeling.SUBDOMEIN_LEZEN,
    ("DELETE", "/api/v2/projects/{project_name}/{deployment_name}"): Handeling.DEPLOYMENT_VERWIJDEREN,
}


#: De routers die hun sleutelcontrole met de HAND doen en dus geen markering dragen
#: (inventarisatie paragraaf 5). Ze zijn niet uit de routetabel te vinden, dus ze staan hier.
#: Loopt dit uit de pas, dan is er een handgeschreven controle bij gekomen of verdwenen.
HANDGESCHREVEN = {
    ("GET", "/api/tasks/{task_id}"): Handeling.TAAK_LEZEN,
    ("GET", "/api/tasks"): Handeling.TAAK_LEZEN,
    ("POST", "/api/tasks/{task_id}/:cancel"): Handeling.TAAK_ANNULEREN,
    ("POST", "/api/sleep-mode/{project_name}/{deployment_name}/wake"): Handeling.SLAAPSTAND_WEKKEN,
    ("GET", "/api/sleep-mode/{project_name}/{deployment_name}/status"): Handeling.SLAAPSTAND_LEZEN,
}

#: Wat de bearerroutes vragen. Ze nemen een SSO-bearer en geen projectsleutel: bij het
#: projectoverzicht bestaat het project nog niet, en een token hoort geen token te maken.
BEARERROUTES = {
    ("GET", "/api/v2/projects"): Handeling.PROJECT_LIJSTEN,
    ("POST", "/api/v2/projects"): Handeling.PROJECT_AANMAKEN,
    ("GET", "/api/v2/projects/{project_name}/api-key"): Handeling.PROJECTSLEUTEL_LEZEN,
    ("GET", "/api/v2/projects/{project_name}/leden"): Handeling.TOKEN_LEZEN,
    ("POST", "/api/v2/projects/{project_name}/leden"): Handeling.TOKEN_BEHEREN,
    ("DELETE", "/api/v2/projects/{project_name}/leden/{naam}"): Handeling.TOKEN_BEHEREN,
    ("PUT", "/api/v2/projects/{project_name}/leden/{naam}/rol"): Handeling.TOKEN_BEHEREN,
    ("POST", "/api/v2/projects/{project_name}/leden/{naam}/tokens"): Handeling.TOKEN_BEHEREN,
    ("POST", "/api/v2/projects/{project_name}/leden/{naam}/tokens/{tokennaam}/:roteer"): Handeling.TOKEN_BEHEREN,
    ("DELETE", "/api/v2/projects/{project_name}/leden/{naam}/tokens/{tokennaam}"): Handeling.TOKEN_BEHEREN,
}


def _gemarkeerde_routes(markering: str) -> dict[tuple[str, str], Handeling | None]:
    gevonden: dict[tuple[str, str], Handeling | None] = {}
    for route in app.routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None or not getattr(endpoint, markering, False):
            continue
        for methode in getattr(route, "methods", None) or []:
            gevonden[(methode, route.path)] = handeling_van(endpoint)
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
    paden: dict[tuple[str, str], Handeling | None] = {}
    for route in app.routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None:
            continue
        for methode in getattr(route, "methods", None) or []:
            paden[(methode, route.path)] = handeling_van(endpoint)

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

    with pytest.raises(RuntimeError, match="noemen geen handeling"):
        controleer_handelingen([_Route()])


def test_elke_route_vraagt_een_handeling_die_een_rol_kan_openen() -> None:
    """Geen route vraagt iets dat geen enkele rol opent; dat zou een dode route zijn."""
    gevraagd = {h for h in _gemarkeerde_routes(REQUIRES_PROJECT_NAME).values() if h is not None}
    gevraagd |= set(HANDGESCHREVEN.values())
    gevraagd |= set(BEARERROUTES.values())

    nooit_te_openen = sorted(h.value for h in gevraagd if h not in ALLE_HANDELINGEN)
    assert not nooit_te_openen, f"deze handelingen opent geen enkele rol: {nooit_te_openen}"


def test_een_leesrol_komt_niet_verder_dan_lezen() -> None:
    """De rol van het lid bepaalt wat zijn token mag, en een developer wijzigt niets.

    Dezelfde grens die ``require_project_edit_access`` in de interface trekt. Loopt deze
    test rood, dan opent een token meer dan zijn eigenaar op het scherm kan.
    """
    for rol in ("member", "developer"):
        assert ROL_HANDELINGEN[rol] == LEESHANDELINGEN

    for rol in ("admin", "owner"):
        assert ROL_HANDELINGEN[rol] == ALLE_HANDELINGEN

    schrijfhandelingen = ALLE_HANDELINGEN - LEESHANDELINGEN
    assert Handeling.PROJECT_VERWIJDEREN in schrijfhandelingen
    assert Handeling.HERSTEL_UITVOEREN in schrijfhandelingen


def test_een_onbekende_rol_opent_niets() -> None:
    """Een rol die iemand in het bestand verzint is geen reden om iets toe te laten."""
    assert ROL_HANDELINGEN.get("beheerder") is None


def test_tokens_beheren_vraagt_een_mens() -> None:
    """Een token aanmaken vraagt een geverifieerde persoon, en een pijplijn is er geen."""
    assert Handeling.TOKEN_BEHEREN in MENS_VERPLICHT
    assert Handeling.TOKEN_LEZEN not in MENS_VERPLICHT


#: Welke step-upactie van RC-332 op welke handeling van deze catalogus zit, voor de routes
#: die met een token bereikbaar zijn. De beheerroutes van RC-377 staan er niet bij: die
#: nemen ``validate_platform_admin_token`` en zijn met geen ``zad_``-token te bereiken.
#:
#: Deze tabel bestaat omdat het twee catalogi zijn die hetzelfde LIJKEN. Ze beslissen een
#: andere vraag, en de noot bij ``MENS_VERPLICHT`` zegt waarom ze niet gelijkgetrokken zijn.
#: Wat wel vast moet staan is de koppeling: komt er een step-up-route bij of verschuift er
#: een handeling, dan valt dat hier op in plaats van bij een aanroeper die hem kwijt is.
STAP_OP_PER_HANDELING = {
    StepUpActie.PROJECT_VERWIJDEREN: Handeling.PROJECT_VERWIJDEREN,
    StepUpActie.DEPLOYMENT_VERWIJDEREN: Handeling.DEPLOYMENT_VERWIJDEREN,
    StepUpActie.COMPONENT_VERWIJDEREN: Handeling.COMPONENT_VERWIJDEREN,
    StepUpActie.MARKERING_VERLAGEN: Handeling.DEPLOYMENT_SCHRIJVEN,
}


def test_elke_step_up_route_met_een_token_hoort_bij_zijn_handeling() -> None:
    """De koppeling tussen de twee catalogi, per route."""
    gevonden = {
        getattr(route.endpoint, REQUIRES_STEP_UP): handeling_van(route.endpoint)
        for route in app.routes
        if getattr(route, "endpoint", None) is not None
        and getattr(route.endpoint, REQUIRES_PROJECT_NAME, False)
        and getattr(route.endpoint, REQUIRES_STEP_UP, None) is not None
    }

    assert gevonden == STAP_OP_PER_HANDELING


def test_een_step_up_handeling_staat_niet_onvoorwaardelijk_in_mens_verplicht() -> None:
    """De twee catalogi overlappen met OPZET niet.

    Een step-up weegt het DOELWIT: een deployment op productie vraagt een verse factor,
    dezelfde op acceptatie niet. Zou dezelfde handeling in ``MENS_VERPLICHT`` staan, dan
    was een pijplijn ook uitgesloten van het niet-productiegeval, en dat is een verruiming
    van de weigering die niemand gevraagd heeft.
    """
    assert not (set(STAP_OP_PER_HANDELING.values()) & MENS_VERPLICHT)


def test_de_markering_wikkelt_de_functie_niet() -> None:
    """``handeling`` zet een attribuut en geeft dezelfde functie terug.

    Zou hij wikkelen, dan verliest FastAPI de handtekening waar hij zijn parameters uit leest.
    """

    async def route() -> None: ...

    gemarkeerd = handeling(Handeling.PROJECT_LEZEN)(route)
    assert gemarkeerd is route
    assert getattr(route, HANDELING_ATTR) is Handeling.PROJECT_LEZEN
