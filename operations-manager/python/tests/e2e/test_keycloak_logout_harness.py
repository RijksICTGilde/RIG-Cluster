"""Harnas voor de uitlogketen: een echte Keycloak 25.0.6 met de shim, drie wegwerprealms.

Dit harnas bestaat omdat het drie verkeerde oplossingen heeft tegengehouden. Het staat
daarom in de repo in plaats van in een scratchpad, achter de marker ``keycloak_harness``
zodat het niet in de gewone run en niet in de browsersuite meedraait::

    uv run pytest tests/e2e/test_keycloak_logout_harness.py -m keycloak_harness -q

Wat het nabouwt, een-op-een met productie::

    app   (speelt docs)  --OIDC--\\
                                  >--  plat (speelt rig-platform)  --OIDC-->  up (speelt BZK)
    app2  (speelt wies)  --OIDC--/

Op ``plat`` wijst de IdP zijn logout naar de ``always-clear-session-logout``-shim, met een
``downstreamLogoutUrl`` die doodloopt op de stub: precies zoals productie naar BZK. De
stub registreert welke URL's de browser aanroept, en dat is de meting.

Nodig: docker (de Keycloak-image), maven (de shim-JAR) en chromium voor playwright. Werkt
zowel op Linux als op Docker Desktop, waar de docker-host een VM is en niet de machine zelf.
"""

from __future__ import annotations

import base64
import faulthandler
import hashlib
import socket
import subprocess
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import httpx
import pytest
from playwright.sync_api import Page, sync_playwright

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = [pytest.mark.keycloak_harness, pytest.mark.requires_infra, pytest.mark.slow]

KEYCLOAK_IMAGE = "quay.io/keycloak/keycloak:25.0.6"
CONTAINER = "kc-logout-harness"
ADMIN = "admin"
SHIM_SOURCE = Path(__file__).resolve().parents[4] / "keycloak-migration" / "custom-mapper"
SHIM_JAR = SHIM_SOURCE / "target" / "keycloak-saml-nameid-mapper-1.1.0.jar"
BROKER_SECRET = "harness-secret"
#: Veiligheidsnet voor de browser, geen wachtmechanisme: het harnas wacht eerst zelf op de
#: voorwaarde (zie ``_wacht_tot_de_inlogpagina_staat``). Playwrights standaard van 30s was
#: hier te krap, zie de meting in die docstring.
PAGINA_BUDGET_MS = 120_000
#: Een eigen gebruiker per realm, zodat een sessie in app niets zegt over een sessie in up.
LOKALE_GEBRUIKER = "alice"
KETEN_GEBRUIKER = "bob"
#: De publieke deployment-client eist PKCE, net als die van OPI. Zonder ``code_challenge``
#: weigert Keycloak het autorisatieverzoek met een redirect NAAR de callback, dus zonder dit
#: paar krijgt de browser wel zijn callback maar is er geen clientsessie.
PKCE_VERIFIER = "harnas-verifier-voor-de-tweede-clientsessie-op-de-publieke-client"
PKCE_CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(PKCE_VERIFIER.encode()).digest()).decode().rstrip("=")


def _vrije_poort() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _heeft(commando: str) -> bool:
    return subprocess.run(["which", commando], capture_output=True, check=False).returncode == 0


# ---------------------------------------------------------------------------
# De stub die de meting doet
# ---------------------------------------------------------------------------


class _Stub:
    """Webserver die onthoudt welke paden binnenkwamen; dat is wat we meten."""

    def __init__(self, poort: int) -> None:
        self.poort = poort
        self.bezocht: list[tuple[str, str]] = []
        self._slot = threading.Lock()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def _registreer(self) -> None:
                with stub._slot:
                    stub.bezocht.append((self.command, self.path.split("?")[0]))

            def do_GET(self) -> None:
                self._registreer()
                body = b"<html><body>stub</body></html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                self._registreer()
                lengte = int(self.headers.get("Content-Length") or 0)
                if lengte:
                    self.rfile.read(lengte)
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args: object) -> None:
                pass

        # Alleen de browser belt de stub: front-channel is een iframe en de shim stuurt een
        # redirect. De Keycloak-container hoeft er niet bij, dus loopback volstaat.
        self._server = ThreadingHTTPServer(("127.0.0.1", poort), Handler)

    @property
    def url(self) -> str:
        return f"http://localhost:{self.poort}"

    def start(self) -> None:
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self._server.shutdown()

    def paden(self) -> list[str]:
        with self._slot:
            return [pad for _, pad in self.bezocht]


# ---------------------------------------------------------------------------
# De Keycloak eromheen
# ---------------------------------------------------------------------------


class _Harnas:
    def __init__(self, keycloak_url: str, stub: _Stub) -> None:
        self.kc = keycloak_url
        self.stub = stub

    def _token(self) -> str:
        antwoord = httpx.post(
            f"{self.kc}/realms/master/protocol/openid-connect/token",
            data={"client_id": "admin-cli", "username": ADMIN, "password": ADMIN, "grant_type": "password"},
            timeout=30,
        )
        antwoord.raise_for_status()
        return antwoord.json()["access_token"]

    def client(self) -> httpx.Client:
        return httpx.Client(
            base_url=f"{self.kc}/admin/realms", headers={"Authorization": f"Bearer {self._token()}"}, timeout=30
        )

    def sessie_ids(self, realm: str) -> list[str]:
        with self.client() as c:
            return [
                sessie["id"]
                for gebruiker in c.get(f"/{realm}/users").json()
                for sessie in c.get(f"/{realm}/users/{gebruiker['id']}/sessions").json()
            ]

    def sessies(self, realm: str) -> int:
        return len(self.sessie_ids(realm))

    @staticmethod
    def nu() -> str:
        """Stempel om ``log(sinds=...)`` mee af te bakenen."""
        return datetime.now(UTC).isoformat()

    def log(self, sinds: str | None = None) -> str:
        """Het containerlog, desgewenst alleen vanaf ``sinds``.

        Het venster is nodig zodra je de AFWEZIGHEID van een regel toetst: de container
        leeft een hele module, dus een eerdere toets heeft ``logout_failed`` er al in staan.
        """
        argumenten = ["docker", "logs"]
        if sinds is not None:
            argumenten += ["--since", sinds]
        klaar = subprocess.run([*argumenten, CONTAINER], capture_output=True, text=True, check=True)
        return klaar.stderr + klaar.stdout


@pytest.fixture(scope="module")
def harnas() -> Iterator[_Harnas]:
    for commando in ("docker", "mvn"):
        if not _heeft(commando):
            pytest.skip(f"{commando} is niet beschikbaar")

    if not SHIM_JAR.exists():
        subprocess.run(["mvn", "-B", "-q", "clean", "package", "-DskipTests"], cwd=SHIM_SOURCE, check=True, timeout=900)
    assert SHIM_JAR.exists(), f"de shim-JAR is niet gebouwd: {SHIM_JAR}"

    # Het bouwen en opstarten duurt langer dan de globale ``faulthandler_timeout`` van 60s;
    # die dumpt dan elke stack en dat leest als een storing.
    faulthandler.cancel_dump_traceback_later()

    kc_poort = _vrije_poort()
    stub = _Stub(_vrije_poort())
    stub.start()

    subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True, check=False)
    # De JAR gaat er met `docker cp` in: een bind-mount kan niet, want de docker-daemon
    # leeft buiten deze container en ziet dit pad niet (hij maakt er dan een MAP van).
    subprocess.run(
        [
            "docker",
            "create",
            "--name",
            CONTAINER,
            # De poort wordt gepubliceerd in plaats van `--network host`: op Docker Desktop is
            # die host de Linux-VM en bereikt localhost de container nooit. Binnen en buiten
            # hetzelfde nummer, want Keycloak belt zijn eigen backchannel-URL over localhost.
            "-p",
            f"127.0.0.1:{kc_poort}:{kc_poort}",
            "-e",
            f"KEYCLOAK_ADMIN={ADMIN}",
            "-e",
            f"KEYCLOAK_ADMIN_PASSWORD={ADMIN}",
            "-e",
            f"KC_HTTP_PORT={kc_poort}",
            "-e",
            "KC_HTTP_ENABLED=true",
            "-e",
            "KC_HOSTNAME_STRICT=false",
            "-e",
            "KC_LOG_LEVEL=info,org.keycloak.services:debug,nl.minbzk.rig:debug",
            KEYCLOAK_IMAGE,
            "start-dev",
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )
    subprocess.run(["docker", "cp", str(SHIM_JAR), f"{CONTAINER}:/opt/keycloak/providers/shim.jar"], check=True)
    subprocess.run(["docker", "start", CONTAINER], check=True, capture_output=True)

    keycloak_url = f"http://localhost:{kc_poort}"
    try:
        _wacht_tot_keycloak_antwoordt(keycloak_url)
        yield _Harnas(keycloak_url, stub)
    finally:
        stub.stop()
        subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True, check=False)


def _wacht_tot_keycloak_antwoordt(keycloak_url: str, seconden: int = 600) -> None:
    """Wacht op de voorwaarde, niet op de klok: het discovery-document is het signaal.

    Het budget is een veiligheidsnet en niet de maat: een koude start met een eigen
    provider-JAR moet eerst opnieuw augmenteren, en dat duurde gemeten 129s tot het
    discovery-document antwoordde (de JVM meldde zelf 82,7s). Met 180s viel het harnas
    daarom om zodra er iets anders op de machine liep.
    """
    grens = time.monotonic() + seconden
    while time.monotonic() < grens:
        draait = subprocess.run(
            ["docker", "ps", "--filter", f"name={CONTAINER}", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if CONTAINER not in draait.stdout:
            logs = subprocess.run(["docker", "logs", "--tail", "30", CONTAINER], capture_output=True, text=True)
            raise RuntimeError(f"de harnas-Keycloak is gestopt:\n{logs.stderr}{logs.stdout}")
        try:
            if (
                httpx.get(f"{keycloak_url}/realms/master/.well-known/openid-configuration", timeout=5).status_code
                == 200
            ):
                return
        except httpx.HTTPError:
            pass
        time.sleep(2)
    raise TimeoutError(f"de harnas-Keycloak antwoordde niet binnen {seconden}s")


# ---------------------------------------------------------------------------
# De drie wegwerprealms
# ---------------------------------------------------------------------------

#: De drie standen van ``frontchannel.logout.url`` op de applicatieclient. "gezet" is wat
#: deze taak levert; "leeg" is wat OPI op een client ZONDER ``logout-path`` schrijft, en
#: "afwezig" is de stand van de productieclients van voor deze taak.
FrontchannelStand = Literal["gezet", "leeg", "afwezig"]


def _bouw_realms(
    harnas: _Harnas,
    *,
    stand: FrontchannelStand,
    frontchannel_vlag: bool = True,
    publieke_vlag: bool | None = None,
) -> None:
    """Bouw app/app2/plat/up opnieuw op. Idempotent: elk realm wordt eerst weggegooid.

    Elk applicatierealm krijgt de TWEE clients die OPI per deployment uitrolt:
    ``<realm>-client`` (``create_deployment_client``) en ``<realm>-client-public``
    (``_ensure_public_client``, onvoorwaardelijk aangeroepen). OPI zet de schakelaar
    "Front channel logout" op beide aan; uit is hier geen alternatieve configuratie maar de
    tegenproef. ``publieke_vlag`` volgt ``frontchannel_vlag`` tenzij je hem apart zet, en
    dat is de meting van de toets die de twee clients scheidt.
    """
    if publieke_vlag is None:
        publieke_vlag = frontchannel_vlag
    kc, stub = harnas.kc, harnas.stub.url

    with harnas.client() as c:

        def post(pad: str, body: dict) -> None:
            antwoord = c.post(pad, json=body)
            if antwoord.status_code not in (201, 204, 409):
                raise RuntimeError(f"POST {pad} -> {antwoord.status_code} {antwoord.text[:300]}")

        def realm(naam: str) -> None:
            c.delete(f"/{naam}")
            post(
                "",
                {
                    "realm": naam,
                    "enabled": True,
                    "sslRequired": "none",
                    "eventsEnabled": True,
                    "eventsListeners": ["jboss-logging"],
                },
            )

        def gebruiker(realm_naam: str, naam: str) -> None:
            post(
                f"/{realm_naam}/users",
                {
                    "username": naam,
                    "enabled": True,
                    "email": f"{naam}@example.com",
                    "emailVerified": True,
                    "firstName": naam.capitalize(),
                    "lastName": "Tester",
                    "credentials": [{"type": "password", "value": naam, "temporary": False}],
                },
            )

        def broker_client(boven: str, client_id: str, onder: str, idp_alias: str) -> None:
            """De evenknie van ``platformClients`` uit de blauwdruk, backchannel-URL incluis."""
            post(
                f"/{boven}/clients",
                {
                    "clientId": client_id,
                    "enabled": True,
                    "protocol": "openid-connect",
                    "publicClient": False,
                    "secret": BROKER_SECRET,
                    "redirectUris": [f"{kc}/realms/{onder}/broker/{idp_alias}/endpoint/*"],
                    "webOrigins": ["+"],
                    "standardFlowEnabled": True,
                    "attributes": {
                        "backchannel.logout.url": (
                            f"{kc}/realms/{onder}/protocol/openid-connect/logout/backchannel-logout"
                        ),
                        "backchannel.logout.session.required": "true",
                        "post.logout.redirect.uris": "+",
                    },
                },
            )

        def oidc_idp(
            realm_naam: str, alias: str, boven: str, client_id: str, logout_url: str, extra: dict | None = None
        ) -> None:
            config = {
                "clientId": client_id,
                "clientSecret": BROKER_SECRET,
                "clientAuthMethod": "client_secret_post",
                "authorizationUrl": f"{kc}/realms/{boven}/protocol/openid-connect/auth",
                "tokenUrl": f"{kc}/realms/{boven}/protocol/openid-connect/token",
                "userInfoUrl": f"{kc}/realms/{boven}/protocol/openid-connect/userinfo",
                "jwksUrl": f"{kc}/realms/{boven}/protocol/openid-connect/certs",
                "issuer": f"{kc}/realms/{boven}",
                "useJwksUrl": "true",
                "logoutUrl": logout_url,
                "backchannelSupported": "false",
                "syncMode": "FORCE",
                "defaultScope": "openid profile email",
                "updateProfileFirstLogin": "off",
            }
            config.update(extra or {})
            post(
                f"/{realm_naam}/identity-provider/instances",
                {
                    "alias": alias,
                    "providerId": "oidc",
                    "enabled": True,
                    "trustEmail": True,
                    "firstBrokerLoginFlowAlias": "first broker login",
                    "config": config,
                },
            )

        def applicatie_client(realm_naam: str) -> None:
            attributen = {"post.logout.redirect.uris": "+"}
            if stand == "gezet":
                attributen["frontchannel.logout.url"] = f"{stub}/{realm_naam}/logout"
            elif stand == "leeg":
                attributen["frontchannel.logout.url"] = ""
            post(
                f"/{realm_naam}/clients",
                {
                    "clientId": f"{realm_naam}-client",
                    "enabled": True,
                    "protocol": "openid-connect",
                    "publicClient": True,
                    "redirectUris": [f"{stub}/*"],
                    "webOrigins": ["+"],
                    "standardFlowEnabled": True,
                    "frontchannelLogout": frontchannel_vlag,
                    "attributes": attributen,
                },
            )
            # De tweede client van de deployment. OPI zet er geen frontchannel.logout.url op,
            # alleen de vlag, dus dat is hier ook de vorm.
            post(
                f"/{realm_naam}/clients",
                {
                    "clientId": f"{realm_naam}-client-public",
                    "enabled": True,
                    "protocol": "openid-connect",
                    "publicClient": True,
                    "redirectUris": [f"{stub}/*"],
                    "webOrigins": ["+"],
                    "standardFlowEnabled": True,
                    "frontchannelLogout": publieke_vlag,
                    "attributes": {
                        "pkce.code.challenge.method": "S256",
                        "post.logout.redirect.uris": "+",
                    },
                },
            )

        realm("up")
        gebruiker("up", KETEN_GEBRUIKER)
        broker_client("up", "plat-broker", "plat", "up-oidc")

        realm("plat")
        oidc_idp(
            "plat",
            "up-oidc",
            "up",
            "plat-broker",
            logout_url=f"{kc}/realms/plat/always-clear-session-logout/up-oidc",
            extra={"downstreamLogoutUrl": f"{stub}/bzk-logout"},
        )
        broker_client("plat", "app-broker", "app", "plat-oidc")
        broker_client("plat", "app2-broker", "app2", "plat-oidc")

        for naam, broker in (("app", "app-broker"), ("app2", "app2-broker")):
            realm(naam)
            gebruiker(naam, LOKALE_GEBRUIKER)
            oidc_idp(naam, "plat-oidc", "plat", broker, logout_url=f"{kc}/realms/plat/protocol/openid-connect/logout")
            applicatie_client(naam)


# ---------------------------------------------------------------------------
# De browserstappen
# ---------------------------------------------------------------------------


def _autorisatie_url(harnas: _Harnas, realm: str, client: str | None = None) -> str:
    return (
        f"{harnas.kc}/realms/{realm}/protocol/openid-connect/auth"
        f"?client_id={client or f'{realm}-client'}&response_type=code&scope=openid"
        f"&redirect_uri={harnas.stub.url}/{realm}/callback&state=s&nonce=n"
    )


def _wacht_tot_de_inlogpagina_staat(harnas: _Harnas, realm: str, seconden: int = 180) -> str:
    """Vraag de inlogpagina eerst zelf op; de browser krijgt pas een URL die antwoordt.

    Een vers aangemaakt realm rendert zijn inlogpagina de eerste keer langzaam: gemeten op
    Keycloak 25.0.6 in dit harnas duurde die eerste render 18,2s en de tweede 0,1s. Samen
    met het starten van chromium viel de eerste toets daarmee om in een ``Page.goto``
    timeout van 30s. Dit wacht op de voorwaarde, zodat dat budget niet meer de maat is.
    """
    url = _autorisatie_url(harnas, realm)
    grens = time.monotonic() + seconden
    while True:
        try:
            if httpx.get(url, timeout=30).status_code == 200:
                return url
        except httpx.HTTPError:
            pass
        if time.monotonic() >= grens:
            raise TimeoutError(f"de inlogpagina van realm '{realm}' antwoordde niet binnen {seconden}s")
        time.sleep(1)


def _log_lokaal_in(page: Page, harnas: _Harnas, realm: str) -> None:
    """Inloggen op het realm zelf, zonder de federatie: de IdP-knop staat ernaast."""
    page.goto(_wacht_tot_de_inlogpagina_staat(harnas, realm))
    page.fill("#username", LOKALE_GEBRUIKER)
    page.fill("#password", LOKALE_GEBRUIKER)
    page.click("#kc-login")
    page.wait_for_url(f"{harnas.stub.url}/{realm}/callback**")


def _log_via_de_keten_in(page: Page, harnas: _Harnas, realm: str) -> None:
    """De volle keten: de IdP-knop heeft voorrang boven het lokale inlogformulier."""
    page.goto(_wacht_tot_de_inlogpagina_staat(harnas, realm))
    page.click("text=plat-oidc")
    page.wait_for_load_state()
    if page.query_selector("text=up-oidc"):
        page.click("text=up-oidc")
        page.wait_for_load_state()
    if page.query_selector("#username"):
        page.fill("#username", KETEN_GEBRUIKER)
        page.fill("#password", KETEN_GEBRUIKER)
        page.click("#kc-login")
    page.wait_for_url(f"{harnas.stub.url}/{realm}/callback**")


def _tweede_clientsessie(page: Page, harnas: _Harnas, realm: str, client: str) -> None:
    """Een tweede clientsessie op een realm waar de gebruiker al in zit: stil SSO.

    Nodig omdat de vlag niet per CLIENT weegt maar per clientsessie: ``backchannelLogoutAll``
    en ``checkUserSessionOnlyHasLoggedOutClients`` lopen beide over
    ``userSession.getAuthenticatedClientSessions()``.
    """
    page.goto(
        f"{_autorisatie_url(harnas, realm, client=client)}&code_challenge={PKCE_CHALLENGE}&code_challenge_method=S256"
    )
    page.wait_for_url(f"{harnas.stub.url}/{realm}/callback**")
    # De weigering van een autorisatieverzoek komt ook op de callback terecht, en zou hier
    # een stille meting zonder clientsessie opleveren.
    assert "error=" not in page.url, f"het autorisatieverzoek werd geweigerd: {page.url}"


def _log_uit(page: Page, harnas: _Harnas, realm: str) -> None:
    """Uitloggen via het end-session endpoint, inclusief de bevestigingspagina.

    Zonder ``id_token_hint`` toont Keycloak eerst "wil je uitloggen?"; wie daar niet op
    klikt meet een uitlogketen die nooit begon. Dat kostte een ronde.
    """
    page.goto(
        f"{harnas.kc}/realms/{realm}/protocol/openid-connect/logout"
        f"?client_id={realm}-client&post_logout_redirect_uri={harnas.stub.url}/{realm}/na-logout"
    )
    page.wait_for_load_state()
    for _ in range(3):
        knop = page.query_selector("#kc-logout") or page.query_selector(
            "form[action*='logout'] input[type=submit], form[action*='logout'] button[type=submit]"
        )
        if knop is None:
            break
        knop.click()
        page.wait_for_load_state()
    page.wait_for_load_state("networkidle")


@pytest.fixture
def page(harnas: _Harnas) -> Iterator[Page]:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        browser_page = browser.new_page()
        browser_page.set_default_timeout(PAGINA_BUDGET_MS)
        browser_page.set_default_navigation_timeout(PAGINA_BUDGET_MS)
        yield browser_page
        browser.close()


# ---------------------------------------------------------------------------
# De metingen
# ---------------------------------------------------------------------------


def test_keycloak_roept_de_frontchannel_logout_url_aan(harnas: _Harnas, page: Page) -> None:
    """Succescriterium 4: het attribuut werkt, Keycloak belt het pad bij uitloggen.

    Gemeten zonder de federatie, want daar draait ``finishBrowserLogout`` wel.
    """
    _bouw_realms(harnas, stand="gezet")
    _log_lokaal_in(page, harnas, "app")
    assert harnas.sessies("app") == 1

    harnas.stub.bezocht.clear()
    _log_uit(page, harnas, "app")

    assert "/app/logout" in harnas.stub.paden()
    assert harnas.sessies("app") == 0


@pytest.mark.parametrize("stand", ["afwezig", "leeg"])
def test_zonder_een_pad_wordt_er_niemand_gebeld(harnas: _Harnas, page: Page, stand: FrontchannelStand) -> None:
    """De rode meting ervoor, in beide standen die geen pad betekenen.

    "afwezig" is de stand van de productieclients van voor deze taak. "leeg" is wat OPI
    sinds deze taak schrijft op een client zonder ``logout-path``: de sleutel staat er
    altijd, want anders ziet de 409-tak het weghalen van het veld niet. Dat leunt op de
    aanname dat Keycloak een lege waarde hetzelfde behandelt als geen waarde, en dat is
    hier de meting: het aanmaken van de client wordt geaccepteerd en er wordt niemand
    gebeld.
    """
    _bouw_realms(harnas, stand=stand)
    _log_lokaal_in(page, harnas, "app")

    harnas.stub.bezocht.clear()
    _log_uit(page, harnas, "app")

    assert "/app/logout" not in harnas.stub.paden()
    # De sessie gaat wel weg; het is alleen de applicatie die het nooit hoort.
    assert harnas.sessies("app") == 0


def test_in_de_shim_keten_hoort_geen_van_de_applicaties_het(harnas: _Harnas, page: Page) -> None:
    """De stand van vandaag, geen gewenst gedrag. De oorzaak staat in de vijf toetsen hierna.

    Twee dingen gaan mis en het is EEN oorzaak: de ``frontchannelLogout``-vlag op de
    applicatieclient. De shim stuurt een backchannel-POST naar beide applicatierealms, en
    daar slaat ``backchannelLogoutAll`` elke front-channel-client over. De controle erna,
    ``checkUserSessionOnlyHasLoggedOutClients``, rekent juist die overgeslagen client als
    "niet uitgelogd", het endpoint antwoordt 501 en ``KeycloakErrorHandler`` zet de
    transactie op rollback-only. Daarmee wordt het verwijderen van de sessie teruggedraaid.
    Dat ``app2`` net zo faalt als ``app`` is geen ordening: een backchannel-POST heeft geen
    browserketen.

    Volgorde van de regels hieronder: eerst de paden, dan de sessies, dan het log.
    """
    _bouw_realms(harnas, stand="gezet")
    _log_via_de_keten_in(page, harnas, "app")
    _log_via_de_keten_in(page, harnas, "app2")
    assert (harnas.sessies("app"), harnas.sessies("app2"), harnas.sessies("plat"), harnas.sessies("up")) == (1, 1, 1, 1)

    harnas.stub.bezocht.clear()
    _log_uit(page, harnas, "app")

    paden = harnas.stub.paden()
    assert "/bzk-logout" in paden, f"de browser hoorde op de doodlopende BZK-pagina te eindigen, kreeg {paden}"
    assert "/app/logout" not in paden, f"de fan-out vuurde onverwacht wel: {paden}"
    assert "/app2/logout" not in paden, f"de fan-out vuurde onverwacht wel: {paden}"

    # plat gaat weg (de shim doet zijn werk), up blijft (daarvoor is forceAuthn de
    # compensatie), en de twee applicatierealms houden hun sessie.
    assert (harnas.sessies("app"), harnas.sessies("app2"), harnas.sessies("plat"), harnas.sessies("up")) == (1, 1, 0, 1)

    # De faalhandtekening, woordelijk: geslaagde logouts logt Keycloak op DEBUG, dus
    # stilte in het log bewijst niets. Deze regels wel.
    log = harnas.log()
    assert 'error="logout_failed"' in log
    for realm in ("app", "app2"):
        assert (
            f'realmName="{realm}", clientId="null", userId="null", ipAddress="127.0.0.1", error="logout_failed"' in log
        )


def test_de_frontchannel_vlag_is_de_oorzaak_van_de_mislukte_notificatie(harnas: _Harnas, page: Page) -> None:
    """De tegenproef op de toets hierboven: met de vlag uit sterven alle drie de sessies.

    Niets anders wijzigt, het pad staat nog op de client. Dit pint dat de oorzaak de vlag
    is en niet de ordening van de keten: ook ``app2``, buiten de browserketen, gaat mee.

    De vlag staat uit op BEIDE clients die OPI per deployment uitrolt, en de gebruiker heeft
    op beide een clientsessie: dat is de vorm die de rechterkolom van de vork belooft. Wat
    een enkele overgebleven vlag doet staat in de toets hierna.
    """
    _bouw_realms(harnas, stand="gezet", frontchannel_vlag=False)
    _log_via_de_keten_in(page, harnas, "app")
    _log_via_de_keten_in(page, harnas, "app2")
    for realm in ("app", "app2"):
        _tweede_clientsessie(page, harnas, realm, f"{realm}-client-public")
    assert (harnas.sessies("app"), harnas.sessies("app2"), harnas.sessies("plat"), harnas.sessies("up")) == (1, 1, 1, 1)

    harnas.stub.bezocht.clear()
    sinds = harnas.nu()
    _log_uit(page, harnas, "app")

    assert "/bzk-logout" in harnas.stub.paden()
    assert (harnas.sessies("app"), harnas.sessies("app2"), harnas.sessies("plat"), harnas.sessies("up")) == (0, 0, 0, 1)

    # Het venster is essentieel: de container leeft de hele module, dus zonder ``sinds``
    # staat de faalregel van de toets hierboven er al in.
    log = harnas.log(sinds=sinds)
    # Eerst dat het venster gevuld IS: een afwezigheidstoets op een leeg venster slaagt altijd.
    assert "Always-clear logout for user" in log, f"het logvenster bevatte deze uitlog niet: {log[-2000:]}"
    assert 'error="logout_failed"' not in log, f"de notificatie faalde alsnog: {log[-2000:]}"


def test_de_vlag_moet_uit_op_elke_client_waar_een_sessie_op_staat(harnas: _Harnas, page: Page) -> None:
    """De vlag weegt per clientSESSIE, en OPI zet hem op twee clients per deployment.

    ``create_deployment_client`` zet hem op ``<client>`` en ``_ensure_public_client``
    onvoorwaardelijk op ``<client>-public``. Beide Keycloak-lussen
    (``backchannelLogoutAll``, ``checkUserSessionOnlyHasLoggedOutClients``) lopen over
    ``userSession.getAuthenticatedClientSessions()``, dus een client met de vlag aan telt pas
    mee als de gebruiker er een sessie op heeft. Twee rondes meten die twee kanten, met de
    vlag uit op ``app-client`` en aan op ``app-client-public``.
    """
    _bouw_realms(harnas, stand="gezet", frontchannel_vlag=False, publieke_vlag=True)

    # Ronde 1: de vlag staat aan, maar op een client zonder clientsessie.
    _log_via_de_keten_in(page, harnas, "app")
    assert harnas.sessies("app") == 1
    sinds = harnas.nu()
    _log_uit(page, harnas, "app")

    log = harnas.log(sinds=sinds)
    assert "Always-clear logout for user" in log, f"het logvenster bevatte deze uitlog niet: {log[-2000:]}"
    assert harnas.sessies("app") == 0, "een client met de vlag aan maar zonder sessie hield de sessie tegen"
    assert 'error="logout_failed"' not in log, f"de notificatie faalde alsnog: {log[-2000:]}"

    # Ronde 2: dezelfde opstelling, nu met een clientsessie op de client met de vlag erbij.
    _log_via_de_keten_in(page, harnas, "app")
    _tweede_clientsessie(page, harnas, "app", "app-client-public")
    assert harnas.sessies("app") == 1
    sinds = harnas.nu()
    _log_uit(page, harnas, "app")

    assert harnas.sessies("app") == 1, "een enkele overgebleven vlag liet de sessie toch weggaan"
    log = harnas.log(sinds=sinds)
    assert 'error="logout_failed"' in log, f"de 501 bleef weg, herzie de conclusie: {log[-2000:]}"


def test_de_frontchannel_vlag_is_ook_de_voorwaarde_voor_het_pad(harnas: _Harnas, page: Page) -> None:
    """En dit is wat die vlag uitzetten kost, zodat niemand het als gratis reparatie leest.

    Keycloak gebruikt ``frontchannel.logout.url`` alleen voor clients waarvan de vlag aan
    staat (``browserLogoutAllClients`` splitst de clientsessies op precies die vlag). Met
    de vlag uit wordt het pad dus ook BUITEN de federatie niet meer gebeld, en dat is de
    groene meting van RC-245.
    """
    _bouw_realms(harnas, stand="gezet", frontchannel_vlag=False)
    _log_lokaal_in(page, harnas, "app")
    assert harnas.sessies("app") == 1

    harnas.stub.bezocht.clear()
    _log_uit(page, harnas, "app")

    assert "/app/logout" not in harnas.stub.paden(), "het pad werd gebeld zonder de vlag"
    assert harnas.sessies("app") == 0


def test_de_afgebroken_terugreis_is_na_de_shim_niet_te_hervatten(harnas: _Harnas, page: Page) -> None:
    """De eerste kandidaat voor een fan-out, gemeten: hervatten lukt niet meer.

    Wat de shim weghaalt is een GET naar ``broker/<alias>/endpoint/logout_response?state=
    <sessie-id>``; dat is het punt waar het applicatierealm ``finishBrowserLogout`` zou
    draaien en zijn iframes zou renderen. Na de shim weigert dat endpoint: het eist
    ``state == LOGGING_OUT``, en de rollback van de 501 heeft die stand niet overgelaten.
    """
    _bouw_realms(harnas, stand="gezet")
    _log_via_de_keten_in(page, harnas, "app")
    _log_uit(page, harnas, "app")

    sessie_ids = harnas.sessie_ids("app")
    assert sessie_ids, "app hield geen sessie over, dan valt er niets te hervatten"

    harnas.stub.bezocht.clear()
    sinds = harnas.nu()
    page.goto(f"{harnas.kc}/realms/app/broker/plat-oidc/endpoint/logout_response?state={sessie_ids[0]}")
    page.wait_for_load_state("networkidle")

    assert "/app/logout" not in harnas.stub.paden(), "hervatten werkte wel, herzie de conclusie"
    assert harnas.sessies("app") == 1

    # De weigering zelf, uit ``OIDCIdentityProvider.logoutResponse``, zodat dit niet slaagt
    # op een verzoek dat het endpoint nooit bereikte.
    log = harnas.log(sinds=sinds)
    weigeringen = [r for r in ("usersession in different state", "no valid user session") if r in log]
    assert weigeringen, f"het endpoint weigerde niet, maar belde ook niemand: {log[-2000:]}"


def test_een_realm_binnengaan_met_initiating_idp_vraagt_een_klik(harnas: _Harnas, page: Page) -> None:
    """De tweede kandidaat, gemeten: hij werkt, maar niet zonder gebruiker.

    ``initiating_idp`` laat het realm de brokerstap overslaan en meteen
    ``finishBrowserLogout`` draaien, dus het pad WORDT gebeld. Alleen staat er eerst een
    bevestigingsscherm: zonder ``id_token_hint`` blijft ``confirmationNeeded`` waar
    (``LogoutEndpoint`` 190-294), en een id_token voor een applicatierealm kan ``plat`` niet
    maken. In een iframe klikt niemand, dus een onzichtbare fan-out is dit niet.
    """
    _bouw_realms(harnas, stand="gezet")
    _log_via_de_keten_in(page, harnas, "app")

    harnas.stub.bezocht.clear()
    page.goto(
        f"{harnas.kc}/realms/app/protocol/openid-connect/logout"
        f"?client_id=app-client&post_logout_redirect_uri={harnas.stub.url}/app/na-logout"
        f"&initiating_idp=plat-oidc"
    )
    page.wait_for_load_state("networkidle")

    assert harnas.stub.paden() == [], f"er was geen klik nodig: {harnas.stub.paden()}"
    assert page.query_selector("#kc-logout") is not None, "het bevestigingsscherm bleef weg"
    assert harnas.sessies("app") == 1

    page.click("#kc-logout")
    page.wait_for_load_state("networkidle")
    assert "/app/logout" in harnas.stub.paden(), f"na de klik bleef de fan-out weg: {harnas.stub.paden()}"
    assert harnas.sessies("app") == 0
