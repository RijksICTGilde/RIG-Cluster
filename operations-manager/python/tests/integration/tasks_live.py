"""Gedeelde hulp voor de groep ``tasks_live``: elke taak van ZAD van begin tot eind.

``task test-taken`` zet dezelfde opstelling neer als fase 3 (het wegwerpcluster van
``task test-cluster-up`` plus PostgreSQL, Keycloak, MinIO en de mailrelay als container) en
draait deze groep ertegen. OPI draait in het proces van de toets, met de echte ``lifespan``
en dus met de ``TaskWorker`` aan: een scenario maakt een taak aan zoals het portaal of de
zad-cli dat doet, de worker voert hem uit, en de toets wacht op de uitkomst.

De instellingen komen uit ``startup_live.patch_settings``: een tweede kopie zou betekenen
dat de twee groepen tegen een andere opstelling meten zodra er een instelling bij komt.
Wat hier bovenop gaat is wat deze groep eigen moet hebben: zijn eigen database, zijn eigen
voorvoegsel en de inloglijst waarmee hij een project MAG aanmaken.

Eigen voorvoegsel ``zadta``: ``kubernetes_live.drop_by_prefix``, ``postgres_live.drop_by_prefix``
en ``minio_live.drop_by_prefix`` ruimen op voorvoegsel op, dus een gedeeld voorvoegsel zou
betekenen dat twee groepen elkaars toestand weghalen.

De terugleeskanalen zijn die van fase 1 en 2, allemaal los van de code onder toets: een
eigen ``asyncpg`` voor PostgreSQL, ``mc`` voor MinIO, de admin-API voor Keycloak, de API van
Forgejo voor de drie repositories en een eigen ``kubectl`` voor het cluster.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import json
import re
import secrets
import socket
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

import asyncpg
import httpx
import pyrage
import pytest
import uvicorn
import yaml
from itsdangerous import TimestampSigner
from opi.api.router import SelfServiceComponent, SelfServiceProjectRequest
from opi.bootstrap.keycloak_setup import OPERATIONS_REALM_NAME
from opi.core.async_task_service import AsyncTaskService, TaskType
from opi.core.cluster_config import CLUSTER_CONFIG, get_prefixed_namespace
from opi.core.config import settings
from opi.handlers.project_file_handler import ProjectFileHandler
from opi.manager.backup import BackupManager, group_snapshots_into_runs
from opi.server import create_app
from opi.services.catalog.sleep_mode.manifests import WAKER_ROLE_LABEL
from opi.services.session_store import get_session_store
from opi.utils.project_utils import generate_self_service_project_yaml
from opi.utils.totp import TOTP_PERIOD, totp_now
from tests.image_pins import REPO_ROOT
from tests.integration import (
    argocd_live,
    forgejo_live,
    keycloak_image,
    kubernetes_live,
    minio_live,
    postgres_live,
    startup_live,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from fastapi import FastAPI

TIMEOUT = 30.0

#: Alles wat deze groep maakt begint hiermee. Het is ook het voorvoegsel van de
#: PROJECTNAAM, want die wordt afgeleid: ``generate_project_name`` maakt van een
#: woord van een naam de eerste vijf letters plus een achtervoegsel.
PREFIX = "zadta"

#: De naam die de aanmaaktaak invult, en het achtervoegsel dat hem voorspelbaar maakt.
#: Zonder vast achtervoegsel krijgt elke run een andere projectnaam, en dan kan de
#: opruiming van een afgebroken run hem niet meer vinden.
DISPLAY_NAME = "zadtask"
POSTFIX = "tsk"
PROJECT = f"{PREFIX}-{POSTFIX}"

#: Het cluster waarvoor deze opstelling doorgaat, hetzelfde als in fase 2 en 3.
CLUSTER = startup_live.CLUSTER

#: De database van OPI zelf. Een eigen, want twee groepen in dezelfde database zouden
#: elkaars taakrijen zien.
OWN_DATABASE = f"{PREFIX}_opi"

#: De beheerder die de scenario's zijn. Hij staat in de realm die het opstarten aanlegt
#: (``opi/configs/projects/operations-manager-local.yaml``), en via ``ALLOWED_EMAILS`` ook
#: op de inloglijst: zonder dat mag hij niets aanmaken.
ADMIN_EMAIL = "admin@sandbox.rijksapp.dev"
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "admin1234"  # staat zo in de blauwdruk van de wegwerprealm

#: De twee deployments van het scenario. Twee, want de vraag is niet alleen wat een taak
#: neerzet maar ook of hij binnen zijn eigen deployment blijft.
DEPLOYMENT = "main"
SECOND_DEPLOYMENT = "tweede"

#: De componenten van het scenario.
COMPONENT = "web"
SECOND_COMPONENT = "api"

#: De image van de werklast. Gereedschap van de meting, geen dienst die het platform
#: uitrolt; ``tests/test_test_images_follow_pins.py`` zondert hem daarom uit. Not the busybox
#: of ``kubernetes_live``: the project file carries no command, so busybox exits at once and
#: no deployment ever turns healthy. This one serves on the component's port 8080 as a
#: non-root user, as in ``test_sandbox_reallife``.
WORKLOAD_IMAGE = "nginxinc/nginx-unprivileged:1.27-alpine"

#: De image die de update-taak erin zet: een andere tag van dezelfde image, zodat de
#: meting de WIJZIGING ziet zonder een tweede image te hoeven halen.
UPDATED_IMAGE = "nginxinc/nginx-unprivileged:1.26-alpine"

#: The port that image serves on. A component published on the web needs one: ZAD refuses
#: publish-on-web on a component without an inbound port.
WORKLOAD_PORT = 8080

#: De diensten die dit project neemt. Alles waarvoor deze opstelling een echte
#: afhankelijkheid heeft: Keycloak, PostgreSQL en MinIO draaien in hun gepinde versie, de
#: rest is manifestwerk op het wegwerpcluster. SeaweedFS en Redis staan er niet bij, want
#: de opstelling heeft er geen server voor; die twee staan op de kaart als ``later``.
SERVICES = (
    "publish-on-web",
    "keycloak",
    # Not authorization-wall: its oauth2-proxy runs in the pod and discovers the realm at
    # the address OPI gave it, which is the disposable Keycloak on the host. No address
    # reaches it from both the host and a pod in kind with the same issuer, so the pod never
    # turns healthy. test_sandbox_all_services measures it where DNS is real.
    "metrics-scraper",
    "persistent-storage",
    "temp-storage",
    "postgresql-database",
    "minio-storage",
    "attachments",
    "user-env-vars",
)

#: Het voorvoegsel waarop de toetsen de toestand van het PROJECT terugleggen. Database- en
#: bucketnamen zijn van de projectnaam afgeleid, en de opruiming gaat ruimer (``PREFIX``):
#: die moet ook de eigen database van OPI en de kloonbronnen kunnen pakken, en de
#: eindinvariant mag juist alleen over het project gaan.
DATABASE_PREFIX = PROJECT.replace("-", "_")
BUCKET_PREFIX = PROJECT
#: MinIO users are named like the database role (``zadta_tsk_main``), not like the bucket.
#: With the bucket prefix the "no user left" check after a delete always passed.
MINIO_USER_PREFIX = DATABASE_PREFIX

#: De bestemming van de backup-scenario's: een bucket in de wegwerp-MinIO, met het
#: voorvoegsel dat ``BACKUP_S3_ORG_PREFIX`` eraan geeft.
BACKUP_BUCKET_PREFIX = f"rig-{PROJECT}"

#: Het tweede project van de groep: de aanmaak uit EEN volledig projectbestand, zoals de
#: wizard die doet. Een eigen project, want de keten begint juist met een leeg project.
FULL_PROJECT = f"{PREFIX}-vol"
FULL_DATABASE_PREFIX = FULL_PROJECT.replace("-", "_")

#: De bijlage waarmee de bijlage-taken gemeten worden.
ATTACHMENT_ID = f"{PREFIX}-bijlage"
ATTACHMENT_PATH = "/etc/meting/bijlage.txt"


def assert_local() -> None:
    """Weiger tegen iets anders dan de wegwerpopstelling op deze machine te draaien.

    Dezelfde zeven grendels als fase 3: deze groep schrijft in alle zeven afhankelijkheden.
    """
    startup_live.assert_local()


def namespace_on_cluster() -> str:
    """De namespace van het project zoals hij op het cluster heet."""
    return get_prefixed_namespace(CLUSTER, PROJECT)


def platform_namespace() -> str:
    return startup_live.platform_namespace()


def realm_name(project: str = PROJECT) -> str:
    """Het realm dat de Keycloak-dienst voor een project aanlegt."""
    return f"{project}-{CLUSTER}"


def patch_settings(
    patch: pytest.MonkeyPatch,
    forgejo: forgejo_live.ForgejoApi,
    *,
    platform_private_key: str,
    platform_public_key: str,
) -> None:
    """Zet de instellingen van OPI op de wegwerpopstelling, voor deze groep.

    De basis is die van fase 3; wat hier bovenop gaat is wat deze groep eigen heeft.
    ``ALLOWED_EMAILS`` en ``ADMIN_EMAILS`` zijn geen opstellingsdetail maar de voorwaarde
    van het eerste scenario: zonder inloglijst weigert ``authorize_claims`` de beheerder.
    """
    startup_live.patch_settings(
        patch, forgejo, platform_private_key=platform_private_key, platform_public_key=platform_public_key
    )
    patch.setattr(settings, "DATABASE_NAME", OWN_DATABASE)
    patch.setattr(settings, "GIT_PROJECTS_SERVER_FILE_PATH", f"projects/{PROJECT}.yaml")
    patch.setattr(settings, "ALLOWED_EMAILS", ADMIN_EMAIL)
    patch.setattr(settings, "ADMIN_EMAILS", ADMIN_EMAIL)
    patch.setattr(settings, "FIXED_PROJECT_POSTFIX", POSTFIX)
    # Een sessie is hier geen cookie met inhoud maar een ondertekend id; de toets tekent
    # met dezelfde sleutel als de server, dus die moet vast staan in plaats van per
    # proces gemunt te worden.
    patch.setattr(settings, "SECRET_KEY", f"{PREFIX}-sessiesleutel-voor-de-taakmeting-32")
    # De backup-bestemming: dezelfde wegwerp-MinIO, met de admin-sleutels. Kopia zelf komt
    # uit de pin van de OPI-image en staat in de PATH van de task.
    patch.setattr(settings, "BACKUP_S3_ENDPOINT", minio_live.host())
    patch.setattr(settings, "BACKUP_S3_ACCESS_KEY", minio_live.admin_access_key())
    patch.setattr(settings, "BACKUP_S3_SECRET_KEY", minio_live.admin_secret_key())
    patch.setattr(settings, "BACKUP_S3_USE_TLS", False)
    # The scrape token comes from Secret prometheus-metrics-auth on a cluster. Without it
    # metrics-scraper writes no Secret while the pod still references one, so the pod
    # never starts.
    patch.setattr(settings, "PROMETHEUS_METRICS_AUTH_TOKEN", f"{PREFIX}-scrape-token-for-the-task-run")
    # The project realms go to the Keycloak the cluster config names, not to the setting:
    # for sandboxed-local that is the shared sandbox Keycloak, which this group must never
    # reach. Point it at the disposable one, as ``KEYCLOAK_URL`` already is.
    patch.setitem(CLUSTER_CONFIG[CLUSTER], "keycloak_discovery_url", keycloak_image.basis_url())
    # This setup has MinIO and no SeaweedFS server. Where a cluster names a SeaweedFS host,
    # minio-storage is replaced for every new deployment, so drop the two keys: the cluster
    # then offers MinIO as before, the path the existing projects still run on. The path of
    # a new project (seaweedfs-storage) is not measured here; it stays on the map as later.
    patch.delitem(CLUSTER_CONFIG[CLUSTER], "seaweedfs_host", raising=False)
    patch.delitem(CLUSTER_CONFIG[CLUSTER], "seaweedfs_port", raising=False)
    # The clone source sits on the disposable PostgreSQL and MinIO, which resolve to
    # loopback here. The egress guard refuses any private address unless the cluster config
    # allows its range, and that knob exists for a cluster that needs one; the guard itself
    # stays on.
    patch.setitem(CLUSTER_CONFIG[CLUSTER], "external_clone_allowed_private_networks", ["127.0.0.0/8", "::1/128"])


def route_deployments_repo_through_host(
    patch: pytest.MonkeyPatch, forgejo: forgejo_live.ForgejoApi, config_dir: Path
) -> None:
    """Give the deployments repo the address ArgoCD reaches it on, as on a cluster.

    OPI writes ``PROJECT_REPO_URL`` into the project file, the Application and the
    repository Secret, and ArgoCD reads it from inside the cluster. On a cluster OPI runs
    there too, so one address serves both; here OPI runs on the host, where only the port
    mapping reaches Forgejo. The setting gets the in-cluster address, so everything OPI writes
    is what the sandbox gets, and a git config of this group sends the host's git calls for
    that address to the mapping.

    The header goes in that config as well: git binds ``http.<url>.extraHeader`` to the URL
    AFTER ``insteadOf``, so the header OPI binds to the in-cluster address is not sent.
    """
    in_cluster = f"{argocd_live.FORGEJO_SERVICE}/"
    on_host = f"{forgejo_live.base_url()}/"
    token = base64.b64encode(f"{forgejo_live.admin_user()}:{forgejo.token}".encode()).decode()
    config = config_dir / "gitconfig"
    config.write_text(
        f'[url "{on_host}"]\n\tinsteadOf = {in_cluster}\n[http "{on_host}"]\n\textraHeader = Authorization: Basic {token}\n'
    )
    config.chmod(0o600)
    patch.setenv("GIT_CONFIG_GLOBAL", str(config))
    patch.setattr(
        settings, "PROJECT_REPO_URL", argocd_live.repo_url_in_cluster(forgejo_live.admin_user(), "zad-deployments")
    )


def enable_background_tasks(patch: pytest.MonkeyPatch) -> None:
    """De achtergrondtaken van ``lifespan`` aan, de ``TaskWorker`` voorop.

    Zonder de worker blijft elke taak op ``pending`` staan en meet deze groep niets.
    """
    startup_live.enable_background_tasks(patch)


def resolve_platform_names(patch: pytest.MonkeyPatch) -> None:
    startup_live.resolve_platform_names(patch)


async def prepare_platform_databases(password: str) -> None:
    await startup_live.prepare_platform_databases(password)


async def drop_platform_databases() -> None:
    await startup_live.drop_platform_databases()


def free_port() -> int:
    """Een vrije poort op de loopback, voor de server van deze groep."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class AppUnderTest:
    """OPI als echte HTTP-server, in de event loop van de toets.

    Een echte server en geen ASGI-transport, zodat de helpers van de sandboxsuite
    (``tests/e2e/helpers/sandbox_api.py``) hier onveranderd op kunnen: die praten met een
    ``base_url``. Hij draait in DEZELFDE loop als de toets, want de ``lifespan`` opent
    daar zijn pools en de worker leeft in dezelfde loop; een server in een eigen thread
    zou een tweede loop betekenen en dus een tweede pool.
    """

    port: int = field(default_factory=free_port)
    _server: uvicorn.Server | None = None
    _serving: asyncio.Task[None] | None = None
    app: FastAPI | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def start(self, *, timeout: float = 300.0) -> None:
        """Start de server en wacht tot de ``lifespan`` klaar is.

        De opstartstappen praten met alle zeven afhankelijkheden, dus dit duurt; de toets
        wacht op de toestand (``server.started``) en niet op de klok.
        """
        self.app = create_app()
        config = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning", lifespan="on")
        self._server = uvicorn.Server(config)
        self._serving = asyncio.create_task(self._server.serve(), name=f"{PREFIX}-server")
        deadline = time.monotonic() + timeout
        while not self._server.started:
            if self._serving.done():
                await self._serving
                pytest.fail("de server stopte tijdens het opstarten")
            if time.monotonic() > deadline:
                pytest.fail(f"de server was na {timeout:.0f}s niet opgestart")
            await asyncio.sleep(0.2)

    async def stop(self) -> None:
        if self._server is None or self._serving is None:
            return
        self._server.should_exit = True
        await self._serving
        self._server = None
        self._serving = None


# ==================== De autorisaties die een taak start ====================


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def bearer_token() -> str:
    """Een echt SSO-token van de beheerder, langs de weg die de zad-cli loopt.

    Authorization code met PKCE op de publieke client ``zad-cli`` van de operations-realm,
    met een loopback-redirect: dat is de enige flow die die client aanbiedt (direct access
    grants staan er met opzet uit). Keycloak negeert de poort van een loopback-redirect, dus
    er hoeft niets te luisteren; alleen de ``code`` uit de omleiding is nodig.

    Het inlogformulier wordt ingevuld met de helpers van de groep ``keycloak_live``
    (``browser_client`` en ``form_action``), want die kennen de valkuilen van Keycloak 26.8
    rond Secure-cookies en relatieve formulier-URL's al.
    """
    base = keycloak_image.basis_url()
    realm_base = f"{base}/realms/{OPERATIONS_REALM_NAME}/protocol/openid-connect"
    redirect_uri = "http://127.0.0.1/callback"
    verifier, challenge = _pkce_pair()

    with keycloak_image.browser_client(base) as http:
        page = http.get(
            f"{realm_base}/auth",
            params={
                "client_id": settings.CLI_CLIENT_ID,
                "response_type": "code",
                "scope": "openid email profile",
                "redirect_uri": redirect_uri,
                "state": secrets.token_urlsafe(8),
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
        )
        assert page.status_code == 200, page.text[:2000]
        answer = http.post(
            keycloak_image.form_action(page.text, "kc-form-login"),
            data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD, "credentialId": ""},
            follow_redirects=False,
        )
        code = _code_from_redirect(answer, redirect_uri)
        token = http.post(
            f"{realm_base}/token",
            data={
                "grant_type": "authorization_code",
                "client_id": settings.CLI_CLIENT_ID,
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
            },
        )
    assert token.status_code == 200, f"token-endpoint gaf {token.status_code}: {token.text[:1000]}"
    access_token = token.json().get("access_token")
    assert access_token, f"geen access_token in het antwoord: {token.text[:500]}"
    return str(access_token)


def _code_from_redirect(answer: httpx.Response, redirect_uri: str) -> str:
    """De ``code`` uit de omleiding naar de loopback, hoeveel stappen er ook voor staan."""
    seen: list[str] = []
    for _ in range(5):
        if answer.status_code not in (301, 302, 303, 307, 308):
            break
        location = answer.headers.get("location", "")
        seen.append(location)
        if location.startswith(redirect_uri):
            code = parse_qs(urlparse(location).query).get("code", [""])[0]
            assert code, f"de omleiding draagt geen code: {location}"
            return code
        answer = httpx.get(location, timeout=TIMEOUT, follow_redirects=False)
    pytest.fail(f"geen loopback-omleiding met een code na het inloggen: {seen or answer.text[:1000]}")


async def session_cookie(*, step_up: bool = True) -> str:
    """Een ondertekende sessiecookie van de beheerder, voor de webroutes.

    Een paar taken (het verwijderen van een project, slapen en wekken, backup en restore)
    hebben geen API-endpoint: het portaal start ze met een sessie en een CSRF-token. Die
    sessie is sinds RC-390 een rij in ``sessions`` met een ondertekend id in de cookie, en
    die rij schrijft deze helper via de store van de server zelf. De sandboxvariant
    (``sandbox_api.make_session_cookie``) doet hetzelfde met een psql-pod; hier is de store
    in hetzelfde proces, dus de omweg over kubectl zou alleen een tweede weg zijn.

    ``step_up`` zet het stempel dat een verwijdering vraagt (RC-332); zonder stempel
    antwoordt het portaal met de OTP-dialoog in plaats van de taak te starten.
    """
    session_id = secrets.token_urlsafe(32)
    data: dict[str, Any] = {"user": {"sub": f"{PREFIX}-admin", "email": ADMIN_EMAIL, "name": "Taakmeting"}}
    data["user"]["login_at"] = time.time()
    if step_up:
        data["step_up_at"] = datetime.now(UTC).isoformat()
    await get_session_store().set(session_id, data)
    return TimestampSigner(settings.SECRET_KEY).sign(session_id.encode()).decode()


@dataclass
class WebRoutes:
    """De webroutes van het portaal, met een sessie en een CSRF-token ervoor.

    Alleen voor de taken zonder API-endpoint. De eisen zijn dezelfde drie als in
    ``sandbox_api.start_verwijdering``: een geldige sessiecookie, de ``csrf_token``-cookie
    terug in de header, en een ``Origin`` op dezelfde host.
    """

    base_url: str
    cookie: str

    def post(self, path: str, data: dict[str, Any] | None = None) -> httpx.Response:
        with httpx.Client(base_url=self.base_url, timeout=120.0, follow_redirects=False) as client:
            client.cookies.set("session", self.cookie, domain="127.0.0.1")
            landing = client.get("/projects")
            assert landing.status_code == 200, (
                f"de sessie wordt niet aanvaard: GET /projects gaf HTTP {landing.status_code}"
            )
            csrf = client.cookies.get("csrf_token")
            assert csrf, "geen csrf_token-cookie na GET /projects"
            return client.post(path, data=data or {}, headers={"x-csrf-token": csrf, "Origin": self.base_url})


async def admin_session_cookie() -> str:
    """A session cookie of the admin carrying the real Keycloak ``sub``.

    The OTP re-login compares the fresh id_token with the session user, so the forged
    ``sub`` of :func:`session_cookie` would be refused there.
    """
    admin = keycloak_image.AdminApi(keycloak_image.basis_url())
    sub = (await asyncio.to_thread(admin.user, OPERATIONS_REALM_NAME, ADMIN_USERNAME))["id"]
    session_id = secrets.token_urlsafe(32)
    data = {"user": {"sub": sub, "email": ADMIN_EMAIL, "name": "Taakmeting", "login_at": time.time()}}
    await get_session_store().set(session_id, data)
    return TimestampSigner(settings.SECRET_KEY).sign(session_id.encode()).decode()


def _allow_portal_redirect(base_url: str) -> None:
    """Add the address of this run's server to the redirect URIs of the portal client.

    In production the portal's own URL is there; this group serves OPI on a random port.
    """
    admin = keycloak_image.AdminApi(keycloak_image.basis_url())
    client = admin.client(OPERATIONS_REALM_NAME, settings.OIDC_CLIENT_ID or "")
    uris = set(client.get("redirectUris") or []) | {f"{base_url}/*"}
    admin.put(f"/admin/realms/{OPERATIONS_REALM_NAME}/clients/{client['id']}", {"redirectUris": sorted(uris)})


def enroll_admin_otp(base_url: str, cookie: str) -> str:
    """Set up the admin's second factor through the portal and return its raw secret.

    There is no API for this: a user does it on ``/account/otp``. The same three steps
    here: a fresh re-login at Keycloak, ``/start`` for the seed, ``/bevestigen`` with a code.
    """
    _allow_portal_redirect(base_url)
    with httpx.Client(base_url=base_url, timeout=120.0, follow_redirects=False) as portal:
        portal.cookies.set("session", cookie, domain="127.0.0.1")
        assert portal.get("/account/otp").status_code == 200
        csrf = portal.cookies.get("csrf_token")
        assert csrf, "no csrf_token cookie after GET /account/otp"
        headers = {"x-csrf-token": csrf, "Origin": base_url}

        to_keycloak = portal.get("/account/otp/herlogin")
        assert to_keycloak.status_code == 302, f"re-login gave HTTP {to_keycloak.status_code}"
        with keycloak_image.browser_client() as keycloak:
            page = keycloak.get(to_keycloak.headers["location"])
            assert page.status_code == 200, page.text[:2000]
            answer = keycloak.post(
                keycloak_image.form_action(page.text, "kc-form-login"),
                data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD, "credentialId": ""},
                follow_redirects=False,
            )
        back = answer.headers.get("location", "")
        assert back.startswith(base_url), f"Keycloak did not send the re-login back: {answer.status_code} {back}"
        returned = portal.get(back.removeprefix(base_url))
        assert returned.status_code == 303, f"the re-login was not accepted: {returned.text[:2000]}"

        registration = portal.post("/account/otp/start", headers=headers)
        assert registration.status_code == 200, f"/start gave HTTP {registration.status_code}"
        seed = re.search(r"<code>([A-Z2-7]+)</code>", registration.text)
        pending = re.search(r'name="pending" value="([^"]+)"', registration.text, re.DOTALL)
        assert seed, f"no seed in the registration page:\n{registration.text[:3000]}"
        assert pending, f"no pending registration in the page:\n{registration.text[:3000]}"
        secret = base64.b32decode(seed.group(1) + "=" * (-len(seed.group(1)) % 8)).decode()

        code, _ = totp_now(secret)
        confirmed = portal.post(
            "/account/otp/bevestigen",
            data={"pending": html.unescape(pending.group(1)), "otp": code},
            headers=headers,
        )
        assert confirmed.status_code == 303, f"the code was not accepted: {confirmed.text[:2000]}"
    return secret


def next_otp_code(secret: str) -> str:
    """A code for the next time step: the confirmation used up the current one (replay guard)."""
    return totp_now(secret, at=time.time() + TOTP_PERIOD)[0]


async def create_task_directly(task_type: TaskType, *, deployment: str | None, payload: dict[str, Any]) -> str:
    """Zet een taak in de rij met de payload die het portaal eromheen bouwt.

    Voor backup en restore, de twee taken die alleen uit de modal-wizard komen: hun payload
    wordt daar uit de wizardstaat opgebouwd (``_handle_backup_restore_submit``), en die
    staat heeft een browser nodig. De wizard zelf meet de sandboxsuite; wat hier gemeten
    wordt is de taak: dezelfde rij, dezelfde worker, dezelfde handler.
    """
    service = AsyncTaskService(CLUSTER)
    task = await service.create_task(
        task_type=str(task_type),
        project_name=PROJECT,
        deployment_name=deployment,
        cluster=CLUSTER,
        payload=payload,
    )
    return str(task["task_id"])


# ==================== De opruiming ====================


async def drop_everything(kubectl: kubernetes_live.Kubectl, forgejo: forgejo_live.ForgejoApi) -> None:
    """Alles weg wat deze groep neerzette, ook dat van een afgebroken run.

    Op het voorvoegsel, in elke afhankelijkheid: namespaces, ArgoCD-applicaties, databases
    en rollen, buckets en gebruikers, het realm, en de drie repositories.
    """
    assert_local()
    # The app-of-apps first: while it lives it recreates every Application dropped below,
    # from the argo repo or from its cache once the repo is gone, and with allowEmpty off it
    # prunes nothing. The fixture puts it back once the repositories are fresh.
    kubectl.run("delete", "application", APP_OF_APPS, "-n", argocd_live.ARGO_NAMESPACE, "--ignore-not-found=true")
    for repo in forgejo_live.PLATFORM_REPOS:
        forgejo.drop_repo(repo)
    drop_argo_applications(kubectl)
    kubernetes_live.drop_by_prefix(get_prefixed_namespace(CLUSTER, PREFIX))
    await postgres_live.drop_by_prefix(PREFIX)
    minio_live.drop_by_prefix(PREFIX)
    drop_backup_buckets()
    api = keycloak_api()
    api.drop_realm(realm_name())
    api.drop_realm(realm_name(FULL_PROJECT))


def drop_argo_applications(kubectl: kubernetes_live.Kubectl) -> None:
    """De Applications en AppProjects van dit project weg, finalizers eerst.

    Zonder het weghalen van de finalizer houdt ArgoCD een Application vast tot de cascade
    klaar is, en dan hangt de opruiming net zo lang als de valkuil die fase 2 meet.
    """
    for kind in ("application", "appproject"):
        for name in sorted(n for n in kubectl.names(kind, "rig-system") if n.startswith(PREFIX)):
            kubectl.run(
                "patch", kind, name, "-n", "rig-system", "--type", "merge", "-p", '{"metadata":{"finalizers":[]}}'
            )
            kubectl.run("delete", kind, name, "-n", "rig-system", "--ignore-not-found=true")


#: What the bootstrap places in the argo-applications repo before any project exists.
BOOTSTRAP_FOLDER = REPO_ROOT / "bootstrap/argo-user-applications/_bootstrap"


def fresh_platform_repos(forgejo: forgejo_live.ForgejoApi) -> None:
    """De drie platformrepositories leeg, zoals OPI ze op een leeg cluster aantreft.

    The argo repo carries the bootstrap folder, like ``sandbox:init-forgejo`` puts it there.
    """
    bootstrap = {f"{CLUSTER}/_bootstrap/{path.name}": path.read_text() for path in BOOTSTRAP_FOLDER.glob("*.yaml")}
    for name in forgejo_live.PLATFORM_REPOS:
        extra = bootstrap if name == "zad-argo-user-applications" else {}
        forgejo.fresh_repo(name, files={"README.md": name, **extra})


async def wait_until(predicate: Callable[[], bool], *, timeout: float, interval: float = 3.0) -> bool:
    """Wacht op een toestand in de afhankelijkheden zelf, niet op de klok.

    De taak is al klaar als een scenario hier komt; wat hier op gewacht wordt is wat ERNA
    in het cluster gebeurt, zoals een namespace die verdwijnt nadat de taak hem opzegde.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


# ==================== De terugleeskanalen ====================


def project_file(forgejo: forgejo_live.ForgejoApi, project: str = PROJECT) -> dict[str, Any] | None:
    """Het gecommitte projectbestand, zoals Forgejo het heeft.

    De git-stand is de waarheid over wat een taak vastlegde: wat alleen in het geheugen
    van de store staat, is na een herstart weg.
    """
    content = forgejo.file_content("zad-projects", f"projects/{project}.yaml")
    if content is None:
        return None
    loaded = yaml.safe_load(content)
    return loaded if isinstance(loaded, dict) else None


def project_services(data: dict[str, Any]) -> set[str]:
    """De namen van de diensten in een projectbestand, in welke vorm ze ook staan.

    Een dienst staat er als kale tekenreeks, als ``{name, config}`` of in de oude
    eenkeuzevorm ``{dienst: {...}}``; de naam is wat de toets vergelijkt.
    """
    names: set[str] = set()
    for entry in data.get("services") or []:
        if isinstance(entry, str):
            names.add(entry)
        elif isinstance(entry, dict):
            name = entry.get("name") or entry.get("reference")
            if name is None:
                keys = [key for key in entry if key not in ("config", "schema-version")]
                name = keys[0] if len(keys) == 1 else None
            if isinstance(name, str):
                names.add(name)
    return names


def deployment_entry(data: dict[str, Any], deployment: str) -> dict[str, Any] | None:
    for entry in data.get("deployments") or []:
        if isinstance(entry, dict) and entry.get("name") == deployment:
            return entry
    return None


def component_images(data: dict[str, Any], deployment: str) -> dict[str, str]:
    """Welke image elk component van deze deployment draagt, volgens het projectbestand."""
    entry = deployment_entry(data, deployment) or {}
    return {
        str(component.get("reference")): str(component.get("image", ""))
        for component in entry.get("components") or []
        if isinstance(component, dict)
    }


def project_namespaces(kubectl: kubernetes_live.Kubectl, project: str = PROJECT) -> set[str]:
    """De namespaces die OPI voor dit project maakte, op zijn eigen label.

    Op het label en niet op de naam: zo valt een infra-namespace er ook onder, en de toets
    hoeft het voorvoegsel van het cluster niet na te bouwen.
    """
    return {name for name in kubectl.names("namespace", selector="created-by=operations-manager") if project in name}


def argo_names(kubectl: kubernetes_live.Kubectl, kind: str, project: str = PROJECT) -> set[str]:
    """De Applications of AppProjects van een project, zoals het cluster ze heeft."""
    return {name for name in kubectl.names(kind, "rig-system") if name.startswith(project)}


def keycloak_api() -> keycloak_image.AdminApi:
    """Het terugleeskanaal van Keycloak: de admin-API, los van de connector onder toets."""
    return keycloak_image.AdminApi(keycloak_image.basis_url())


def new_platform_keys() -> tuple[str, str]:
    """De platformsleutel van de opstelling, zoals fase 3 hem maakt."""
    return startup_live.new_platform_keys()


#: The app-of-apps of the sandbox, the Application that syncs the argo repo. It has no
#: finalizer, so deleting it leaves the Applications it made in place.
APP_OF_APPS = "user-applications"
APP_OF_APPS_MANIFEST = (
    REPO_ROOT / "bootstrap/rig-system/kustomize/overlays/sandboxed-local/argocd-application-user-applications.yaml"
)


def install_app_of_apps(kubectl: kubernetes_live.Kubectl) -> None:
    """The app-of-apps of the sandbox, unchanged, on the fresh argo repo."""
    kubectl.apply(APP_OF_APPS_MANIFEST.read_text())


#: Where the platform key lives on the cluster, and where the CMP of the app-of-apps reads it.
PLATFORM_KEY_SECRET = "sops-age-key"
PLATFORM_KEY_MOUNT = "/sops/sops-age-key/key"
PLATFORM_KEY_SIDECAR = "cmp-platform"
PLATFORM_KEY_TIMEOUT = 180


def platform_keys(kubectl: kubernetes_live.Kubectl) -> tuple[str, str]:
    """The platform key of this cluster: the one in Secret `sops-age-key`, made on first use.

    The bootstrap puts it there on a real cluster. It lives as long as the disposable
    cluster rather than one run, because the CMP of the app-of-apps reads it from a MOUNTED
    file: a replaced key reaches that file only after the kubelet's sync period, and a
    render in between fails with the old key and stays cached as a generation error.
    """
    secret = kubectl.get("secret", PLATFORM_KEY_SECRET, argocd_live.ARGO_NAMESPACE)
    encoded = ((secret or {}).get("data") or {}).get("key")
    if encoded:
        private_key = base64.b64decode(encoded).decode().strip()
        return private_key, str(pyrage.x25519.Identity.from_str(private_key).to_public())
    private_key, public_key = new_platform_keys()
    kubectl.apply(
        yaml.safe_dump(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": PLATFORM_KEY_SECRET, "namespace": argocd_live.ARGO_NAMESPACE},
                "type": "Opaque",
                "stringData": {"key": private_key},
            }
        )
    )
    return private_key, public_key


async def wait_for_mounted_platform_key(kubectl: kubernetes_live.Kubectl, private_key: str) -> None:
    """Wait until the CMP sidecar sees this key in its mounted file (only on a new key)."""

    def mounted() -> bool:
        output, code = kubectl.run(
            "exec",
            "-n",
            argocd_live.ARGO_NAMESPACE,
            "deploy/argocd-repo-server",
            "-c",
            PLATFORM_KEY_SIDECAR,
            "--",
            "cat",
            PLATFORM_KEY_MOUNT,
        )
        return code == 0 and output.strip() == private_key

    assert await wait_until(mounted, timeout=PLATFORM_KEY_TIMEOUT), (
        f"the CMP sidecar did not see the platform key in {PLATFORM_KEY_MOUNT} within {PLATFORM_KEY_TIMEOUT}s"
    )


async def databases_with_prefix(prefix: str = DATABASE_PREFIX) -> set[str]:
    """De databases van dit PROJECT. Niet die van OPI zelf: die heet anders."""
    conn = await postgres_live.admin_connection()
    try:
        rows = await conn.fetch("SELECT datname FROM pg_database WHERE datname LIKE $1", f"{prefix}%")
        return {row["datname"] for row in rows}
    finally:
        await conn.close()


async def roles_with_prefix(prefix: str = DATABASE_PREFIX) -> set[str]:
    conn = await postgres_live.admin_connection()
    try:
        rows = await conn.fetch("SELECT rolname FROM pg_roles WHERE rolname LIKE $1", f"{prefix}%")
        return {row["rolname"] for row in rows}
    finally:
        await conn.close()


async def databases_of(deployment: str) -> set[str]:
    """De databases waarvan de naam deze deployment noemt."""
    return {name for name in await databases_with_prefix() if deployment.replace("-", "_") in name}


def clone_generation(forgejo: forgejo_live.ForgejoApi, service: str) -> int | None:
    """The generation of this service on the first deployment, as the project file in git has it."""
    return ProjectFileHandler().get_deployment_service_generation(project_file(forgejo) or {}, DEPLOYMENT, service)


def schema_entry(data: Any, postfix: str) -> dict[str, Any] | None:
    """The entry for this schema postfix in the project file, wherever its service keeps it."""
    if isinstance(data, dict):
        for key, value in data.items():
            if key == "schemas" and isinstance(value, list):
                for item in value:
                    if isinstance(item, dict) and item.get("postfix") == postfix:
                        return item
            found = schema_entry(value, postfix)
            if found is not None:
                return found
    elif isinstance(data, list):
        for item in data:
            found = schema_entry(item, postfix)
            if found is not None:
                return found
    return None


async def schemas_in(database: str) -> set[str]:
    conn = await postgres_live.admin_connection(database)
    try:
        rows = await conn.fetch("SELECT nspname FROM pg_namespace WHERE nspname NOT LIKE 'pg_%'")
        return {row["nspname"] for row in rows} - {"information_schema"}
    finally:
        await conn.close()


def buckets_with_prefix(prefix: str = BUCKET_PREFIX) -> set[str]:
    mc = minio_live.admin_mc()
    return {
        str(entry.get("key", "")).strip("/")
        for entry in mc.json_lines("ls", "--json", mc.alias)
        if str(entry.get("key", "")).strip("/").startswith(prefix)
    }


def buckets_of(deployment: str) -> set[str]:
    return {name for name in buckets_with_prefix() if deployment in name}


def minio_users_with_prefix(prefix: str = MINIO_USER_PREFIX) -> set[str]:
    mc = minio_live.admin_mc()
    return {
        str(entry.get("accessKey", ""))
        for entry in mc.json_lines("admin", "user", "list", mc.alias, "--json")
        if str(entry.get("accessKey", "")).startswith(prefix)
    }


def realm_exists(project: str = PROJECT) -> bool:
    return keycloak_api().status(f"/admin/realms/{realm_name(project)}") == 200


def argo_names_for(kubectl: kubernetes_live.Kubectl, kind: str, deployment: str) -> set[str]:
    """De Applications of AppProjects van dit project die deze deployment noemen."""
    return {name for name in argo_names(kubectl, kind) if deployment in name}


def _secrets_in(kubectl: kubernetes_live.Kubectl, namespace: str) -> list[dict[str, Any]]:
    output, code = kubectl.run("get", "secret", "-n", namespace, "-o", "json")
    if code != 0:
        return []
    items = json.loads(output).get("items")
    return items if isinstance(items, list) else []


def _secret_items(kubectl: kubernetes_live.Kubectl, namespace: str) -> dict[str, dict[str, str]]:
    """Elk Secret in de namespace, met zijn waarden ontsleuteld uit base64.

    Een niet-tekstuele waarde blijft base64: wat de toetsen hier zoeken is tekst, en een
    binaire waarde die niet decodeert hoort geen fout te zijn.
    """
    result: dict[str, dict[str, str]] = {}
    for item in _secrets_in(kubectl, namespace):
        name = ((item.get("metadata") or {}).get("name")) or ""
        values: dict[str, str] = {}
        for key, encoded in (item.get("data") or {}).items():
            try:
                values[key] = base64.b64decode(encoded).decode()
            except ValueError, UnicodeDecodeError:
                values[key] = str(encoded)
        result[name] = values
    return result


def secret_holds(kubectl: kubernetes_live.Kubectl, namespace: str, key: str | None, value: str) -> bool:
    """Draagt een Secret in deze namespace deze waarde, eventueel onder deze sleutel?"""
    for values in _secret_items(kubectl, namespace).values():
        for name, stored in values.items():
            if (key is None or name == key) and value in stored:
                return True
    return False


def secret_snapshot(kubectl: kubernetes_live.Kubectl, namespace: str) -> dict[str, dict[str, str]]:
    """Elk Secret in de namespace, zoals het nu is. De rotatietoets vergelijkt hiermee."""
    return _secret_items(kubectl, namespace)


def namespace_key_matches(kubectl: kubernetes_live.Kubectl, namespace: str, public_key: str) -> bool:
    """Draagt het sleutel-Secret van de namespace deze publieke sleutel?"""
    return secret_holds(kubectl, namespace, None, public_key)


def image_in_use(kubectl: kubernetes_live.Kubectl, namespace: str, image: str) -> bool:
    """Draagt een deployment in deze namespace deze image in zijn pod-spec?"""
    for name in kubectl.names("deployment", namespace):
        spec = ((kubectl.get("deployment", name, namespace) or {}).get("spec")) or {}
        containers = (((spec.get("template") or {}).get("spec")) or {}).get("containers") or []
        if any(container.get("image") == image for container in containers):
            return True
    return False


def replicas_of(kubectl: kubernetes_live.Kubectl, namespace: str, deployment: str) -> int | None:
    """Het aantal gevraagde replicas van de werklasten van deze deployment.

    Op het ``deployment``-label dat ``manifests/deployment.yaml.jinja`` zet, want de
    kubernetes-deployment heet naar het component en niet naar de ZAD-deployment. Not the
    waker: it carries the same label and is meant to run while the deployment sleeps.
    """
    ((role_key, waker_role),) = WAKER_ROLE_LABEL.items()
    names = kubectl.names("deployment", namespace, selector=f"deployment={deployment},{role_key}!={waker_role}")
    if not names:
        return None
    total = 0
    for name in names:
        spec = ((kubectl.get("deployment", name, namespace) or {}).get("spec")) or {}
        total += int(spec.get("replicas", 0))
    return total


async def database_credentials_work(kubectl: kubernetes_live.Kubectl, namespace: str) -> bool:
    """Werkt het databasewachtwoord uit de namespace werkelijk tegen PostgreSQL?

    Dit is wat een rotatie moet opleveren: een nieuw geheim in het cluster EN in de
    afhankelijkheid. Een Secret met een wachtwoord dat nergens op past, ziet een toets die
    alleen de waarde vergelijkt niet.
    """
    for values in _secret_items(kubectl, namespace).values():
        user = values.get("DATABASE_SERVER_USER") or values.get("username")
        password = values.get("DATABASE_PASSWORD") or values.get("password")
        database = values.get("DATABASE_DB") or values.get("dbname")
        if not (user and password and database):
            continue
        try:
            conn = await postgres_live.connection_as(user, password, database)
        except asyncpg.PostgresError, OSError:
            continue
        try:
            return await conn.fetchval("SELECT 1") == 1
        finally:
            await conn.close()
    return False


async def row_in_any_project_database(value: str) -> bool:
    """Staat deze tekst in een tabel van een database van dit project?

    De kloontaak kiest zelf het doelschema, dus de toets zoekt de rij in plaats van de
    naam van dat schema na te bouwen.
    """
    for database in sorted(await databases_with_prefix()):
        conn = await postgres_live.admin_connection(database)
        try:
            tables = await conn.fetch(
                "SELECT table_schema, table_name FROM information_schema.tables WHERE table_type = 'BASE TABLE' "
                "AND table_schema NOT IN ('pg_catalog', 'information_schema')"
            )
            for table in tables:
                if await _table_holds(conn, table["table_schema"], table["table_name"], value):
                    return True
        finally:
            await conn.close()
    return False


async def _table_holds(conn: asyncpg.Connection, schema: str, table: str, value: str) -> bool:
    """Draagt een tekstkolom van deze tabel deze waarde?"""
    columns = await conn.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = $1 AND table_name = $2 "
        "AND data_type IN ('text', 'character varying')",
        schema,
        table,
    )
    for column in columns:
        found = await conn.fetchval(
            f'SELECT count(*) FROM "{schema}"."{table}" WHERE "{column["column_name"]}" = $1',
            value,
        )
        if found:
            return True
    return False


#: De tabel waarin de restore-meting zijn markering zet.
MARKER_TABLE = f"{PREFIX}_markering"


async def add_marker_row(marker: str) -> None:
    """Zet een rij in de database van de deployment, NA de backup.

    Het bewijs van een restore is niet dat de oude rijen er zijn (die stonden er al), maar
    dat wat erna kwam weg is.
    """
    for database in sorted(await databases_with_prefix()):
        conn = await postgres_live.admin_connection(database)
        try:
            await conn.execute(f"CREATE TABLE IF NOT EXISTS {MARKER_TABLE} (tekst text)")
            await conn.execute(f"INSERT INTO {MARKER_TABLE} (tekst) VALUES ($1)", marker)
        finally:
            await conn.close()


async def marker_row_exists(marker: str) -> bool:
    for database in sorted(await databases_with_prefix()):
        conn = await postgres_live.admin_connection(database)
        try:
            exists = await conn.fetchval("SELECT to_regclass($1)", MARKER_TABLE)
            if not exists:
                continue
            found = await conn.fetchval(f"SELECT count(*) FROM {MARKER_TABLE} WHERE tekst = $1", marker)
            if found:
                return True
        finally:
            await conn.close()
    return False


async def backup_runs(deployment: str) -> list[dict[str, Any]]:
    """De backup-runs van deze deployment, zoals de restore-wizard ze leest.

    Langs ``BackupManager`` en ``group_snapshots_into_runs``, want de restore-taak krijgt
    zijn ``backup_items`` in precies die vorm; een eigen vorm zou een payload opleveren die
    het portaal nooit stuurt.
    """
    manager = BackupManager()
    snapshots = await manager.list_snapshots(CLUSTER, namespace_on_cluster(), project_name=PROJECT)
    runs = group_snapshots_into_runs(snapshots, deployment)
    return sorted(runs.values(), key=lambda run: str(run.get("timestamp", "")), reverse=True)


def backup_destination_has_objects() -> bool:
    """Staat er iets in de backup-bucket van dit project in de wegwerp-MinIO?

    Het eigen kanaal naast kopia: wat de taak meldde zegt niets over wat er werkelijk op de
    bestemming terechtkwam.
    """
    mc = minio_live.admin_mc()
    return any(mc.objects(bucket) for bucket in _backup_buckets(mc))


def _backup_buckets(mc: minio_live.Mc) -> list[str]:
    return [
        str(entry.get("key", "")).strip("/")
        for entry in mc.json_lines("ls", "--json", mc.alias)
        if str(entry.get("key", "")).strip("/").startswith(BACKUP_BUCKET_PREFIX)
    ]


def drop_backup_buckets() -> None:
    """De backup-buckets van dit project weg; ze overleven het verwijderen van het project."""
    mc = minio_live.admin_mc()
    for bucket in _backup_buckets(mc):
        mc.run("rb", "--force", f"{mc.alias}/{bucket}")


async def full_project_yaml() -> str:
    """Het projectbestand dat de wizard neerzet: componenten, diensten en een deployment.

    Langs de productiehelper (``generate_self_service_project_yaml``), want de aanmaaktaak
    krijgt in het portaal precies die tekst mee. Een zelfgeschreven bestand zou meten wat
    ZAD nooit aangeboden krijgt.
    """
    request = SelfServiceProjectRequest(
        project_name=FULL_PROJECT,
        display_name="Taakmeting volledig",
        project_description="Project van de groep tasks_live, uit een volledig bestand",
        cluster=CLUSTER,
        deployment_name=DEPLOYMENT,
        user_email=[ADMIN_EMAIL],
        user_roles=["admin"],
        services=list(SERVICES),
        components=[
            SelfServiceComponent(
                type="single", port=WORKLOAD_PORT, image=WORKLOAD_IMAGE, path="/", services=list(SERVICES)
            )
        ],
    )
    return await generate_self_service_project_yaml(request)


async def await_task_in_queue(task_id: str, *, timeout: float) -> dict[str, Any]:
    """Wacht op de uitkomst van een taak via de taakrij zelf.

    Voor de twee taken die het project zelf AANMAKEN of WEGHALEN: ``/api/tasks/{id}``
    legitimeert zich op de sleutel van het project van de taak, en dat project staat niet in
    de store terwijl de aanmaak loopt en niet meer zodra de verwijdering klaar is. In beide
    gevallen antwoordt die route 401 ("Invalid API key"). Het portaal volgt zo'n taak met het
    token van wie hem startte; deze groep leest hem uit de rij, want hij zette hem daar ook
    zelf in.
    """
    service = AsyncTaskService(CLUSTER)
    deadline = time.monotonic() + timeout
    task: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        task = await service.get_task(task_id)
        assert task is not None, f"taak {task_id} staat niet in de rij"
        if task["status"] in ("completed", "failed", "cancelled"):
            assert task["status"] == "completed", f"taak {task_id} eindigde als {task['status']}: {task.get('result')}"
            return task
        await asyncio.sleep(3.0)
    raise AssertionError(f"taak {task_id} was na {timeout:.0f}s niet klaar: {(task or {}).get('status')}")
