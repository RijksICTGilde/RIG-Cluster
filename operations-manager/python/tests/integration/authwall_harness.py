"""Een echte oauth2-proxy voor een echte Keycloak, zodat de authorization wall te METEN is.

De opstelling, twee containers en geen cluster:

- ``zad-test-keycloak``: dezelfde Keycloak-versie als productie, in dev-mode. De realm
  wordt opgebouwd met OPI's eigen connectorcode, niet met een handgeschreven import, zodat
  de test meet wat OPI werkelijk aanmaakt.
- ``zad-test-authwall-proxy``: hetzelfde oauth2-proxy-image als in het sjabloon, met de
  args UIT het sjabloon en ``--upstream=static://200``.

Drie dingen die uren kosten als je ze niet weet:

1. De proxy praat met Keycloak binnen het docker-netwerk, de testclient praat met Keycloak
   via een gepubliceerde poort op de host. Een container kan hier NIET bij een
   gepubliceerde poort op de host ("Host is unreachable" op de bridge-gateway), dus die
   twee wegen hebben verschillende hostnamen. De issuer moet voor beide hetzelfde zijn,
   anders mint Keycloak tokens met de ene naam terwijl de proxy de andere verwacht en
   krijg je ``id token issued by a different provider``. Daarom staat ``frontendUrl`` als
   realm-attribuut op de INTERNE naam en schrijft de testclient die naam in elke redirect
   om naar de host (:func:`naar_host`).
2. Het sjabloon zet ``--cookie-secure=true``, en de opstelling draait op http. Een cookie
   met ``Secure`` wordt dan niet bewaard en niet teruggestuurd, dus elke sessie zou leeg
   zijn. :func:`proxy_args_voor_opstelling` zet die ene vlag om, en alleen die.
3. ``--custom-templates-dir`` wijst naar een map die in Kubernetes uit een ConfigMap komt.
   Hier bakken we die ConfigMap in een image bovenop het proxy-image, want een bind mount
   van de host werkt niet: de Docker-daemon ziet het bestandssysteem van deze container
   niet. Zo krijgt F1 de ECHTE inlogkaart terug en niet die van oauth2-proxy zelf.
"""

import asyncio
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx
from opi.generation.manifests import render_template
from ruamel.yaml import YAML
from tests.docker_runtime import DockerError, container_state, docker


class AuthwallHarnessError(DockerError):
    """De opstelling kon niet opgebouwd worden. Luid falen, nooit stil."""


#: Vaste namen, want daar draait dit ontwerp om: wat een mens kan noemen, kan hij opruimen.
KEYCLOAK_CONTAINER = "zad-test-keycloak"
PROXY_CONTAINER = "zad-test-authwall-proxy"
PROXY_IMAGE = "zad-test-authwall-proxy:local"
NETWORK = "zad-test-authwall"

KEYCLOAK_IMAGE = "quay.io/keycloak/keycloak:25.0.6"
KEYCLOAK_ADMIN = "admin"
KEYCLOAK_ADMIN_PASSWORD = "adminadmin"

#: Op de host. Alleen van belang bij het AANMAKEN; staat de container er al, dan is zijn
#: werkelijke poort leidend.
KEYCLOAK_PORT = os.environ.get("ZAD_TEST_KEYCLOAK_PORT", "58080")
#: Vast, want hij staat als redirect-URI in de Keycloak-client en er draait er maar een.
PROXY_PORT = os.environ.get("ZAD_TEST_AUTHWALL_PROXY_PORT", "58081")

#: De naam waarmee de PROXY Keycloak bereikt, en dus ook de issuer in elk token.
KEYCLOAK_INTERN = f"http://{KEYCLOAK_CONTAINER}:8080"

_HOST_PATROON = re.compile(r"\baction=\"([^\"]+)\"")


def keycloak_host_url() -> str:
    """De URL waarop de testclient en de OPI-connector Keycloak bereiken."""
    return f"http://127.0.0.1:{_keycloak_host_poort()}"


def proxy_url() -> str:
    return f"http://127.0.0.1:{PROXY_PORT}"


def naar_host(url: str) -> str:
    """Schrijf de interne Keycloak-naam om naar de weg die de host heeft.

    De issuer is bewust de interne naam (zie de moduledocstring), dus elke redirect en elk
    formulier dat Keycloak afgeeft wijst naar een naam die alleen in het docker-netwerk
    bestaat.
    """
    return url.replace(KEYCLOAK_INTERN, keycloak_host_url())


# --- de containers -------------------------------------------------------------------


def _keycloak_host_poort() -> str:
    klaar = docker("port", KEYCLOAK_CONTAINER, "8080/tcp", fout=AuthwallHarnessError)
    return klaar.stdout.strip().splitlines()[0].rsplit(":", 1)[1]


def _zorg_voor_netwerk() -> None:
    # Bestaat hij al, dan is "already exists" het antwoord en geen fout.
    docker("network", "create", NETWORK, check=False, fout=AuthwallHarnessError)


def _wacht_tot_keycloak_antwoordt(seconden: int = 180) -> None:
    basis = keycloak_host_url()
    grens = time.monotonic() + seconden
    while time.monotonic() < grens:
        try:
            antwoord = httpx.get(f"{basis}/realms/master/.well-known/openid-configuration", timeout=3)
        except httpx.HTTPError:
            antwoord = None
        if antwoord is not None and antwoord.status_code == 200:
            return
        time.sleep(1)
    logboek = docker("logs", "--tail", "30", KEYCLOAK_CONTAINER, check=False, fout=AuthwallHarnessError).stdout
    raise AuthwallHarnessError(f"'{KEYCLOAK_CONTAINER}' werd niet klaar binnen {seconden}s. Laatste regels:\n{logboek}")


def zorg_voor_keycloak() -> str:
    """Start de vaste Keycloak als hij er niet is, en geef zijn host-URL terug.

    Draait hij al, dan blijft hij draaien: hergebruik is het punt. Draait hij op een ander
    image, dan gaat hij eraf en komt hij terug, anders meet een volgende versie stilzwijgend
    tegen de oude.
    """
    _zorg_voor_netwerk()
    draait, image = container_state(KEYCLOAK_CONTAINER, fout=AuthwallHarnessError)
    if image and (not draait or image != KEYCLOAK_IMAGE):
        docker("rm", "-f", KEYCLOAK_CONTAINER, fout=AuthwallHarnessError)
        draait = False

    if not draait:
        gemaakt = docker(
            "run",
            "-d",
            "--name",
            KEYCLOAK_CONTAINER,
            "--network",
            NETWORK,
            "-p",
            f"{KEYCLOAK_PORT}:8080",
            "-e",
            f"KEYCLOAK_ADMIN={KEYCLOAK_ADMIN}",
            "-e",
            f"KEYCLOAK_ADMIN_PASSWORD={KEYCLOAK_ADMIN_PASSWORD}",
            KEYCLOAK_IMAGE,
            "start-dev",
            check=False,
            timeout=180,
            fout=AuthwallHarnessError,
        )
        # Twee suites die tegelijk beginnen doen allebei dit commando; een van de twee
        # krijgt "name already in use". Dat is het antwoord, niet een fout.
        if gemaakt.returncode != 0 and not container_state(KEYCLOAK_CONTAINER, fout=AuthwallHarnessError)[0]:
            raise AuthwallHarnessError(
                f"'{KEYCLOAK_CONTAINER}' kon niet starten: {gemaakt.stderr.strip()}\n"
                f"Zit poort {KEYCLOAK_PORT} bezet, zet dan ZAD_TEST_KEYCLOAK_PORT."
            )

    _wacht_tot_keycloak_antwoordt()
    return keycloak_host_url()


def _render_sjabloon_sectie(sectie: str, **extra: Any) -> str:
    aw = {
        "issuer_url": "https://keycloak.example.com/realms/placeholder",
        "client_id": "placeholder",
        "keycloak_secret_name": "placeholder-keycloak",
        "cookie_secret_name": "placeholder-cookie",
    }
    context: dict[str, Any] = {
        "section": sectie,
        "name": "wall",
        "namespace": "rig-test",
        "project": {"name": "test"},
        "application_port": 8080,
        "hostname": f"127.0.0.1:{PROXY_PORT}",
        "authorization_wall": aw,
    }
    context.update(extra)
    return render_template("sidecar-authorization-wall.yaml.jinja", context)


def sjabloon_sidecar() -> dict[str, Any]:
    """De sidecar zoals OPI hem genereert: image, args en al.

    Hier, en niet in de test, staat de enige plek die het sjabloon leest. Een test die de
    vlaggen opnieuw opschrijft toetst zijn eigen kopie, en dan lopen sjabloon en test uit
    elkaar zonder dat iets rood wordt.
    """
    doc = YAML().load("containers:\n" + _render_sjabloon_sectie("container"))
    containers = doc["containers"]
    if len(containers) != 1:
        raise AuthwallHarnessError(f"sjabloon gaf {len(containers)} containers in plaats van 1")
    return containers[0]


def sjabloon_templates() -> dict[str, str]:
    """De inlogkaart en de foutpagina uit de ConfigMap-sectie van hetzelfde sjabloon."""
    cm = YAML().load(_render_sjabloon_sectie("configmap"))
    return {naam: str(inhoud) for naam, inhoud in cm["data"].items()}


def proxy_args_voor_opstelling(args: list[str], issuer: str, client_id: str) -> list[str]:
    """De args uit het sjabloon, met alleen wat de opstelling anders MOET hebben.

    Vier vlaggen wijzen naar een cluster dat hier niet staat (issuer, client, upstream,
    redirect-url) en ``--cookie-secure`` moet uit (zie de moduledocstring). Alles verder
    blijft letterlijk wat het sjabloon zegt.
    """
    omzettingen = {
        "--oidc-issuer-url=": f"--oidc-issuer-url={issuer}",
        "--client-id=": f"--client-id={client_id}",
        "--upstream=": "--upstream=static://200",
        "--redirect-url=": f"--redirect-url={proxy_url()}/oauth2/callback",
        "--cookie-secure=": "--cookie-secure=false",
    }
    uit: list[str] = []
    gezien: set[str] = set()
    for arg in args:
        for prefix, nieuw in omzettingen.items():
            if arg.startswith(prefix):
                uit.append(nieuw)
                gezien.add(prefix)
                break
        else:
            uit.append(arg)
    ontbreekt = sorted(set(omzettingen) - gezien)
    if ontbreekt:
        raise AuthwallHarnessError(f"sjabloon zet deze vlaggen niet meer: {ontbreekt}")
    return uit


def zonder_vlag(args: list[str], prefix: str) -> list[str]:
    """De args zonder een vlag, voor de controlemeting zonder de fix.

    Faalt als de vlag er niet in zit: een controlemeting die niets weghaalt meet niets.
    """
    overblijvend = [a for a in args if not a.startswith(prefix)]
    if len(overblijvend) == len(args):
        raise AuthwallHarnessError(f"'{prefix}' staat niet in de args, dus er valt niets weg te halen")
    return overblijvend


def met_vlagwaarde(args: list[str], prefix: str, waarde: str) -> list[str]:
    """Dezelfde args met een andere waarde op een bestaande vlag."""
    if not any(a.startswith(prefix) for a in args):
        raise AuthwallHarnessError(f"'{prefix}' staat niet in de args")
    return [f"{prefix}{waarde}" if a.startswith(prefix) else a for a in args]


def bouw_proxy_image() -> None:
    """Het proxy-image met de sjabloon-templates erin gebakken.

    Bakken in plaats van mounten, want een build streamt zijn context vanaf hier en een
    bind mount niet (zie de moduledocstring).
    """
    sidecar = sjabloon_sidecar()
    basis = sidecar["image"]
    templates = sjabloon_templates()
    with tempfile.TemporaryDirectory() as map_:
        pad = Path(map_)
        for naam, inhoud in templates.items():
            (pad / naam).write_text(inhoud, encoding="utf-8")
        regels = [f"FROM {basis}", f"COPY {' '.join(sorted(templates))} /etc/oauth2-proxy/templates/"]
        (pad / "Dockerfile").write_text("\n".join(regels) + "\n", encoding="utf-8")
        docker("build", "-t", PROXY_IMAGE, str(pad), timeout=300, fout=AuthwallHarnessError)


def _wacht_tot_proxy_antwoordt(seconden: int = 60) -> None:
    grens = time.monotonic() + seconden
    while time.monotonic() < grens:
        try:
            antwoord = httpx.get(f"{proxy_url()}/ping", timeout=2)
        except httpx.HTTPError:
            antwoord = None
        if antwoord is not None and antwoord.status_code == 200:
            return
        time.sleep(0.3)
    logboek = docker("logs", "--tail", "40", PROXY_CONTAINER, check=False, fout=AuthwallHarnessError).stdout
    logboek += docker("logs", "--tail", "40", PROXY_CONTAINER, check=False, fout=AuthwallHarnessError).stderr
    raise AuthwallHarnessError(f"de proxy werd niet klaar binnen {seconden}s. Laatste regels:\n{logboek}")


def start_proxy(args: list[str], client_secret: str, cookie_secret: str) -> None:
    """Zet er een proxy met deze args. Er draait er altijd precies een."""
    stop_proxy()
    docker(
        "run",
        "-d",
        "--name",
        PROXY_CONTAINER,
        "--network",
        NETWORK,
        "-p",
        f"{PROXY_PORT}:4180",
        "-e",
        f"OAUTH2_PROXY_CLIENT_SECRET={client_secret}",
        "-e",
        f"OAUTH2_PROXY_COOKIE_SECRET={cookie_secret}",
        PROXY_IMAGE,
        *args,
        timeout=120,
        fout=AuthwallHarnessError,
    )
    _wacht_tot_proxy_antwoordt()


def stop_proxy() -> None:
    docker("rm", "-f", PROXY_CONTAINER, check=False, fout=AuthwallHarnessError)


def proxy_logboek() -> str:
    klaar = docker("logs", PROXY_CONTAINER, check=False, fout=AuthwallHarnessError)
    return klaar.stdout + klaar.stderr


# --- de inlogflow --------------------------------------------------------------------


async def login(client: httpx.AsyncClient, gebruiker: str, wachtwoord: str, pad: str = "/") -> httpx.Response:
    """Doorloop de echte OAuth-flow en laat de client met een sessie achter.

    Handmatig redirects volgen, want elke hop die van Keycloak komt wijst naar de interne
    naam (zie :func:`naar_host`) en de statuscodes onderweg zijn zelf de meting.
    """
    antwoord = await client.get(f"{proxy_url()}/oauth2/start", params={"rd": pad})
    for _ in range(12):
        if antwoord.is_redirect:
            doel = naar_host(urljoin(str(antwoord.request.url), antwoord.headers["location"]))
            antwoord = await client.get(doel)
            continue
        actie = _inlogformulier_actie(antwoord.text)
        if actie is None:
            return antwoord
        antwoord = await client.post(
            naar_host(urljoin(str(antwoord.request.url), actie)),
            data={"username": gebruiker, "password": wachtwoord, "credentialId": ""},
        )
    raise AuthwallHarnessError("de inlogflow bleef rondlopen zonder ergens uit te komen")


def _inlogformulier_actie(html: str) -> str | None:
    """Het action-attribuut van Keycloak's inlogformulier, of None als dit geen formulier is."""
    for actie in _HOST_PATROON.findall(html):
        if "login-actions/authenticate" in actie:
            return actie.replace("&amp;", "&")
    return None


def proxy_sessiecookies(client: httpx.AsyncClient) -> list[str]:
    """De namen van de sessiecookies die de proxy zette, gesorteerd.

    ``_oauth2_proxy_0``, ``_1``, ... betekent dat de hele tokenset niet in een cookie paste
    en oauth2-proxy hem heeft gesplitst. Dat is de meting van F3.
    """
    namen = [naam for naam in client.cookies if naam.startswith("_oauth2_proxy")]
    return sorted(n for n in namen if not n.endswith("_csrf"))


async def paginalading(client: httpx.AsyncClient, paden: list[str]) -> list[httpx.Response]:
    """Vuur de aanvragen van een paginalading PARALLEL af, zoals een browser doet."""
    taken = [client.get(f"{proxy_url()}{pad}") for pad in paden]
    return list(await asyncio.gather(*taken))
