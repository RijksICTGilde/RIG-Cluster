"""Elke taak van ZAD van begin tot eind, tegen alle echte afhankelijkheden tegelijk.

Fase 1 en 2 meten de connectoren en de managers los, fase 3 het opstarten. Een taak is
meer dan de som daarvan: hij leest het projectbestand uit git, laat een manager zijn werk
doen in PostgreSQL, MinIO, Keycloak en Kubernetes, schrijft de manifesten terug naar git en
wacht tot ArgoCD ze heeft uitgerold. Dat is wat hier gemeten wordt, taak voor taak, met de
echte ``TaskWorker`` ertussen: een scenario maakt de taak aan zoals het portaal of de
zad-cli dat doet en wacht op de UITKOMST van de taak, niet op de klok.

De scenario's vormen een keten op EEN project, in de orde waarin een project leeft:
aanmaken, een component, twee deployments, de diensten, de configuratie, de bijlagen, de
klonen, slapen en wekken, een backup en een restore, de rotaties, en alles weer weg. Daarom
``serial``: elk scenario heeft de toestand van het vorige nodig, en de laatste invariant is
dat er na het verwijderen niets meer van het project in een van de afhankelijkheden staat.

Draai hem met ``task test-taken``; zonder de omgeving slaat de groep zichzelf over.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import pytest_asyncio
from opi.core.async_task_service import TaskType
from opi.core.config import settings
from opi.core.database_pools import close_database_pools
from opi.services.catalog.base import ConfigLayer, config_endpoint_path
from opi.services.project_store import reset_project_store
from tests.e2e.helpers import sandbox_api
from tests.integration import forgejo_live, kubernetes_live, minio_live, postgres_live, startup_live, tasks_live
from tests.integration.tasks_live import (
    ATTACHMENT_ID,
    ATTACHMENT_PATH,
    COMPONENT,
    DEPLOYMENT,
    PREFIX,
    PROJECT,
    SECOND_COMPONENT,
    SECOND_DEPLOYMENT,
    SERVICES,
    UPDATED_IMAGE,
    WORKLOAD_IMAGE,
    WORKLOAD_PORT,
    AppUnderTest,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

# Een loop voor de module: de server en zijn pools leven in de loop van de toetsen, en een
# loop per toets zou betekenen dat de volgende toets tegen een gesloten pool praat.
pytestmark = [
    pytest.mark.requires_infra,
    pytest.mark.tasks_live,
    pytest.mark.serial,
    pytest.mark.asyncio(loop_scope="module"),
]

#: Hoelang een taak mag doen. Een vangnet, geen wachtmechanisme: de helper wacht op de
#: uitkomst van de taak. Royaal, want een uitrol wacht op een echte ArgoCD-sync.
TASK_TIMEOUT = 900.0

#: Hoelang een toestand in het cluster mag doen nadat de taak klaar is.
CLUSTER_TIMEOUT = 300.0

#: Het wachtwoord van de wegwerprollen van de opstelling. Plaatshoudervorm, en het bereikt
#: alleen de containers van deze groep.
OPSTELLING_PASSWORD = "example-Wachtwoord-1234"

#: De bron van de kloonscenario's: een database en een bucket binnen de opstelling. Een
#: externe bron zou over een chisel-tunnel lopen, en die staat met zijn reden op de kaart.
# Een eigen voorvoegsel, NIET dat van het project: de eindinvariant eist dat er van het
# project niets overblijft, en een bron met het voorvoegsel van het project zou daar als
# restant lezen.
CLONE_SOURCE_DATABASE = "zadbron_kloon"
CLONE_SOURCE_SCHEMA = "zadbron_kloon"
CLONE_SOURCE_BUCKET = "zadbron-kloon"
CLONE_SOURCE_OBJECT = "regel.txt"
CLONE_SOURCE_CONTENT = "een regel uit de bron\n"

#: Het extra schema dat de schemataak aanlegt en weer opzegt.
SCHEMA_POSTFIX = "rapportage"

#: De env-var waarmee de values-taak gemeten wordt.
ENV_NAME = "METING_SLEUTEL"
ENV_VALUE = "metingwaarde"

#: De inhoud van de bijlage. Een constante, want het bijlage-scenario zet hem neer en het
#: opzeg-scenario eist dat precies deze tekst weg is.
ATTACHMENT_CONTENT = b"inhoud van de meting\n"


@pytest.fixture(autouse=True)
def no_api_step_up() -> None:
    """Overrides the root conftest, which mocks ``is_fresh`` to False for every test.

    This group measures the real step-up: the code goes in through ``POST /api/v2/step-up``
    and the table it lands in is what the next call reads.
    """


@pytest.fixture(scope="module")
def forgejo() -> Iterator[forgejo_live.ForgejoApi]:
    tasks_live.assert_local()
    api = forgejo_live.ForgejoApi()
    yield api
    api.drop_token()


@pytest.fixture(scope="module")
def kubectl() -> kubernetes_live.Kubectl:
    return kubernetes_live.Kubectl()


class Wereld:
    """Wat de scenario's aan elkaar doorgeven: de server en wat het project legitimeert.

    Een object en geen fixture per waarde, want de sleutel van het project bestaat pas na
    het eerste scenario: hij komt EEN keer uit het antwoord van de aanmaak en is daarna
    niet meer op te halen.
    """

    def __init__(self, app: AppUnderTest) -> None:
        self.app = app
        self.api_key = ""
        self.backup_run_id = ""

    @property
    def base_url(self) -> str:
        return self.app.base_url


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def wereld(
    forgejo: forgejo_live.ForgejoApi,
    kubectl: kubernetes_live.Kubectl,
    tmp_path_factory: pytest.TempPathFactory,
) -> AsyncIterator[Wereld]:
    """De wereld zoals OPI hem aantreft, en de opruiming erna.

    De drie platformrepositories leeg, de platformdatabases van de gedeelde server, en de
    bron van de kloonscenario's. Verder niets: alles wat dit project heeft, hoort door een
    taak neergezet te worden, en dat is wat de scenario's meten.
    """
    private_key, public_key = tasks_live.platform_keys(kubectl)
    with pytest.MonkeyPatch.context() as patch:
        tasks_live.patch_settings(patch, forgejo, platform_private_key=private_key, platform_public_key=public_key)
        tasks_live.enable_background_tasks(patch)
        tasks_live.resolve_platform_names(patch)
        # Een lege schijf hoort bij een leeg cluster: een warme kopie van een eerdere run
        # draagt de historie van een repository die deze fixture net opnieuw heeft gemaakt.
        patch.setattr("opi.connectors.git_repo.GIT_REPOS_DIR", str(tmp_path_factory.mktemp("git-repos")))
        tasks_live.route_deployments_repo_through_host(patch, forgejo, tmp_path_factory.mktemp("git-config"))

        await tasks_live.drop_everything(kubectl, forgejo)
        tasks_live.fresh_platform_repos(forgejo)
        await tasks_live.wait_for_mounted_platform_key(kubectl, private_key)
        tasks_live.install_app_of_apps(kubectl)
        await tasks_live.prepare_platform_databases(OPSTELLING_PASSWORD)
        await _prepare_clone_sources()

        app = AppUnderTest()
        await app.start()
        try:
            yield Wereld(app)
        finally:
            await app.stop()
            await close_database_pools()
            reset_project_store()
            startup_live.forget_collectors()
            await tasks_live.drop_everything(kubectl, forgejo)
            await _drop_clone_sources()
            await tasks_live.drop_platform_databases()


async def _prepare_clone_sources() -> None:
    """De bron van de twee kloonscenario's: een schema met een rij, en een bucket met een object."""
    conn = await postgres_live.admin_connection()
    try:
        if not await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", CLONE_SOURCE_DATABASE):
            await conn.execute(f'CREATE DATABASE "{CLONE_SOURCE_DATABASE}"')
    finally:
        await conn.close()
    conn = await postgres_live.admin_connection(CLONE_SOURCE_DATABASE)
    try:
        await conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{CLONE_SOURCE_SCHEMA}"')
        await conn.execute(f'CREATE TABLE IF NOT EXISTS "{CLONE_SOURCE_SCHEMA}".regels (tekst text)')
        await conn.execute(f'TRUNCATE "{CLONE_SOURCE_SCHEMA}".regels')
        await conn.execute(f'INSERT INTO "{CLONE_SOURCE_SCHEMA}".regels (tekst) VALUES ($1)', CLONE_SOURCE_CONTENT)
    finally:
        await conn.close()

    mc = minio_live.admin_mc()
    mc.run("mb", "--ignore-existing", f"{mc.alias}/{CLONE_SOURCE_BUCKET}")
    mc.put(CLONE_SOURCE_BUCKET, CLONE_SOURCE_OBJECT, CLONE_SOURCE_CONTENT)


async def _drop_clone_sources() -> None:
    conn = await postgres_live.admin_connection()
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{CLONE_SOURCE_DATABASE}" WITH (FORCE)')
    finally:
        await conn.close()
    mc = minio_live.admin_mc()
    mc.run("rb", "--force", f"{mc.alias}/{CLONE_SOURCE_BUCKET}")


# ==================== De weg waarlangs een taak begint ====================


async def _start(wereld: Wereld, method: str, path: str, body: dict[str, Any]) -> str:
    """Start een taak over de API en geef zijn id terug, met de helper van de sandboxsuite.

    In een thread, want de server draait in DEZELFDE event loop als de toets: een
    blokkerende aanroep uit de toets zou de loop bezet houden waarin de server moet
    antwoorden, en dan wacht de toets op zichzelf.
    """
    return await asyncio.to_thread(
        sandbox_api.start_task, wereld.base_url, method, path, wereld.api_key, body, verify_ssl=False
    )


async def _await_task(wereld: Wereld, task_id: str, *, timeout: float = TASK_TIMEOUT) -> dict[str, Any]:
    """Wacht op de uitkomst van de taak. Een gefaalde taak is een gefaalde toets."""
    return await asyncio.to_thread(
        sandbox_api.wait_for_task, wereld.base_url, task_id, wereld.api_key, verify_ssl=False, timeout=timeout
    )


async def _run(wereld: Wereld, method: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
    return await _await_task(wereld, await _start(wereld, method, path, body))


async def _web(wereld: Wereld, path: str, data: dict[str, Any] | None = None) -> httpx.Response:
    """Start een taak over een webroute van het portaal, met sessie en CSRF-token."""
    routes = tasks_live.WebRoutes(wereld.base_url, await tasks_live.session_cookie())
    return await asyncio.to_thread(routes.post, path, data)


def _task_id_from_fragment(answer: httpx.Response) -> str:
    """Het taak-id uit het voortgangsfragment waarmee een webroute antwoordt.

    Het fragment ververst zichzelf op ``/projects/<naam>/task-progress/<id>``
    (``opi/web/task_progress.py``); dat id is het taak-id.
    """
    assert answer.status_code == 200, f"de webroute nam het verzoek niet aan: {answer.status_code} {answer.text[:500]}"
    assert "data-step-up" not in answer.text, "het portaal vroeg een step-up in plaats van de taak te starten"
    found = re.search(r"/task-progress/([0-9a-fA-F-]{36})", answer.text)
    assert found is not None, f"geen taak-id in het voortgangsfragment: {answer.text[:800]}"
    return found.group(1)


# ==================== De scenario's, in de orde waarin een project leeft ====================


async def test_create_project_writes_the_project_file(wereld: Wereld, forgejo: forgejo_live.ForgejoApi) -> None:
    """CREATE_PROJECT: het project bestaat in git, en zijn sleutel opent de API.

    Over de enige route die een SSO-token vraagt in plaats van een projectsleutel: voor
    een project dat nog niet bestaat kan er geen projectsleutel zijn.
    """
    token = await asyncio.to_thread(tasks_live.bearer_token)
    async with httpx.AsyncClient(base_url=wereld.base_url, timeout=120.0) as client:
        answer = await client.post(
            "/api/v2/projects",
            json={"display_name": tasks_live.DISPLAY_NAME, "description": "Project van de groep tasks_live"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert answer.status_code == 202, f"aanmaken gaf {answer.status_code}: {answer.text[:800]}"
    accepted = answer.json()
    assert accepted["project_name"] == PROJECT, (
        f"de afgeleide naam is '{accepted['project_name']}' en niet '{PROJECT}'; staat FIXED_PROJECT_POSTFIX goed?"
    )
    wereld.api_key = accepted["api_key"]
    await tasks_live.await_task_in_queue(accepted["task_id"], timeout=TASK_TIMEOUT)

    data = tasks_live.project_file(forgejo)
    assert data is not None, "het projectbestand staat niet in de zad-projects-repo"
    assert data["name"] == PROJECT
    assert tasks_live.CLUSTER in (data.get("clusters") or [])
    assert any(user.get("email") == tasks_live.ADMIN_EMAIL for user in data.get("users") or [])
    assert (data.get("config") or {}).get("age-public-key"), "het project kreeg geen eigen AGE-sleutelpaar"

    # De sleutel uit het antwoord is de legitimatie van elke volgende taak; werkt hij niet,
    # dan meet geen enkel scenario hierna iets.
    async with httpx.AsyncClient(base_url=wereld.base_url, timeout=60.0) as client:
        listing = await client.get(f"/api/v2/projects/{PROJECT}/deployments", headers={"X-API-Key": wereld.api_key})
    assert listing.status_code == 200, (
        f"de projectsleutel opent de API niet: {listing.status_code} {listing.text[:300]}"
    )


async def test_add_component_declares_it_without_rolling_out(
    wereld: Wereld, forgejo: forgejo_live.ForgejoApi, kubectl: kubernetes_live.Kubectl
) -> None:
    """ADD_COMPONENT: het component staat in git en het cluster heeft nog niets.

    ``rollout=false``, want een component zonder deployment heeft geen bestemming; dat de
    uitrol dan ook werkelijk uitblijft, is de helft die alleen tegen een echt cluster te
    meten is.
    """
    await _run(
        wereld,
        "POST",
        f"/api/v2/projects/{PROJECT}/components?rollout=false",
        {"name": COMPONENT, "image": WORKLOAD_IMAGE, "port": WORKLOAD_PORT, "deployment_names": []},
    )

    data = tasks_live.project_file(forgejo) or {}
    names = {component.get("name") for component in data.get("components") or []}
    assert COMPONENT in names, f"component '{COMPONENT}' staat niet in het projectbestand: {names}"
    assert not tasks_live.project_namespaces(kubectl), "er staat een namespace terwijl er niets uitgerold is"


async def test_upsert_deployment_rolls_the_first_one_out(
    wereld: Wereld, forgejo: forgejo_live.ForgejoApi, kubectl: kubernetes_live.Kubectl
) -> None:
    """UPSERT_DEPLOYMENT: de eerste deployment, met zijn namespace en zijn Application.

    Nog zonder diensten: wat een deployment aan een dienst-rijk project toevoegt, meet het
    scenario na ADD_SERVICE. Dit is de helft die er moet staan voordat een dienst een
    bestemming heeft.
    """
    await _run(
        wereld,
        "POST",
        f"/api/v2/projects/{PROJECT}/:upsert-deployment",
        {"deploymentName": DEPLOYMENT, "components": [{"reference": COMPONENT, "image": WORKLOAD_IMAGE}]},
    )

    data = tasks_live.project_file(forgejo) or {}
    entry = tasks_live.deployment_entry(data, DEPLOYMENT)
    assert entry is not None, f"deployment '{DEPLOYMENT}' staat niet in het projectbestand"
    assert entry.get("cluster") == tasks_live.CLUSTER

    namespaces = tasks_live.project_namespaces(kubectl)
    assert namespaces, "OPI maakte geen namespace voor dit project"
    _assert_everything_synced(kubectl)

    namespace = next(iter(namespaces))
    assert await tasks_live.wait_until(lambda: bool(kubectl.names("deployment", namespace)), timeout=CLUSTER_TIMEOUT), (
        f"namespace {namespace} heeft geen enkele deployment"
    )


async def test_add_service_provisions_every_dependency(
    wereld: Wereld, forgejo: forgejo_live.ForgejoApi, kubectl: kubernetes_live.Kubectl
) -> None:
    """ADD_SERVICE: elke dienst van de catalogus, en wat hij in zijn afhankelijkheid neerzet.

    De diensten waarvoor deze opstelling een echte afhankelijkheid heeft, in hun gepinde
    versie. De invarianten zijn die van fase 1 en 2: een database met een eigen rol, een
    bucket met een eigen gebruiker, een realm met een client, en een ingress in de namespace.
    """
    for service in SERVICES:
        await _run(
            wereld,
            "POST",
            f"/api/v2/projects/{PROJECT}/services",
            {"service": service, "components": [COMPONENT]},
        )

    data = tasks_live.project_file(forgejo) or {}
    missing = sorted(set(SERVICES) - tasks_live.project_services(data))
    assert not missing, f"diensten die niet in het projectbestand landden: {missing}"

    databases = await tasks_live.databases_with_prefix()
    assert databases, "de PostgreSQL-dienst maakte geen database aan"
    roles = await tasks_live.roles_with_prefix()
    assert roles, "de PostgreSQL-dienst maakte geen rol aan"

    buckets = tasks_live.buckets_with_prefix()
    assert buckets, "de MinIO-dienst maakte geen bucket aan"
    assert tasks_live.minio_users_with_prefix(), "de MinIO-dienst maakte geen gebruiker aan"

    assert tasks_live.realm_exists(), f"de Keycloak-dienst maakte geen realm {tasks_live.realm_name()}"

    namespace = next(iter(tasks_live.project_namespaces(kubectl)))
    assert await tasks_live.wait_until(lambda: bool(kubectl.names("ingress", namespace)), timeout=CLUSTER_TIMEOUT), (
        f"publish-on-web leverde geen ingress in {namespace}"
    )

    # Per dienst zijn eigen uitkomst in de namespace, op de namen die de sandboxsuite ook
    # naleest (test_all_services_provisioned_in_cluster): een dienst die alleen in het
    # projectbestand staat, is niet uitgerold.
    secrets = kubectl.names("secret", namespace)
    verwacht = {
        "postgresql-database": f"{DEPLOYMENT}-database",
        "keycloak": f"{DEPLOYMENT}-keycloak",
        "minio-storage": f"{DEPLOYMENT}-minio",
        "metrics-scraper": f"{DEPLOYMENT}-metrics-auth",
    }
    ontbreekt = {dienst: naam for dienst, naam in verwacht.items() if naam not in secrets}
    assert not ontbreekt, f"diensten zonder hun Secret in {namespace}: {ontbreekt}. Aanwezig: {sorted(secrets)}"
    assert any("data" in naam for naam in kubectl.names("persistentvolumeclaim", namespace)), (
        "persistent-storage leverde geen PVC"
    )


async def test_upsert_deployment_adds_a_second_one_with_every_service(
    wereld: Wereld, forgejo: forgejo_live.ForgejoApi, kubectl: kubernetes_live.Kubectl
) -> None:
    """UPSERT_DEPLOYMENT op een project dat elke dienst al heeft: eigen database en bucket.

    De andere helft van deze taak, en de zwaarste: een deployment erbij moet zijn EIGEN
    database, rol en bucket krijgen, en niet die van de buur. Dezelfde invariant als fase 1
    (test_two_deployments_each_reach_only_their_own_database), nu na de echte taak.

    Deze deployment is ook wat het verwijder-scenario straks weghaalt, met de eis dat de
    eerste blijft staan.
    """
    await _run(
        wereld,
        "POST",
        f"/api/v2/projects/{PROJECT}/:upsert-deployment",
        {"deploymentName": SECOND_DEPLOYMENT, "components": [{"reference": COMPONENT, "image": WORKLOAD_IMAGE}]},
    )

    data = tasks_live.project_file(forgejo) or {}
    assert tasks_live.deployment_entry(data, SECOND_DEPLOYMENT) is not None

    eigen = await tasks_live.databases_of(SECOND_DEPLOYMENT)
    buur = await tasks_live.databases_of(DEPLOYMENT)
    assert eigen, "de tweede deployment kreeg geen eigen database"
    assert not (eigen & buur), f"de twee deployments delen een database: {eigen & buur}"
    assert tasks_live.buckets_of(SECOND_DEPLOYMENT), "de tweede deployment kreeg geen eigen bucket"
    assert tasks_live.realm_exists(), "het realm van het project is verdwenen"

    assert tasks_live.argo_names_for(kubectl, "application", SECOND_DEPLOYMENT), (
        "de tweede deployment kreeg geen eigen ArgoCD-Application"
    )
    _assert_everything_synced(kubectl)


async def test_configure_service_changes_the_realm(wereld: Wereld, forgejo: forgejo_live.ForgejoApi) -> None:
    """CONFIGURE_SERVICE: een configuratie van een dienst, en wat de dienst ermee doet.

    Op de keycloak-dienst, want die schrijft zijn configuratie werkelijk naar een
    afhankelijkheid: een extra redirect-URI hoort in de client van het realm te staan.
    """
    extra = "http://localhost:8099/*"
    await _run(
        wereld,
        "PUT",
        config_endpoint_path(ConfigLayer.PROJECT, "keycloak", PROJECT),
        {"additional_redirect_uris": [extra]},
    )

    data = tasks_live.project_file(forgejo) or {}
    assert "keycloak" in tasks_live.project_services(data)

    api = tasks_live.keycloak_api()
    clients = api.clients(tasks_live.realm_name())
    assert any(extra in (client.get("redirectUris") or []) for client in clients), (
        f"geen client in {tasks_live.realm_name()} draagt de extra redirect-URI {extra}"
    )


async def test_configure_service_values_reaches_the_namespace(wereld: Wereld, kubectl: kubernetes_live.Kubectl) -> None:
    """CONFIGURE_SERVICE_VALUES: een env-var van de gebruiker, tot in het Secret.

    De waarde staat versleuteld in het projectbestand; wat de toets naleest is wat er in
    het cluster komt, want dat is wat de applicatie ziet.
    """
    await _run(
        wereld,
        "POST",
        f"/api/v2/projects/{PROJECT}/services/user-env-vars/values/component/{COMPONENT}",
        {"values": {ENV_NAME: ENV_VALUE}},
    )

    namespace = next(iter(tasks_live.project_namespaces(kubectl)))
    assert await tasks_live.wait_until(
        lambda: tasks_live.secret_holds(kubectl, namespace, ENV_NAME, ENV_VALUE), timeout=CLUSTER_TIMEOUT
    ), f"geen Secret in {namespace} draagt {ENV_NAME}"


async def test_manage_database_schemas_adds_and_removes_one(wereld: Wereld, forgejo: forgejo_live.ForgejoApi) -> None:
    """MANAGE_DATABASE_SCHEMAS: an extra schema comes, and its removal only marks it.

    Removing a schema marks it instead of dropping it (RC-17): the schema and its data stay,
    so a routine save never discards data.
    """
    databases = sorted(await tasks_live.databases_with_prefix())
    assert databases, "geen database om een schema in te maken"
    database = databases[0]
    before = await tasks_live.schemas_in(database)

    path = f"/api/v2/projects/{PROJECT}/services/postgresql-database/schemas"
    await _run(wereld, "POST", path, {"postfix": SCHEMA_POSTFIX, "description": "Waar de meting bij mag"})
    after = await tasks_live.schemas_in(database)
    added = after - before
    assert added, f"er kwam geen schema bij in {database}"

    await _run(wereld, "DELETE", f"{path}/{SCHEMA_POSTFIX}", {})
    assert await tasks_live.schemas_in(database) == after, "removing the schema dropped it, data and all"
    entry = tasks_live.schema_entry(tasks_live.project_file(forgejo) or {}, SCHEMA_POSTFIX)
    assert entry is not None, f"schema '{SCHEMA_POSTFIX}' is gone from the project file instead of marked"
    assert entry.get("marked-for-deletion") is True, f"schema '{SCHEMA_POSTFIX}' is not marked: {entry}"


async def test_add_component_to_deployment_hangs_it_on_one(wereld: Wereld, forgejo: forgejo_live.ForgejoApi) -> None:
    """ADD_COMPONENT_TO_DEPLOYMENT: een tweede component, aan EEN van de twee deployments."""
    await _run(
        wereld,
        "POST",
        f"/api/v2/projects/{PROJECT}/components?rollout=false",
        {"name": SECOND_COMPONENT, "image": WORKLOAD_IMAGE, "deployment_names": []},
    )
    await _run(
        wereld,
        "POST",
        f"/api/v2/projects/{PROJECT}/deployments/{DEPLOYMENT}/components",
        {"component_name": SECOND_COMPONENT, "image": WORKLOAD_IMAGE},
    )

    data = tasks_live.project_file(forgejo) or {}
    assert SECOND_COMPONENT in tasks_live.component_images(data, DEPLOYMENT)
    assert SECOND_COMPONENT not in tasks_live.component_images(data, SECOND_DEPLOYMENT), (
        "het component landde ook in de deployment waar het niet aan gehangen is"
    )


async def test_update_component_changes_its_resources(wereld: Wereld, forgejo: forgejo_live.ForgejoApi) -> None:
    """UPDATE_COMPONENT: een gewijzigde grens, tot in het gecommitte projectbestand."""
    await _run(
        wereld,
        "PATCH",
        f"/api/v2/projects/{PROJECT}/components/{SECOND_COMPONENT}",
        {"memory_limit": "192Mi"},
    )

    data = tasks_live.project_file(forgejo) or {}
    component = next((entry for entry in data.get("components") or [] if entry.get("name") == SECOND_COMPONENT), None)
    assert component is not None
    limits = ((component.get("resources") or {}).get("limits")) or {}
    assert limits.get("memory") == "192Mi", f"de grens werd niet vastgelegd: {limits}"


async def test_update_image_reaches_the_cluster(
    wereld: Wereld, forgejo: forgejo_live.ForgejoApi, kubectl: kubernetes_live.Kubectl
) -> None:
    """UPDATE_IMAGE: een andere tag, in het projectbestand EN in de draaiende pod-spec."""
    await _run(
        wereld,
        "PUT",
        f"/api/v2/projects/{PROJECT}/deployments/{DEPLOYMENT}/image",
        {"componentName": COMPONENT, "newImageUrl": UPDATED_IMAGE},
    )

    data = tasks_live.project_file(forgejo) or {}
    assert tasks_live.component_images(data, DEPLOYMENT).get(COMPONENT) == UPDATED_IMAGE
    assert tasks_live.component_images(data, SECOND_DEPLOYMENT).get(COMPONENT) == WORKLOAD_IMAGE, (
        "de andere deployment kreeg de nieuwe image ook, en die is er niet om gevraagd"
    )

    namespace = next(iter(tasks_live.project_namespaces(kubectl)))
    assert await tasks_live.wait_until(
        lambda: tasks_live.image_in_use(kubectl, namespace, UPDATED_IMAGE), timeout=CLUSTER_TIMEOUT
    ), f"geen pod-spec in {namespace} draagt {UPDATED_IMAGE}"


async def test_refresh_deployment_syncs_one(wereld: Wereld, kubectl: kubernetes_live.Kubectl) -> None:
    """REFRESH_DEPLOYMENT: de manifesten opnieuw, en ArgoCD weer Synced."""
    await _run(wereld, "POST", f"/api/v2/projects/{PROJECT}/deployments/{DEPLOYMENT}/:refresh", {})
    _assert_everything_synced(kubectl)


async def test_refresh_project_syncs_everything(wereld: Wereld, kubectl: kubernetes_live.Kubectl) -> None:
    """REFRESH_PROJECT: het hele project opnieuw, met elke Application Synced."""
    answer = await _web(wereld, f"/projects/{PROJECT}/refresh")
    await _await_task(wereld, _task_id_from_fragment(answer))
    _assert_everything_synced(kubectl)


def _assert_everything_synced(kubectl: kubernetes_live.Kubectl) -> None:
    applications = tasks_live.argo_names(kubectl, "application")
    assert applications, "er staat geen ArgoCD-Application voor dit project"
    for application in applications:
        status = (kubectl.get("application", application, "rig-system") or {}).get("status") or {}
        assert (status.get("sync") or {}).get("status") == "Synced", f"{application} is niet Synced: {status}"


async def test_configure_attachment_reaches_the_namespace(wereld: Wereld, kubectl: kubernetes_live.Kubectl) -> None:
    """CONFIGURE_ATTACHMENT: een bijlage, gekoppeld aan een component, tot in het Secret.

    Het Secret in de namespace en niet het bestand in de pod: dat laatste meet de
    sandboxsuite (test_een_vervangen_bijlage_bereikt_de_draaiende_pod), met een probe in de
    pod die deze opstelling niet heeft.
    """
    async with httpx.AsyncClient(base_url=wereld.base_url, timeout=120.0) as client:
        answer = await client.post(
            f"/api/v2/projects/{PROJECT}/services/attachments/component/{COMPONENT}/attachment",
            headers={"X-API-Key": wereld.api_key},
            data={"attachment_id": ATTACHMENT_ID, "provide-as": "file", "path": ATTACHMENT_PATH},
            files={"file": ("meting.txt", ATTACHMENT_CONTENT, "text/plain")},
        )
    assert answer.status_code == 202, f"de bijlage werd niet aangenomen: {answer.status_code} {answer.text[:500]}"
    task_id = answer.headers.get("Location", "").rsplit("/", 1)[-1] or answer.json().get("task_id")
    await _await_task(wereld, task_id)

    namespace = next(iter(tasks_live.project_namespaces(kubectl)))
    assert await tasks_live.wait_until(
        lambda: tasks_live.secret_holds(kubectl, namespace, None, ATTACHMENT_CONTENT.decode()),
        timeout=CLUSTER_TIMEOUT,
    ), f"geen Secret in {namespace} draagt de inhoud van de bijlage"


async def test_delete_attachment_removes_it_everywhere(
    wereld: Wereld, forgejo: forgejo_live.ForgejoApi, kubectl: kubernetes_live.Kubectl
) -> None:
    """DELETE_ATTACHMENT: de bijlage is weg uit het projectbestand en uit het cluster.

    The previous scenario coupled it to a component, so it is in use: the delete is refused
    with 409 unless the caller states with ``confirm_in_use`` that it has seen that.
    """
    async with httpx.AsyncClient(base_url=wereld.base_url, timeout=120.0) as client:
        answer = await client.delete(
            f"/api/v2/projects/{PROJECT}/services/attachments/attachment/{ATTACHMENT_ID}",
            params={"confirm_in_use": "true"},
            headers={"X-API-Key": wereld.api_key},
        )
    assert answer.status_code == 202, f"het opzeggen werd niet aangenomen: {answer.status_code} {answer.text[:500]}"
    task_id = answer.headers.get("Location", "").rsplit("/", 1)[-1] or answer.json().get("task_id")
    await _await_task(wereld, task_id)

    data = tasks_live.project_file(forgejo) or {}
    assert ATTACHMENT_ID not in str(data.get("services")), "de bijlage staat nog in het projectbestand"
    namespace = next(iter(tasks_live.project_namespaces(kubectl)))
    assert await tasks_live.wait_until(
        lambda: not tasks_live.secret_holds(kubectl, namespace, None, ATTACHMENT_CONTENT.decode()),
        timeout=CLUSTER_TIMEOUT,
    ), f"de inhoud van de bijlage staat nog in een Secret in {namespace}"


async def test_clone_database_copies_the_rows(wereld: Wereld, forgejo: forgejo_live.ForgejoApi) -> None:
    """CLONE_DATABASE: de rijen van de bron staan in de database van de deployment.

    De bron staat binnen de opstelling. Een bron buiten het cluster loopt over een
    chisel-tunnel, en die staat met zijn reden op de kaart.
    """
    await _run(
        wereld,
        "POST",
        f"/api/v2/projects/{PROJECT}/deployments/{DEPLOYMENT}/:clone-database",
        {
            "sourceHost": postgres_live.SERVICE_NAME,
            "sourcePort": 5432,
            "sourceUsername": postgres_live.admin_credentials()[0],
            "sourcePassword": postgres_live.admin_credentials()[1],
            "sourceDatabase": CLONE_SOURCE_DATABASE,
            "sourceSchema": CLONE_SOURCE_SCHEMA,
            "forceClone": True,
        },
    )

    assert await tasks_live.row_in_any_project_database(CLONE_SOURCE_CONTENT), (
        "geen database van dit project draagt de rij van de kloonbron"
    )
    # The next run of the project reads the generation from the file; without it the
    # deployment points back at the database from before the clone.
    assert tasks_live.clone_generation(forgejo, "postgresql-database") == 1, "de generatie staat niet in git"


async def test_clone_bucket_copies_the_objects(wereld: Wereld, forgejo: forgejo_live.ForgejoApi) -> None:
    """CLONE_BUCKET: de objecten van de bron staan in de bucket van de deployment."""
    await _run(
        wereld,
        "POST",
        f"/api/v2/projects/{PROJECT}/deployments/{DEPLOYMENT}/:clone-bucket",
        {
            "sourceHost": minio_live.host().split(":")[0],
            "sourcePort": int(minio_live.host().split(":")[1]),
            "sourceAccessKey": minio_live.admin_access_key(),
            "sourceSecretKey": minio_live.admin_secret_key(),
            "sourceBucket": CLONE_SOURCE_BUCKET,
            "sourceSecure": False,
            "forceClone": True,
        },
    )

    mc = minio_live.admin_mc()
    buckets = sorted(tasks_live.buckets_with_prefix())
    assert any(CLONE_SOURCE_OBJECT in mc.objects(bucket) for bucket in buckets), (
        f"geen bucket van dit project draagt {CLONE_SOURCE_OBJECT}: {buckets}"
    )
    assert tasks_live.clone_generation(forgejo, "minio-storage") == 1, "de generatie staat niet in git"


async def test_sleep_deployment_scales_it_down(wereld: Wereld, kubectl: kubernetes_live.Kubectl) -> None:
    """SLEEP_DEPLOYMENT: de werklast van deze deployment staat op nul, de andere niet."""
    answer = await _web(wereld, f"/projects/{PROJECT}/deployments/{DEPLOYMENT}/sleep")
    await _await_task(wereld, _task_id_from_fragment(answer))

    namespace = next(iter(tasks_live.project_namespaces(kubectl)))
    assert await tasks_live.wait_until(
        lambda: tasks_live.replicas_of(kubectl, namespace, DEPLOYMENT) == 0, timeout=CLUSTER_TIMEOUT
    ), f"de werklast van {DEPLOYMENT} staat niet op nul replicas"


async def test_wake_deployment_scales_it_back_up(wereld: Wereld, kubectl: kubernetes_live.Kubectl) -> None:
    """WAKE_DEPLOYMENT: de werklast draait weer."""
    answer = await _web(wereld, f"/projects/{PROJECT}/deployments/{DEPLOYMENT}/wake")
    await _await_task(wereld, _task_id_from_fragment(answer))

    namespace = next(iter(tasks_live.project_namespaces(kubectl)))
    assert await tasks_live.wait_until(
        lambda: (tasks_live.replicas_of(kubectl, namespace, DEPLOYMENT) or 0) > 0, timeout=CLUSTER_TIMEOUT
    ), f"de werklast van {DEPLOYMENT} draait na het wekken niet"


@pytest.mark.xfail(
    strict=True,
    reason="the backup pod in kind cannot reach PostgreSQL and MinIO beside the cluster (issue #362)",
)
async def test_backup_writes_a_run_to_the_destination(wereld: Wereld) -> None:
    """BACKUP: een backup-run met inhoud op de bestemming.

    De payload is die van de modal-wizard (``_handle_backup_restore_submit``); de wizard
    zelf heeft een browser nodig en wordt door de sandboxsuite gemeten. Wat hier gemeten
    wordt is de taak: dezelfde rij, dezelfde worker, dezelfde handler, met kopia uit de
    pin van de OPI-image en de bestemming in de wegwerp-MinIO.
    """
    task_id = await tasks_live.create_task_directly(
        TaskType.BACKUP,
        deployment=DEPLOYMENT,
        payload={
            "project_name": PROJECT,
            "deployment_name": DEPLOYMENT,
            "resource_types": ["database"],
            "trigger": "manual",
        },
    )
    await _await_task(wereld, task_id)

    runs = await tasks_live.backup_runs(DEPLOYMENT)
    assert runs, f"de bestemming {settings.BACKUP_S3_ENDPOINT} draagt geen enkele backup-run"
    assert tasks_live.backup_destination_has_objects(), f"de backup-bucket op {settings.BACKUP_S3_ENDPOINT} is leeg"
    wereld.backup_run_id = str(runs[0]["backup_run_id"])


@pytest.mark.xfail(
    strict=True,
    reason="the backup pod in kind cannot reach PostgreSQL and MinIO beside the cluster (issue #362)",
)
async def test_restore_puts_the_data_back(wereld: Wereld) -> None:
    """RESTORE: de backup van het vorige scenario terug in dezelfde deployment."""
    run_id = wereld.backup_run_id
    assert run_id, "geen backup-run om terug te zetten; het backup-scenario moet eerst slagen"
    runs = await tasks_live.backup_runs(DEPLOYMENT)
    items = next((run.get("items") or [] for run in runs if run.get("backup_run_id") == run_id), [])
    assert items, f"run {run_id} draagt geen items om terug te zetten"

    marker = f"{PREFIX}_na_de_backup"
    await tasks_live.add_marker_row(marker)

    task_id = await tasks_live.create_task_directly(
        TaskType.RESTORE,
        deployment=DEPLOYMENT,
        payload={
            "project_name": PROJECT,
            "backup_run_id": run_id,
            "target_deployment": DEPLOYMENT,
            "backup_items": items,
            "create_new_deployment": False,
            "source_deployment": DEPLOYMENT,
            "deployment_config": None,
            "config_source": None,
        },
    )
    await _await_task(wereld, task_id)

    # De rij van NA de backup hoort weg te zijn: dat is het bewijs dat de teruggezette
    # toestand die van de backup is en niet wat er nog stond.
    assert not await tasks_live.marker_row_exists(marker), (
        "de rij van na de backup staat er nog; dan is er niets teruggezet"
    )


async def test_rotate_credentials_replaces_the_secrets(wereld: Wereld, kubectl: kubernetes_live.Kubectl) -> None:
    """ROTATE_CREDENTIALS: nieuwe geheimen in het cluster, en ze werken ook echt.

    Niet alleen dat de waarde anders is: een rotatie die het Secret bijwerkt maar de
    afhankelijkheid niet, laat een applicatie met een wachtwoord dat nergens op past.
    """
    namespace = next(iter(tasks_live.project_namespaces(kubectl)))
    before = tasks_live.secret_snapshot(kubectl, namespace)

    await _run(
        wereld,
        "POST",
        f"/api/v2/projects/{PROJECT}/deployments/{DEPLOYMENT}/:rotate-credentials",
        {"services": ["postgresql-database", "minio-storage"]},
    )

    assert await tasks_live.wait_until(
        lambda: tasks_live.secret_snapshot(kubectl, namespace) != before, timeout=CLUSTER_TIMEOUT
    ), "geen enkel Secret in de namespace veranderde door de rotatie"
    assert await tasks_live.database_credentials_work(kubectl, namespace), (
        "het nieuwe databasewachtwoord uit het Secret werkt niet tegen PostgreSQL"
    )


async def test_rotate_project_key_replaces_the_keypair(
    wereld: Wereld, forgejo: forgejo_live.ForgejoApi, kubectl: kubernetes_live.Kubectl
) -> None:
    """ROTATE_PROJECT_KEY: een nieuw sleutelpaar in git EN in de namespace.

    Het Secret in de namespace is wat de CMP-sidecar gebruikt om de manifesten te openen;
    loopt dat achter op het projectbestand, dan rendert geen enkele sync meer.
    """
    before = (tasks_live.project_file(forgejo) or {}).get("config", {}).get("age-public-key")
    assert before

    # A platform admin with a bearer token and a fresh step-up, whose factor is set up the
    # way a user does it: in the portal.
    secret = await asyncio.to_thread(
        tasks_live.enroll_admin_otp, wereld.base_url, await tasks_live.admin_session_cookie()
    )
    token = await asyncio.to_thread(tasks_live.bearer_token)
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(base_url=wereld.base_url, timeout=120.0) as client:
        step_up = await client.post("/api/v2/step-up", json={"code": tasks_live.next_otp_code(secret)}, headers=headers)
        assert step_up.status_code == 200, f"step-up gave {step_up.status_code}: {step_up.text[:800]}"
        answer = await client.post(f"/api/v2/projects/{PROJECT}/:rotate-project-key", headers=headers)
    assert answer.status_code == 202, f"rotate gave {answer.status_code}: {answer.text[:800]}"
    await tasks_live.await_task_in_queue(answer.json()["task_id"], timeout=TASK_TIMEOUT)

    after = (tasks_live.project_file(forgejo) or {}).get("config", {}).get("age-public-key")
    assert after, "het projectbestand draagt geen publieke projectsleutel meer"
    assert after != before, "de publieke projectsleutel in het projectbestand is niet vervangen"

    namespace = next(iter(tasks_live.project_namespaces(kubectl)))
    assert await tasks_live.wait_until(
        lambda: tasks_live.namespace_key_matches(kubectl, namespace, after), timeout=CLUSTER_TIMEOUT
    ), f"het sleutel-Secret in {namespace} draagt de nieuwe sleutel niet"


async def test_create_project_from_a_complete_file_provisions_everything(
    wereld: Wereld, forgejo: forgejo_live.ForgejoApi, kubectl: kubernetes_live.Kubectl
) -> None:
    """CREATE_PROJECT met een volledig projectbestand: elke afhankelijkheid in EEN taak.

    Het eerste scenario meet de API-aanmaak, en die zet met opzet alleen de basis neer: een
    project zonder deployments heeft nog niets om uit te rollen. De aanmaak uit de wizard is
    de andere helft, en de zware: EEN taak die de namespace, de database, de bucket, het
    realm en de ArgoCD-Application neerzet. De payload is die van de wizard en van de
    goedkeuringsroute (``yaml_content`` plus ``rollout``), en de tekst komt uit de
    productiehelper die het portaal gebruikt.

    Een eigen project, want de keten hiervoor begint juist met een leeg project. Hij gaat
    aan het eind van dit scenario weer weg, zodat de eindinvariant over de hele groep blijft
    gelden: er blijft niets van staan.
    """
    task_id = await tasks_live.create_task_directly(
        TaskType.CREATE_PROJECT,
        deployment=None,
        payload={
            "project_name": tasks_live.FULL_PROJECT,
            "yaml_content": await tasks_live.full_project_yaml(),
            "is_new_project": True,
            "rollout": True,
        },
    )
    await tasks_live.await_task_in_queue(task_id, timeout=TASK_TIMEOUT)

    full = tasks_live.FULL_PROJECT
    data = tasks_live.project_file(forgejo, full)
    assert data is not None, "het projectbestand van de volledige aanmaak staat niet in git"
    missing = sorted(set(SERVICES) - tasks_live.project_services(data))
    assert not missing, f"diensten die de aanmaak niet vastlegde: {missing}"

    assert tasks_live.project_namespaces(kubectl, full), "de aanmaak maakte geen namespace"
    applications = tasks_live.argo_names(kubectl, "application", full)
    assert applications, "de aanmaak maakte geen ArgoCD-Application"
    for application in applications:
        status = (kubectl.get("application", application, "rig-system") or {}).get("status") or {}
        assert (status.get("sync") or {}).get("status") == "Synced", f"{application} is niet Synced: {status}"

    assert await tasks_live.databases_with_prefix(tasks_live.FULL_DATABASE_PREFIX), "de aanmaak maakte geen database"
    assert tasks_live.buckets_with_prefix(full), "de aanmaak maakte geen bucket"
    assert tasks_live.realm_exists(full), f"de aanmaak maakte geen realm {tasks_live.realm_name(full)}"

    answer = await _web(wereld, f"/projects/delete/{full}")
    await tasks_live.await_task_in_queue(_task_id_from_fragment(answer), timeout=TASK_TIMEOUT)
    assert await tasks_live.wait_until(
        lambda: not tasks_live.project_namespaces(kubectl, full), timeout=CLUSTER_TIMEOUT
    ), "de namespace van de volledige aanmaak staat er nog"


async def test_delete_component_removes_it(wereld: Wereld, forgejo: forgejo_live.ForgejoApi) -> None:
    """DELETE_COMPONENT: het component is weg uit het projectbestand en uit de deployment.

    It hangs on deployment main, so the delete is refused with 409 unless the caller states
    with ``confirm_in_use`` that it has seen that.
    """
    await _run(wereld, "DELETE", f"/api/v2/projects/{PROJECT}/components/{SECOND_COMPONENT}?confirm_in_use=true", {})

    data = tasks_live.project_file(forgejo) or {}
    names = {component.get("name") for component in data.get("components") or []}
    assert SECOND_COMPONENT not in names, f"{SECOND_COMPONENT} staat nog in het projectbestand"
    assert SECOND_COMPONENT not in tasks_live.component_images(data, DEPLOYMENT)


async def test_delete_deployment_leaves_nothing_of_it(
    wereld: Wereld, forgejo: forgejo_live.ForgejoApi, kubectl: kubernetes_live.Kubectl
) -> None:
    """DELETE_DEPLOYMENT: de tweede deployment is weg, de eerste staat er nog.

    Dat laatste is de helft die een scenario over twee deployments kan meten en een over
    een niet: een verwijdering die te breed opruimt, haalt de buur mee.
    """
    await _run(wereld, "DELETE", f"/api/v2/projects/{PROJECT}/{SECOND_DEPLOYMENT}", {})

    data = tasks_live.project_file(forgejo) or {}
    assert tasks_live.deployment_entry(data, SECOND_DEPLOYMENT) is None, "de deployment staat nog in het projectbestand"
    assert tasks_live.deployment_entry(data, DEPLOYMENT) is not None, "de andere deployment is meegegaan"

    assert await tasks_live.wait_until(
        lambda: not tasks_live.argo_names_for(kubectl, "application", SECOND_DEPLOYMENT), timeout=CLUSTER_TIMEOUT
    ), "de ArgoCD-Application van de verwijderde deployment staat er nog"
    assert await tasks_live.databases_of(SECOND_DEPLOYMENT) == set(), "de database van de deployment staat er nog"
    assert tasks_live.buckets_of(SECOND_DEPLOYMENT) == set(), "de bucket van de deployment staat er nog"

    # De scheiding tussen de twee niveaus: een deployment weghalen raakt niets van het
    # PROJECT. Het realm en de database van de buur horen te blijven staan.
    assert tasks_live.realm_exists(), "het realm van het project ging mee met de deployment"
    assert await tasks_live.databases_of(DEPLOYMENT), "de database van de andere deployment ging mee"
    assert tasks_live.project_namespaces(kubectl), "de namespace van het project ging mee"


async def test_delete_project_leaves_nothing_anywhere(
    wereld: Wereld, forgejo: forgejo_live.ForgejoApi, kubectl: kubernetes_live.Kubectl
) -> None:
    """DELETE_PROJECT: na het verwijderen staat er niets meer in een van de afhankelijkheden.

    De laatste invariant van de keten, en de enige die over alle zeven tegelijk gaat.
    """
    answer = await _web(wereld, f"/projects/delete/{PROJECT}")
    await tasks_live.await_task_in_queue(_task_id_from_fragment(answer), timeout=TASK_TIMEOUT)

    assert tasks_live.project_file(forgejo) is None, "het projectbestand staat nog in de zad-projects-repo"
    assert await tasks_live.wait_until(lambda: not tasks_live.project_namespaces(kubectl), timeout=CLUSTER_TIMEOUT), (
        "er staat nog een namespace van dit project"
    )
    assert await tasks_live.wait_until(
        lambda: not tasks_live.argo_names(kubectl, "application"), timeout=CLUSTER_TIMEOUT
    ), "er staat nog een ArgoCD-Application van dit project"
    assert not tasks_live.argo_names(kubectl, "appproject"), "er staat nog een AppProject van dit project"
    assert await tasks_live.databases_with_prefix() == set(), "er staat nog een database van dit project"
    assert await tasks_live.roles_with_prefix() == set(), "er staat nog een rol van dit project"
    assert tasks_live.buckets_with_prefix() == set(), "er staat nog een bucket van dit project"
    assert tasks_live.minio_users_with_prefix() == set(), "er staat nog een MinIO-gebruiker van dit project"
    assert not tasks_live.realm_exists(), "het realm van dit project staat er nog"
