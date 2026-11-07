"""Het opstarten van OPI van begin tot eind, tegen alle echte afhankelijkheden tegelijk.

Fase 1 en 2 meten elke connector en manager los. Het opstarten is meer dan de som daarvan:
een vaste volgorde over alle afhankelijkheden tegelijk, waarvan elke stap moet slagen op een
leeg cluster EN op een cluster dat al draait. Dat is wat hier gemeten wordt, met de echte
``lifespan`` en zonder een mock op een enkele stap.

Drie rondes, met na elke ronde dezelfde invariant: de hele toestand teruggelezen uit de
afhankelijkheden zelf.

1. op een leeg cluster;
2. direct nog een keer in hetzelfde proces (idempotent);
3. na een herstart, met de toestand van ronde 1 er al.

En het geval dat op productie telt: een afhankelijkheid die bij de start onbereikbaar is.
OPI hoort dan in de herhaallus te gaan en zichzelf te herstellen zodra ze terugkomt, zonder
halve toestand achter te laten.

Draai hem met ``task test-startup``; zonder de omgeving slaat de groep zichzelf over.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import socket
import time
from typing import TYPE_CHECKING, Any

import asyncpg
import httpx
import pytest
import pytest_asyncio
from opi.bootstrap.keycloak_setup import OPERATIONS_REALM_NAME
from opi.connectors.git_repo import GitCredentials, get_git_repo, reset_git_repos
from opi.connectors.mail import MAIL_LIMITER_PREFIX, MAIL_SENDER_NAME_PREFIX
from opi.core import readiness as readiness_module
from opi.core.cluster_config import namespace_labels
from opi.core.config import settings
from opi.core.database_pools import close_database_pools
from opi.core.readiness import get_readiness_state
from opi.core.startup import check_minio_availability, run_startup_tasks
from opi.handlers.sops import CMP_SOPS_ROLE_NAME, SOPS_SECRET_NAME
from opi.server import create_app, lifespan
from opi.services import project_store as project_store_module
from opi.services.project_store import get_project_store, reset_project_store
from prometheus_client import REGISTRY
from tests.integration import forgejo_live, keycloak_image, kubernetes_live, postgres_live, startup_live
from tests.integration.keycloak_image import assert_realm_consistent
from tests.integration.startup_live import PREFIX, PROJECT, RelayApi

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from fastapi import FastAPI

# One loop for the module: the boot opens pools and clients in the tests, and the module
# fixture closes them; on a loop per test that close lands on a loop that is already gone.
pytestmark = [
    pytest.mark.requires_infra,
    pytest.mark.startup_live,
    pytest.mark.serial,
    pytest.mark.asyncio(loop_scope="module"),
]

#: Het realm van het platform, zoals de bootstrap het aanlegt.
PLATFORM_REALM = "rig-platform"

#: Hoelang de herhaallus mag doen over het herstel nadat de afhankelijkheid terug is. Een
#: vangnet, geen wachtmechanisme: de toets wacht op de toestand zelf.
RECOVERY_TIMEOUT = 180.0

#: Een loopback-adres zonder luisteraar: een verbinding erheen wordt geweigerd in plaats van
#: te blijven hangen, zoals een database die nog niet antwoordt.
NOWHERE = "127.0.0.9"

#: Het wachtwoord van de wegwerprollen en van het Keycloak-mailaccount. Plaatshoudervorm,
#: en het bereikt alleen de opstelling van deze groep.
OPSTELLING_PASSWORD = "example-Wachtwoord-1234"


@pytest.fixture(scope="module")
def forgejo() -> Iterator[forgejo_live.ForgejoApi]:
    startup_live.assert_local()
    api = forgejo_live.ForgejoApi()
    yield api
    api.drop_token()


@pytest.fixture(scope="module")
def kubectl() -> kubernetes_live.Kubectl:
    return kubernetes_live.Kubectl()


@pytest.fixture(scope="module")
def relay() -> RelayApi:
    return RelayApi()


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def opstelling(
    forgejo: forgejo_live.ForgejoApi,
    kubectl: kubernetes_live.Kubectl,
    relay: RelayApi,
    tmp_path_factory: pytest.TempPathFactory,
) -> AsyncIterator[str]:
    """De wereld zoals OPI hem bij het opstarten aantreft, en de opruiming erna.

    De drie platformrepositories met een projectbestand erin, de platformdatabases van de
    gedeelde server, en het Secret waaruit Keycloak zijn mailwachtwoord leest. Verder niets:
    de namespaces, de realms, de clients, de migraties en de mailaccounts zijn juist wat het
    opstarten hoort neer te zetten.

    Antwoordt de publieke projectsleutel, want daarop leest de invariant het sleutel-Secret na.
    """
    private_key, public_key = startup_live.new_platform_keys()
    with pytest.MonkeyPatch.context() as patch:
        startup_live.patch_settings(patch, forgejo, platform_private_key=private_key, platform_public_key=public_key)
        startup_live.enable_background_tasks(patch)
        startup_live.resolve_platform_names(patch)
        # An empty cluster comes with an empty disk: a copy left by an earlier run holds the
        # history of a repository this fixture has since recreated.
        patch.setattr("opi.connectors.git_repo.GIT_REPOS_DIR", str(tmp_path_factory.mktemp("git-repos")))

        project_public, project_private = startup_live.new_project_keys()
        for name in forgejo_live.PLATFORM_REPOS:
            files = (
                {f"projects/{PROJECT}.yaml": startup_live.project_file(project_public, project_private)}
                if name == "zad-projects"
                else {"README.md": name}
            )
            forgejo.fresh_repo(name, files=files)

        await startup_live.prepare_platform_databases(OPSTELLING_PASSWORD)
        kubectl.apply(startup_live.keycloak_mail_secret_manifest(OPSTELLING_PASSWORD))
        startup_live.drop_namespaces()

        yield project_public

        await _drop_everything(kubectl, relay)


async def _drop_everything(kubectl: kubernetes_live.Kubectl, relay: RelayApi) -> None:
    """Alles weg wat deze groep neerzette, ook als een toets faalde."""
    await close_database_pools()
    api = keycloak_image.AdminApi(keycloak_image.basis_url())
    for realm in (OPERATIONS_REALM_NAME, PLATFORM_REALM):
        api.drop_realm(realm)
    relay.drop_platform_accounts()
    namespace = startup_live.platform_namespace()
    for secret in (settings.MAIL_PLATFORM_SECRET_NAME, settings.MAIL_KEYCLOAK_SECRET_NAME):
        kubectl.run("delete", "secret", secret, "-n", namespace, "--ignore-not-found")
    startup_live.drop_namespaces()
    await startup_live.drop_platform_databases()


async def _restart() -> None:
    """Vergeet module-state zodat een nieuw proces dit niet heeft staan.

    Warme git-kopie en prometheus REGISTRY moeten vers (ProjectStore reset --hard; REGISTRY weigert duplicaten).
    The database pools go too: a new process has none, and a pool left open would answer
    the readiness check while the database is down.
    """
    await close_database_pools()
    readiness_module._state = None
    reset_project_store()
    reset_git_repos()
    for collector in list(REGISTRY._collector_to_names):
        if type(collector).__name__ in ("OPICollector", "TracmallocCollector"):
            REGISTRY.unregister(collector)


# ==================== De invariant ====================


async def _assert_the_boot_built_everything(
    forgejo: forgejo_live.ForgejoApi,
    kubectl: kubernetes_live.Kubectl,
    relay: RelayApi,
    project_public_key: str,
    *,
    step: str,
) -> None:
    """Wat er na het opstarten moet staan, teruggelezen uit de afhankelijkheden zelf."""
    readiness = get_readiness_state()
    assert readiness.is_ready, f"{step}: OPI is niet ready: {readiness.summary()}"

    await _assert_the_database_is_migrated_and_restricted(step=step)
    _assert_the_realms_are_there(step=step)
    _assert_the_namespace_carries_its_key(kubectl, project_public_key, step=step)
    await _assert_the_platform_repos_are_warm(forgejo, step=step)
    _assert_both_mail_accounts_are_there(relay, kubectl, step=step)
    await _assert_oauth_works(step=step)
    assert await check_minio_availability() is True, f"{step}: de MinIO-check van het opstarten faalt"


async def _assert_the_database_is_migrated_and_restricted(*, step: str) -> None:
    """De eigen database op de laatste migratie, en elke platformdatabase dicht voor PUBLIC."""
    admin_user, admin_password = postgres_live.admin_credentials()
    conn = await postgres_live.connection_as(admin_user, admin_password, startup_live.OWN_DATABASE)
    try:
        assert await conn.fetchval("SELECT count(*) FROM alembic_version") == 1, (
            f"{step}: de database van OPI heeft geen enkele alembic-revisie"
        )
    finally:
        await conn.close()

    for database in (*startup_live.PLATFORM_DATABASES, startup_live.OWN_DATABASE):
        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await postgres_live.connection_as(startup_live.STRANGER_ROLE, OPSTELLING_PASSWORD, database)
    # En de rol van de database zelf komt er nog wel bij: een afsluiting die iedereen
    # buitensluit, legt de dienst plat in plaats van hem te beschermen. De relay is daarvan
    # het levende bewijs, want die draait op zijn eigen rol; zie RELAY_DATABASE.
    for database in startup_live.OWN_ROLE_DATABASES:
        eigen = await postgres_live.connection_as(database, OPSTELLING_PASSWORD, database)
        await eigen.close()


def _assert_the_realms_are_there(*, step: str) -> None:
    """De platformrealms met hun clients, en na elke stap een realm dat helemaal klopt."""
    api = keycloak_image.AdminApi(keycloak_image.basis_url())
    assert api.realm(PLATFORM_REALM)["realm"] == PLATFORM_REALM
    assert api.realm(OPERATIONS_REALM_NAME)["realm"] == OPERATIONS_REALM_NAME
    assert api.client("master", settings.KEYCLOAK_ADMIN_CLIENT_ID)["serviceAccountsEnabled"] is True
    assert api.client_or_none(OPERATIONS_REALM_NAME, f"{PLATFORM_REALM}-operations-manager") is not None
    for realm in (PLATFORM_REALM, OPERATIONS_REALM_NAME):
        assert_realm_consistent(api, realm, step=step)


def _assert_the_namespace_carries_its_key(
    kubectl: kubernetes_live.Kubectl, project_public_key: str, *, step: str
) -> None:
    """De namespace van het project, met de labels, het sleutel-Secret en de sleutellezer."""
    namespace = startup_live.namespace_on_cluster()
    stored = kubectl.get("namespace", namespace)
    assert stored is not None, f"{step}: namespace {namespace} staat niet op het cluster"
    labels = stored["metadata"].get("labels") or {}
    for key, value in namespace_labels(startup_live.CLUSTER).items():
        assert labels.get(key) == value, f"{step}: namespace {namespace} mist label {key}={value}"

    secret = kubectl.get("secret", SOPS_SECRET_NAME, namespace)
    assert secret is not None, f"{step}: {SOPS_SECRET_NAME} staat niet in {namespace}"
    # De vergelijking eerst, en daarna pas de assert: dit Secret draagt de PRIVE-helft van
    # de projectsleutel, en pytest zet beide kanten van een `in` in de foutmelding.
    carries_the_key = project_public_key in base64.b64decode(secret["data"]["key"]).decode()
    assert carries_the_key, f"{step}: het sleutel-Secret draagt niet de sleutel van het project"

    assert kubectl.exists("role", CMP_SOPS_ROLE_NAME, namespace), (
        f"{step}: de CMP-sleutellezer staat niet in {namespace}, dus ArgoCD rendert het project niet"
    )


async def _assert_the_platform_repos_are_warm(forgejo: forgejo_live.ForgejoApi, *, step: str) -> None:
    """De opgewarmde kopieen staan klaar, en het projectbestand uit git staat in de cache."""
    credentials = GitCredentials(username=forgejo_live.admin_user(), password=forgejo.token)
    for name in ("zad-deployments", "zad-argo-user-applications"):
        copy = get_git_repo(forgejo.clone_url(name), "main")
        assert await copy.tip(credentials) == forgejo.tip(name), f"{step}: geen warme kopie van {name}"

    namen = {project.name for project in get_project_store().get_all()}
    assert PROJECT in namen, f"{step}: het projectbestand uit git staat niet in de cache: {sorted(namen)}"


def _assert_both_mail_accounts_are_there(relay: RelayApi, kubectl: kubernetes_live.Kubectl, *, step: str) -> None:
    """De twee platformaccounts op de relay, met hun dagbudget, hun afzender en hun Secret."""
    assert relay.is_ready() is True, f"{step}: de relay antwoordt niet op /healthz/ready"
    for account in (settings.MAIL_PLATFORM_ACCOUNT, settings.MAIL_KEYCLOAK_ACCOUNT):
        assert relay.principal(account) is not None, f"{step}: mailaccount {account} staat niet op de relay"
        limiter = relay.settings_with_prefix(f"{MAIL_LIMITER_PREFIX}-{account}")
        assert limiter.get("rate"), f"{step}: mailaccount {account} heeft geen dagbudget op de relay"

    names = relay.settings_with_prefix(MAIL_SENDER_NAME_PREFIX)
    assert names.get(settings.MAIL_KEYCLOAK_ACCOUNT) == settings.MAIL_KEYCLOAK_FROM_NAME, (
        f"{step}: de weergavenaam van het Keycloak-account staat niet op de relay: {names}"
    )

    secret = kubectl.get("secret", settings.MAIL_PLATFORM_SECRET_NAME, startup_live.platform_namespace())
    assert secret is not None, f"{step}: het Secret met het platform-mailaccount is niet geschreven"
    assert base64.b64decode(secret["data"]["username"]).decode() == settings.MAIL_PLATFORM_ACCOUNT
    assert base64.b64decode(secret["data"]["from-address"]).decode() == startup_live.platform_mail_address()


async def _assert_oauth_works(*, step: str) -> None:
    """De OIDC-client die het opstarten zelf opzocht, en zijn discovery-document."""
    assert settings.OIDC_CLIENT_ID, f"{step}: het opstarten heeft geen OIDC-client gevonden"
    assert settings.OIDC_CLIENT_SECRET, f"{step}: de OIDC-client heeft geen secret"
    assert settings.OIDC_DISCOVERY_URL, f"{step}: er is geen discovery-URL"
    answer = httpx.get(settings.OIDC_DISCOVERY_URL, timeout=30.0)
    assert answer.status_code == 200, f"{step}: de discovery-URL antwoordt {answer.status_code}"
    assert answer.json()["token_endpoint"]


def _digest(value: str | None) -> str | None:
    """Een waarde als korte digest: vergelijkbaar, en niet te lezen in een foutmelding."""
    return None if value is None else hashlib.sha256(value.encode()).hexdigest()[:16]


async def _fingerprint(relay: RelayApi, kubectl: kubernetes_live.Kubectl) -> dict[str, Any]:
    """De hele toestand in een vergelijkbare vorm: hiermee is "niets dubbel, niets weg" te meten."""
    api = keycloak_image.AdminApi(keycloak_image.basis_url())
    conn = await postgres_live.admin_connection()
    try:
        databases = sorted(row["datname"] for row in await conn.fetch("SELECT datname FROM pg_database"))
        roles = sorted(row["rolname"] for row in await conn.fetch("SELECT rolname FROM pg_roles"))
    finally:
        await conn.close()
    secret = kubectl.get("secret", settings.MAIL_PLATFORM_SECRET_NAME, startup_live.platform_namespace()) or {}
    key_secret = kubectl.get("secret", SOPS_SECRET_NAME, startup_live.namespace_on_cluster()) or {}
    return {
        "realms": sorted(realm["realm"] for realm in api.get("/admin/realms")),
        "master_clients": sorted(client["clientId"] for client in api.clients("master")),
        "platform_clients": sorted(client["clientId"] for client in api.clients(PLATFORM_REALM)),
        "operations_clients": sorted(client["clientId"] for client in api.clients(OPERATIONS_REALM_NAME)),
        "platform_flows": sorted(flow["alias"] for flow in api.flows(PLATFORM_REALM)),
        "operations_flows": sorted(flow["alias"] for flow in api.flows(OPERATIONS_REALM_NAME)),
        "databases": databases,
        "roles": roles,
        "namespaces": sorted(name for name in kubectl.names("namespace") if PREFIX in name),
        "mail_accounts": sorted(relay.principals()),
        "mail_senders": relay.settings_with_prefix(MAIL_SENDER_NAME_PREFIX),
        # Als digest, want een vingerafdruk die niet klopt staat voluit in de foutmelding:
        # dat het wachtwoord ONVERANDERD is, is wat telt. Een tweede ronde die een nieuw
        # wachtwoord genereert, sluit ZAD uit zijn eigen mailaccount tot iemand het merkt.
        "mail_password_unchanged": _digest((secret.get("data") or {}).get("password")),
        # De resourceVersion van het sleutel-Secret: een apply met dezelfde inhoud laat hem
        # staan, dus een ronde die het Secret OPNIEUW schrijft valt hier op. Dat is de
        # dubbeling die de invariant zelf niet ziet, want de inhoud zou kloppen.
        "key_secret_version": (key_secret.get("metadata") or {}).get("resourceVersion"),
    }


def _assert_the_background_tasks_run(app: FastAPI, *, step: str) -> None:
    """De achtergrondtaken die ``lifespan`` start, staan er allemaal.

    Via ``app.state`` en niet via de log: ``lifespan`` vangt de fout van een scheduler die
    niet start en gaat door, dus een gemiste scheduler is alleen te zien aan wat er NIET in
    de toestand staat.
    """
    for name in (
        "task_service",
        "backup_scheduler",
        "resource_tuning_scheduler",
        "audit_retention_scheduler",
        "reconciliation_scheduler",
        "db_console_reaper",
        "image_pull_observer",
        "sleep_mode_scheduler",
        "git_monitor_tasks",
    ):
        assert getattr(app.state, name, None) is not None, f"{step}: {name} is niet gestart"
    # De reconcile-poll staat niet in app.state, maar in zijn eigen module.
    assert project_store_module._reconcile_poll_task is not None, f"{step}: de reconcile-poll loopt niet"


# ==================== De drie rondes ====================


async def test_the_whole_boot_runs_three_times_without_changing_anything(
    opstelling: str, forgejo: forgejo_live.ForgejoApi, kubectl: kubernetes_live.Kubectl, relay: RelayApi
) -> None:
    """Het echte opstarten op een leeg cluster, direct nog eens, en na een herstart.

    Een ronde is de echte ``lifespan``: de opstartstappen EN de achtergrondtaken die daarna
    starten, die bij het verlaten van het blok weer netjes moeten stoppen.
    """
    first_app = create_app()
    async with lifespan(first_app):
        step = "ronde 1 (leeg cluster)"
        await _assert_the_boot_built_everything(forgejo, kubectl, relay, opstelling, step=step)
        _assert_the_background_tasks_run(first_app, step=step)
        first = await _fingerprint(relay, kubectl)

        # Ronde 2: dezelfde stappen nog een keer in hetzelfde proces, zonder de
        # achtergrondtaken opnieuw te starten.
        assert await run_startup_tasks(first_app) is True, "ronde 2: het opstarten meldde niet dat alles klaar is"
        await _assert_the_boot_built_everything(forgejo, kubectl, relay, opstelling, step="ronde 2 (direct opnieuw)")
        assert await _fingerprint(relay, kubectl) == first, "ronde 2 veranderde de toestand"

    # Ronde 3: een herstart, met de toestand van ronde 1 er al.
    await _restart()
    second_app = create_app()
    async with lifespan(second_app):
        step = "ronde 3 (na een herstart)"
        await _assert_the_boot_built_everything(forgejo, kubectl, relay, opstelling, step=step)
        _assert_the_background_tasks_run(second_app, step=step)
        assert await _fingerprint(relay, kubectl) == first, "ronde 3 veranderde de toestand"


# ==================== Een storing bij het opstarten ====================


def _both_mail_accounts_exist(relay: RelayApi) -> bool:
    return all(
        relay.principal(account) is not None
        for account in (settings.MAIL_PLATFORM_ACCOUNT, settings.MAIL_KEYCLOAK_ACCOUNT)
    )


async def test_the_boot_recovers_when_postgresql_and_the_relay_are_down_at_first(
    opstelling: str,
    forgejo: forgejo_live.ForgejoApi,
    kubectl: kubernetes_live.Kubectl,
    relay: RelayApi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PostgreSQL en de mailrelay zijn weg als OPI start en komen terug: de lus repareert het.

    Dit is het gedrag dat op productie telt: een verse uitrol komt op voordat de database
    en de relay antwoorden, en wat dan niet in de lus wordt opgepakt, blijft uit tot iemand
    de pod herstart. Twee afhankelijkheden, want het zijn twee takken van de lus: de
    readiness-fasen en de mailaccounts, die juist NIET in de readiness staan.

    De lus tikt hier sneller dan de minuut van productie; dat is de enige knop die deze
    toets aan het gedrag draait.
    """
    await _restart()
    # De vorige toets liet de accounts staan; zonder dit zou de mailtak van de lus niets
    # meer te doen hebben en zou deze toets hem niet meten.
    relay.drop_platform_accounts()
    monkeypatch.setattr("opi.core.startup.STARTUP_RETRY_INTERVAL", 2)
    relay_url = settings.MAIL_RELAY_API_URL
    monkeypatch.setattr(settings, "MAIL_RELAY_API_URL", f"http://{NOWHERE}:8080")

    weg = {"nu": True}
    echt = socket.getaddrinfo

    def resolve(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        if weg["nu"] and host in startup_live.PLATFORM_DATABASE_NAMES:
            return echt(NOWHERE, port, *args, **kwargs)
        return echt(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)

    app = create_app()
    async with lifespan(app):
        readiness = get_readiness_state()
        assert readiness.database.ready is False, "de database antwoordde terwijl hij uit hoorde te staan"
        assert readiness.projects.ready is False, "de projectstap hoort niet te lopen zonder database"
        assert getattr(app.state, "startup_retry_task", None) is not None, "er loopt geen herhaallus"
        assert relay.principal(settings.MAIL_PLATFORM_ACCOUNT) is None, (
            "het mailaccount bestond al; dan meet deze toets de mailtak van de lus niet"
        )

        weg["nu"] = False
        monkeypatch.setattr(settings, "MAIL_RELAY_API_URL", relay_url)
        # Op de toestand wachten en niet op de klok, en op BEIDE takken: de mailaccounts
        # staan niet in de readiness, dus "ready" alleen zou te vroeg doorlopen.
        deadline = time.monotonic() + RECOVERY_TIMEOUT
        while time.monotonic() < deadline and not (readiness.is_ready and _both_mail_accounts_exist(relay)):
            await asyncio.sleep(1)

        await _assert_the_boot_built_everything(forgejo, kubectl, relay, opstelling, step="na het herstel")
