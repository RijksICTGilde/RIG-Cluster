"""Gedeelde hulp voor de groep ``startup_live``: het hele opstarten van OPI, echt.

``task test-startup`` zet de opstelling neer en draait deze groep ertegen: het wegwerpcluster
van fase 2 (Kubernetes, ArgoCD en Forgejo) plus de containers van fase 1 (PostgreSQL, Keycloak
en MinIO) en de mailrelay in zijn gepinde versie. OPI draait in het proces van de toets, met
de echte ``lifespan``; de uitrol-manifests meten de kustomize-toetsen.

Deze module zet de instellingen van OPI op die opstelling en levert het terugleeskanaal dat
er nog niet was: de management-API van de relay. Alle andere kanalen komen uit de helpers van
fase 1 en 2 (``postgres_live``, ``keycloak_image``, ``minio_live``, ``forgejo_live``,
``kubernetes_live``, ``argocd_live``), en daarom leest deze groep ook hun omgevingsvariabelen.

Eigen voorvoegsel ``zadstart``: ``kubernetes_live.drop_by_prefix`` ruimt elke namespace met
het voorvoegsel van die groep op, dus een gedeeld voorvoegsel zou betekenen dat de twee
groepen elkaars toestand weghalen.
"""

from __future__ import annotations

import socket
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx
import pyrage
import yaml
from opi.core.cluster_config import get_database_server, get_mail_from_address, get_namespace, get_prefixed_namespace
from opi.core.config import settings
from opi.core.platform_database_access import PLATFORM_BACKUP_ROLE, PLATFORM_DATABASE_ROLES
from opi.core.project_schema import latest_schema_version, validate_project_schema
from opi.utils.age import wrap_project_key_sync
from prometheus_client import REGISTRY
from tests.integration import argocd_live, forgejo_live, keycloak_image, kubernetes_live, minio_live, postgres_live
from tests.integration.live import STARTUP

if TYPE_CHECKING:
    import pytest

TIMEOUT = 30.0

#: Alles wat deze groep maakt begint hiermee. Niet ``zadlive``: zie de modulekop.
PREFIX = "zadstart"

#: Het cluster waarvoor deze opstelling doorgaat. Hetzelfde als in ``argocd_live``: de
#: managers leiden de ArgoCD- en platformnamespace eruit af, en op het wegwerpcluster staan
#: die waar ze op de sandbox staan.
CLUSTER = "sandboxed-local"

#: Het project dat in de projects-repo staat als OPI opstart, en de namespace die het
#: declareert. Het opstarten zet die namespace en zijn sleutel-Secret neer. The namespace
#: must be the project name: project validation rejects any other.
PROJECT = PREFIX
DECLARED_NAMESPACE = PROJECT

#: De database van OPI zelf op de wegwerpserver.
OWN_DATABASE = f"{PREFIX}_opi"

#: De platformdatabases van de gedeelde server, uit de lijst die het opstarten zelf
#: afsluit. Deze groep zet ze op de wegwerpserver neer, zodat
#: ``restrict_platform_databases`` echt werk heeft en de invariant kan nalezen wat hij deed.
PLATFORM_DATABASES = tuple(PLATFORM_DATABASE_ROLES)

#: De database van de mailrelay. De task maakt hem met de relay zelf aan, met een
#: wachtwoord dat deze groep niet kent: de relay draait al voordat de eerste toets begint.
#: Dat de afsluiting hem niet plat legt, is daarom te zien aan de relay (``is_ready``) en
#: niet aan een inlog met zijn rol.
RELAY_DATABASE = "mailrelay"

#: De platformdatabases waarvan deze groep de rol zelf aanmaakt, en dus het wachtwoord kent.
OWN_ROLE_DATABASES = tuple(name for name in PLATFORM_DATABASES if name != RELAY_DATABASE)

#: De rol die nergens bij hoort: hij meet de uitkomst van het afsluiten voor PUBLIC.
STRANGER_ROLE = f"{PREFIX}_vreemd"

#: De in-cluster namen waarop OPI de gedeelde PostgreSQL aanspreekt: de instelling en de
#: naam die ``cluster_config`` eruit afleidt. Beide wijzen naar de wegwerpserver, en de
#: storingstoets laat ze beide tijdelijk ergens anders uitkomen.
PLATFORM_DATABASE_NAMES = frozenset({postgres_live.SERVICE_NAME, get_database_server(CLUSTER)})

#: De rollen die de opstelling aanmaakt. De database ``mailrelay`` en zijn rol staan er al
#: als deze groep begint: de relay heeft ze nodig om te starten, dus ``task test-startup``
#: maakt die twee voordat hij de relay start.
OPSTELLING_ROLES = (*PLATFORM_DATABASES, PLATFORM_BACKUP_ROLE, STRANGER_ROLE)


def mail_api_url() -> str:
    """Het adres van de wegwerp-mailrelay, of een skip (in CI een fout)."""
    return STARTUP.omgeving("STARTUP_LIVE_MAIL_URL").rstrip("/")


def mail_admin() -> str:
    return STARTUP.omgeving("STARTUP_LIVE_MAIL_ADMIN")


def mail_admin_password() -> str:
    return STARTUP.omgeving("STARTUP_LIVE_MAIL_ADMIN_PASSWORD")


def assert_local() -> None:
    """Weiger tegen iets anders dan de wegwerpopstelling op deze machine te draaien.

    Elke afhankelijkheid door zijn eigen grendel: deze groep schrijft in alle zeven, en
    ``kubernetes_live`` en ``argocd_live`` hebben een andere grendel dan een hostnaam (de
    API-server van kind staat op een willekeurige poort).
    """
    STARTUP.assert_local(mail_api_url())
    postgres_live.assert_local()
    keycloak_image.assert_local(keycloak_image.basis_url())
    minio_live.assert_local()
    forgejo_live.assert_local()
    argocd_live.assert_local()
    kubernetes_live.assert_test_cluster()


class RelayApi:
    """Het terugleeskanaal van de relay: eigen HTTP, naast de connector onder toets.

    De management-API van Stalwart antwoordt een onbekend account met 200 en een
    ``error``-veld in plaats van met 404, dus dit kanaal kijkt naar het lichaam en niet naar
    de statuscode. Dezelfde valkuil als in ``opi/connectors/mail.py``, en precies daarom
    leest de toets niet terug met de connector die hij meet.
    """

    def __init__(self) -> None:
        STARTUP.assert_local(mail_api_url())
        self._url = mail_api_url()
        self._auth = (mail_admin(), mail_admin_password())

    def get(self, path: str) -> dict[str, Any]:
        answer = httpx.get(f"{self._url}{path}", auth=self._auth, timeout=TIMEOUT)
        assert answer.status_code == 200, f"GET {path}: {answer.status_code} {answer.text}"
        body = answer.json()
        assert isinstance(body, dict), f"GET {path} gaf geen object terug: {body!r}"
        return body

    def principal(self, name: str) -> dict[str, Any] | None:
        """Het account zoals de relay het heeft, of None als het er niet is."""
        body = self.get(f"/api/principal/{name}")
        if body.get("error"):
            return None
        data = body.get("data")
        return data if isinstance(data, dict) else body

    def principals(self) -> set[str]:
        items = (self.get("/api/principal?types=individual&page=1&limit=100").get("data") or {}).get("items") or []
        return {item["name"] if isinstance(item, dict) else item for item in items}

    def settings_with_prefix(self, prefix: str) -> dict[str, str]:
        """Een reeks instellingen, gesleuteld op het laatste deel van de sleutel.

        ``removeprefix`` omdat de relay het gevraagde voorvoegsel zelf afhaalt; doet een
        versie dat niet, dan leest deze methode hem nog steeds hetzelfde.
        """
        items = ((self.get(f"/api/settings/list?prefix={prefix}.").get("data") or {}).get("items")) or {}
        return {key.removeprefix(f"{prefix}."): value for key, value in items.items()}

    def is_ready(self) -> bool:
        return httpx.get(f"{self._url}/healthz/ready", timeout=TIMEOUT).status_code == 200

    def drop_platform_accounts(self) -> None:
        """Haal de twee platform-mailaccounts weg, zodat een volgende run leeg begint."""
        for account in (settings.MAIL_PLATFORM_ACCOUNT, settings.MAIL_KEYCLOAK_ACCOUNT):
            httpx.delete(f"{self._url}/api/principal/{account}", auth=self._auth, timeout=TIMEOUT)
        self.get("/api/reload")


def project_file(public_key: str, wrapped_private_key: str) -> str:
    """Het projectbestand dat in de projects-repo staat als OPI opstart.

    Een deployment op dit cluster, want wat het opstarten ermee doet is de namespace en het
    sleutel-Secret neerzetten. Het AGE-sleutelpaar hoort erbij: zonder publieke sleutel
    weigert ``_ensure_sops_secret_in_namespace``, en de private helft is ingepakt met de
    platformsleutel zoals een echt projectbestand hem draagt.

    De schemaversie komt uit het schema zelf en staat als tekenreeks in het bestand, en het
    resultaat gaat door ``validate_project_schema``: een fixture die het schema niet haalt,
    meet het opstarten tegen een bestand dat ZAD nooit zou accepteren.
    """
    content = {
        "schema-version": str(latest_schema_version()),
        "name": PROJECT,
        "display-name": "Opstartmeting",
        "description": "Project van de groep startup_live",
        "clusters": [CLUSTER],
        "users": [{"email": f"{PREFIX}@example.invalid", "roles": ["admin"]}],
        "services": ["publish-on-web"],
        "components": [
            {
                "name": "web",
                "type": "single",
                "ports": {"inbound": [8080], "outbound": [443]},
                "services": ["publish-on-web"],
            }
        ],
        "deployments": [
            {
                "name": "d1",
                "cluster": CLUSTER,
                "namespace": DECLARED_NAMESPACE,
                "components": [{"reference": "web", "image": kubernetes_live.WORKLOAD_IMAGE}],
            }
        ],
        "config": {"age-public-key": public_key, "age-private-key": wrapped_private_key},
    }
    validate_project_schema(content)
    return yaml.safe_dump(content, sort_keys=False)


def namespace_on_cluster() -> str:
    """De namespace van het project zoals hij op het cluster heet."""
    return get_prefixed_namespace(CLUSTER, DECLARED_NAMESPACE)


def platform_namespace() -> str:
    """De eigen namespace van OPI op dit cluster: daar landen de secrets die hij zelf schrijft."""
    return get_namespace(CLUSTER)


def platform_mail_address() -> str:
    """Het afzendadres dat de relay voor dit cluster draagt."""
    return get_mail_from_address(CLUSTER)


def keycloak_mail_secret_manifest(password: str) -> str:
    """Het Secret dat de bootstrap van een cluster rendert, met het wachtwoord van Keycloak.

    Zonder dit Secret meldt ``ensure_keycloak_mail_account`` "dit cluster heeft geen
    Keycloak-mail" en meet de toets die tak niet.
    """
    return (
        "apiVersion: v1\n"
        "kind: Secret\n"
        "metadata:\n"
        f"  name: {settings.MAIL_KEYCLOAK_SECRET_NAME}\n"
        f"  namespace: {platform_namespace()}\n"
        "stringData:\n"
        f"  {settings.MAIL_KEYCLOAK_SECRET_KEY}: {password}\n"
    )


def new_project_keys() -> tuple[str, str]:
    """Een AGE-sleutelpaar voor het project, met de private helft ingepakt zoals OPI dat doet."""
    identity = pyrage.x25519.Identity.generate()
    return str(identity.to_public()), wrap_project_key_sync(str(identity))


def new_platform_keys() -> tuple[str, str]:
    """De platformsleutel van de opstelling: zonder KEK is dit de sleutel die projectsleutels opent."""
    identity = pyrage.x25519.Identity.generate()
    return str(identity), str(identity.to_public())


def patch_settings(
    patch: pytest.MonkeyPatch, forgejo: forgejo_live.ForgejoApi, *, platform_private_key: str, platform_public_key: str
) -> None:
    """Zet de instellingen van OPI op de wegwerpopstelling.

    Alleen instellingen: geen enkele stap van het opstarten wordt vervangen. De DNS-stappen
    staan uit door hun eigen schakelaar leeg te laten (``TRANSIP_ACCOUNT_NAME``), want
    TransIP is een externe dienst zonder image om tegen te meten.

    De OIDC-instellingen staan er met opzet NIET bij: die vult het opstarten zelf, en dat is
    precies wat ``_setup_oauth`` en ``ensure_keycloak_credentials`` doen.
    """
    admin_user, admin_password = postgres_live.admin_credentials()

    patch.setattr(settings, "CLUSTER_MANAGER", CLUSTER)
    patch.setattr(settings, "SKIP_STARTUP_CHECKS", False)
    patch.setattr(settings, "OWN_DOMAIN", f"{PREFIX}.example.invalid")
    patch.setattr(settings, "ADDITIONAL_DOMAINS", "")

    # PostgreSQL: de servicenaam van het cluster, naar de wegwerpserver geleid door
    # resolve_platform_names. De connector accepteert alleen een in-cluster naam.
    patch.setattr(settings, "DATABASE_HOST", postgres_live.SERVICE_NAME)
    patch.setattr(settings, "DATABASE_ADMIN_NAME", admin_user)
    patch.setattr(settings, "DATABASE_ADMIN_PASSWORD", admin_password)
    patch.setattr(settings, "DATABASE_NAME", OWN_DATABASE)
    patch.setattr(settings, "DATABASE_APP_USER", admin_user)
    patch.setattr(settings, "DATABASE_APP_PASSWORD", admin_password)
    patch.setattr(settings, "DATABASE_RESTRICT_PUBLIC_CONNECT", "on")

    # Keycloak: dezelfde container als de groep keycloak_live, met de service-account-route
    # die de bootstrap zelf aanlegt.
    patch.setattr(settings, "KEYCLOAK_URL", keycloak_image.basis_url())
    patch.setattr(settings, "KEYCLOAK_ADMIN_USERNAME", keycloak_image.omgeving("KEYCLOAK_IMAGE_TEST_ADMIN"))
    patch.setattr(settings, "KEYCLOAK_ADMIN_PASSWORD", keycloak_image.omgeving("KEYCLOAK_IMAGE_TEST_ADMIN_PASSWORD"))
    patch.setattr(settings, "KEYCLOAK_ADMIN_CLIENT_ID", "opi-admin-service")
    patch.setattr(settings, "KEYCLOAK_ADMIN_CLIENT_SECRET", keycloak_image.SERVICE_ACCOUNT_SECRET)
    patch.setattr(settings, "KEYCLOAK_BOOTSTRAP_CONFIG", "local")
    # Zonder OTP op master: de opstelling heeft geen tweede factor om mee in te leveren, en
    # wat deze groep meet is de orde van de opstartstappen. Zelfde keuze als de
    # bootstraptoets in de groep keycloak_live.
    patch.setattr(settings, "KEYCLOAK_ENFORCE_MASTER_OTP", False)

    # MinIO: de gepinde server, met de mc die de OPI-image meebrengt.
    patch.setattr(settings, "MINIO_HOST", minio_live.host())
    patch.setattr(settings, "MINIO_ADMIN_ACCESS_KEY", minio_live.admin_access_key())
    patch.setattr(settings, "MINIO_ADMIN_SECRET_KEY", minio_live.admin_secret_key())
    patch.setattr(settings, "MINIO_USE_TLS", False)

    # ArgoCD op het wegwerpcluster, met het API-token van het account opi.
    patch.setattr(settings, "ARGOCD_HOST", argocd_live.host())
    patch.setattr(settings, "ARGOCD_PORT", argocd_live.port())
    patch.setattr(settings, "ARGOCD_TOKEN", argocd_live.token())
    patch.setattr(settings, "ARGOCD_USERNAME", "admin")
    patch.setattr(settings, "ARGOCD_PASSWORD", argocd_live.admin_password())
    patch.setattr(settings, "ARGOCD_USE_TLS", argocd_live.use_tls())
    patch.setattr(settings, "ARGOCD_VERIFY_SSL", False)

    # De mailrelay in zijn gepinde versie, met de fallback-admin uit zijn eigen config.
    patch.setattr(settings, "MAIL_RELAY_API_URL", mail_api_url())
    patch.setattr(settings, "MAIL_RELAY_ADMIN_USERNAME", mail_admin())
    patch.setattr(settings, "MAIL_RELAY_ADMIN_PASSWORD", f"plain:{mail_admin_password()}")
    patch.setattr(settings, "MAIL_RELAY_VERIFY_TLS", False)

    # De platformsleutel: hiermee pakt OPI de projectsleutel uit het projectbestand uit.
    patch.setattr(settings, "SOPS_AGE_PRIVATE_KEY", platform_private_key)
    patch.setattr(settings, "SOPS_AGE_PUBLIC_KEY", platform_public_key)
    patch.setattr(settings, "SOPS_AGE_KEY_FILE", None)
    patch.setattr(settings, "SOPS_AGE_KEY_CONTENT", None)
    patch.setattr(settings, "PROJECT_KEK_PRIVATE_KEY", "")
    patch.setattr(settings, "PROJECT_KEK_PUBLIC_KEY", "")

    # De DNS-stappen praten met TransIP en blijven uit; zie de kaart.
    patch.setattr(settings, "TRANSIP_ACCOUNT_NAME", "")
    patch.setattr(settings, "TRANSIP_PRIVATE_KEY", "")

    _patch_git_settings(patch, forgejo)


def _patch_git_settings(patch: pytest.MonkeyPatch, forgejo: forgejo_live.ForgejoApi) -> None:
    """De drie repositories die OPI beheert, op de Forgejo van het wegwerpcluster."""
    user = forgejo_live.admin_user()
    token = forgejo.token
    projects, argo, deployments = (forgejo.clone_url(name) for name in forgejo_live.PLATFORM_REPOS)

    patch.setattr(settings, "GIT_PROJECTS_SERVER_URL", projects)
    patch.setattr(settings, "GIT_PROJECTS_SERVER_USERNAME", user)
    patch.setattr(settings, "GIT_PROJECTS_SERVER_PASSWORD", f"plain:{token}")
    patch.setattr(settings, "GIT_PROJECTS_WEB_URL", forgejo_live.base_url())
    patch.setattr(settings, "GIT_PROJECTS_SERVER_FILE_PATH", f"projects/{PROJECT}.yaml")
    patch.setattr(settings, "GIT_PROJECTS_SERVER_BRANCH", "main")
    patch.setattr(settings, "GIT_ARGO_APPLICATIONS_URL", argo)
    patch.setattr(settings, "GIT_ARGO_APPLICATIONS_USERNAME", user)
    patch.setattr(settings, "GIT_ARGO_APPLICATIONS_PASSWORD", f"plain:{token}")
    patch.setattr(settings, "PROJECT_REPO_URL", deployments)
    patch.setattr(settings, "PROJECT_REPO_USERNAME", user)
    patch.setattr(settings, "PROJECT_REPO_PASSWORD", f"plain:{token}")


def enable_background_tasks(patch: pytest.MonkeyPatch) -> None:
    """Zet de achtergrondtaken van ``lifespan`` aan die een schakelaar hebben.

    Ze staan met opzet aan: wat deze groep meet is dat ze tegen echte afhankelijkheden
    zonder fout starten en bij het afsluiten weer netjes stoppen. Een scheduler die niet
    start wordt in ``lifespan`` gelogd en niet geworpen, dus de toets leest ``app.state``.
    """
    patch.setattr(settings, "ENABLE_GIT_MONITOR", True)
    patch.setattr(settings, "TASK_WORKER_ENABLED", True)
    patch.setattr(settings, "BACKUP_SCHEDULER_ENABLED", True)
    patch.setattr(settings, "RECONCILIATION_SCHEDULER_ENABLED", True)
    patch.setattr(settings, "SLEEP_MODE_SCHEDULER_ENABLED", True)
    patch.setattr(settings, "DB_CONSOLE_ENABLED", True)


def forget_collectors() -> None:
    """De eigen collectors uit het prometheus-register halen.

    ``REGISTRY`` weigert een duplicaat, en een nieuw proces heeft ze niet staan: zonder dit
    valt een tweede opstart in hetzelfde proces om op de registratie.
    """
    for collector in list(REGISTRY._collector_to_names):
        if type(collector).__name__ in ("OPICollector", "TracmallocCollector"):
            REGISTRY.unregister(collector)


def resolve_platform_names(patch: pytest.MonkeyPatch) -> None:
    """Laat de in-cluster namen van de gedeelde PostgreSQL naar de wegwerpserver wijzen.

    Dezelfde weg als ``postgres_live.resolve_service_name``, en om dezelfde reden: de
    connector accepteert alleen een in-cluster servicenaam, en die grendel hoort mee te
    draaien in plaats van uit te staan. Hier twee namen, want het opstarten leidt zijn
    adressen ook uit ``cluster_config`` af (``restrict_platform_databases``).
    """
    parsed = urlparse(postgres_live.dsn())
    address = socket.gethostbyname(parsed.hostname or "localhost")
    patch.setenv("PGHOSTADDR", address)

    echt = socket.getaddrinfo

    def resolve(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        return echt(address if host in PLATFORM_DATABASE_NAMES else host, port, *args, **kwargs)

    patch.setattr(socket, "getaddrinfo", resolve)


async def prepare_platform_databases(password: str) -> None:
    """Zet de platformdatabases en hun rollen neer, zoals de gedeelde server ze heeft.

    Herhaalbaar: een afgebroken run laat ze staan, en dan hoort deze stap ze te vinden in
    plaats van te struikelen. Een bestaande rol houdt zijn wachtwoord: dat van de relay is
    van de task, en het omzetten zou de relay bij zijn eerste herverbinding buitensluiten.
    """
    postgres_live.assert_local()
    conn = await postgres_live.admin_connection()
    try:
        for role in OPSTELLING_ROLES:
            if not await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", role):
                await conn.execute(f"CREATE ROLE \"{role}\" LOGIN PASSWORD '{password}'")
        for database in PLATFORM_DATABASES:
            if not await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", database):
                await conn.execute(f'CREATE DATABASE "{database}" OWNER "{database}"')
    finally:
        await conn.close()


async def drop_platform_databases() -> None:
    """Haal de databases en rollen van deze groep weg, ook die van een afgebroken run."""
    postgres_live.assert_local()
    conn = await postgres_live.admin_connection()
    try:
        for database in (*PLATFORM_DATABASES, OWN_DATABASE):
            await conn.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
        for role in OPSTELLING_ROLES:
            await conn.execute(f'DROP ROLE IF EXISTS "{role}"')
    finally:
        await conn.close()


def drop_namespaces() -> None:
    """Haal de namespaces van deze groep weg, ook die van een afgebroken run.

    Op het voorvoegsel zoals het op het cluster staat: ``get_prefixed_namespace`` zet er het
    voorvoegsel van dit cluster voor, dus ``zadstart`` heet daar ``rig-zadstart``.
    """
    kubernetes_live.drop_by_prefix(get_prefixed_namespace(CLUSTER, PREFIX))
