"""De authorization wall gemeten in plaats van beweerd (RC-240).

Per storing staat hier een CONTROLEMETING zonder de vlag, die de storing reproduceert,
naast de meting met de vlag. Een test die alleen de goede kant toetst zou niet weten of de
storing ooit bestond.

De drie storingen, en wat er gemeten is:

F1  Een sessieloze aanvraag om een stylesheet krijgt 403 met ``Content-Type: text/html``
    en de inlogkaart als body. De browser kan dat niet als CSS gebruiken, dus de pagina
    komt zonder opmaak op het scherm. Gereproduceerd: 2315 bytes HTML op ``/bediening.css``,
    exact de 2313 tot 2315 bytes die in de productielogs van ``rig-prd-mpfm-w3h`` staan.

F2  Zonder ``--cookie-refresh`` vernieuwt oauth2-proxy de sessie NOOIT (``refresh:disabled``
    in zijn eigen log), ook niet als het access token al lang verlopen is. De sessie leeft
    dan op het cookie alleen, en op de grens van ``--cookie-expire`` valt hij in een keer
    om: het document krijgt de muur en de subresources een 401. Met een refresh korter dan
    de tokenlevensduur schuift de sessie mee en blijft de pagina heel.

F3  Een gebruiker met veel realmrollen heeft een tokenset die niet in een cookie past,
    waarna oauth2-proxy hem splitst in ``_oauth2_proxy_0``, ``_1``, ... Dat is hier
    bevestigd, maar een gesplitste sessie WERKT: hij is geen verklaring voor de halve
    pagina's. Wat hij wel doet is de sessie breekbaar maken, en dat staat in de laatste
    test. Zie de PR voor de volledige uitkomst van deze poort.

De opstelling (twee containers, geen cluster) staat in ``authwall_harness.py``.
"""

import asyncio
import base64
import os
from dataclasses import dataclass

import httpx
import pytest
import pytest_asyncio
from opi.connectors.keycloak import KeycloakConnector
from tests.integration.authwall_harness import (
    KEYCLOAK_ADMIN,
    KEYCLOAK_ADMIN_PASSWORD,
    KEYCLOAK_INTERN,
    PROXY_PORT,
    bouw_proxy_image,
    login,
    met_vlagwaarde,
    paginalading,
    proxy_args_voor_opstelling,
    proxy_sessiecookies,
    proxy_url,
    sjabloon_sidecar,
    start_proxy,
    stop_proxy,
    zonder_vlag,
    zorg_voor_keycloak,
)
from tests.integration.conftest import is_docker_available

pytestmark = [
    pytest.mark.requires_infra,
    pytest.mark.slow,
    # Er draait altijd precies EEN proxy op een vaste poort, dus deze tests kunnen niet
    # door elkaar heen lopen.
    pytest.mark.serial,
]

WACHTWOORD = "Geheim123!"

#: De paden van een paginalading: het document plus vier subresources. Parallel gevuurd,
#: want dat is wat een browser doet.
PAGINALADING = ["/", "/bediening.css", "/app.js", "/logo.png", "/api/me"]

#: F2 schaalt het drietal (token 5 min, ``--cookie-refresh=3m``, ``--cookie-expire=10h``)
#: terug naar seconden. De ORDENING bepaalt de storing en die blijft gelijk:
#: refresh < tokenlevensduur < cookie-expiry.
F2_TOKEN_S = 10
F2_REFRESH = "5s"
F2_EXPIRE = "15s"
#: Zes rondes van vier seconden komen twee keer voorbij de cookie-grens.
F2_RONDES = 6
F2_INTERVAL_S = 4

#: Genoeg realmrollen om de tokenset over de 4 kB te duwen. Gemeten: met deze 80 rollen
#: splitst oauth2-proxy in twee cookies, met geen rollen in een.
F3_ROLLEN = [f"afdeling-directie-informatievoorziening-medewerker-{i:03d}" for i in range(80)]


@dataclass(frozen=True)
class Wall:
    """Een realm met een client en de args waarmee de proxy daarop draait."""

    realm: str
    client_secret: str
    cookie_secret: str
    args: list[str]


def _cookie_secret() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


@pytest.fixture(scope="session")
def keycloak() -> KeycloakConnector:
    if not is_docker_available():
        pytest.skip("Docker niet beschikbaar; deze tests hebben een echte Keycloak nodig")
    url = zorg_voor_keycloak()
    bouw_proxy_image()
    return KeycloakConnector(keycloak_url=url, admin_username=KEYCLOAK_ADMIN, admin_password=KEYCLOAK_ADMIN_PASSWORD)


async def _bouw_realm(kc: KeycloakConnector, realm: str, extra_realm: dict | None = None) -> Wall:
    """Een realm met een deployment-client, opgebouwd met OPI's eigen connectorcode.

    Niet met een handgeschreven realm-import: zo meet de test wat OPI werkelijk aanmaakt.
    """
    await kc.create_realm(realm_name=realm, display_name=realm)
    instellingen: dict = {
        # De issuer moet voor de proxy en de testclient dezelfde naam zijn; zonder deze
        # ene instelling is hij dat niet. Zie de moduledocstring van de harness.
        "attributes": {"frontendUrl": KEYCLOAK_INTERN},
        # Een vanilla Keycloak-container heeft het platformthema niet.
        "loginTheme": "keycloak",
    }
    instellingen.update(extra_realm or {})
    await kc.update_realm_settings(realm, instellingen)
    client = await kc.create_deployment_client(
        deployment_name="wall",
        project_name="authwall",
        realm_name=realm,
        ingress_hosts=[f"127.0.0.1:{PROXY_PORT}"],
        additional_redirect_uris=[f"http://127.0.0.1:{PROXY_PORT}/*"],
    )
    args = proxy_args_voor_opstelling(
        sjabloon_sidecar()["args"],
        f"{KEYCLOAK_INTERN}/realms/{realm}",
        client["client_id"],
    )
    return Wall(realm=realm, client_secret=client["client_secret"], cookie_secret=_cookie_secret(), args=args)


async def _maak_gebruiker(kc: KeycloakConnector, realm: str, naam: str, rollen: list[str] | None = None) -> None:
    gebruiker = await kc.create_user(
        realm_name=realm,
        username=naam,
        password=WACHTWOORD,
        email=f"{naam}@example.nl",
        # Het sjabloon zet --insecure-oidc-allow-unverified-email=false, dus een account
        # met een onbevestigd adres komt niet door de muur.
        skip_email_verification=True,
    )
    for rol in rollen or []:
        await kc.create_realm_role(realm, rol)
    if rollen:
        await kc.assign_realm_roles_to_user(realm, gebruiker["id"], rollen)


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def wall(keycloak: KeycloakConnector) -> Wall:
    """De realm voor F1 en F3: standaard levensduren, een gebruiker met en zonder rollen."""
    opstelling = await _bouw_realm(keycloak, "authwall-test")
    await _maak_gebruiker(keycloak, opstelling.realm, "weinig")
    await _maak_gebruiker(keycloak, opstelling.realm, "veel", rollen=F3_ROLLEN)
    return opstelling


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def wall_kort(keycloak: KeycloakConnector) -> Wall:
    """De realm voor F2: een access token van tien seconden in plaats van vijf minuten."""
    opstelling = await _bouw_realm(keycloak, "authwall-refresh", {"accessTokenLifespan": F2_TOKEN_S})
    await _maak_gebruiker(keycloak, opstelling.realm, "weinig")
    return opstelling


@pytest.fixture
def zet_wall():
    """Zet er een proxy met deze args, en haal hem na de test weg."""

    def _zet(opstelling: Wall, args: list[str]) -> None:
        start_proxy(args, opstelling.client_secret, opstelling.cookie_secret)

    yield _zet
    stop_proxy()


@pytest.fixture
async def client():
    async with httpx.AsyncClient(follow_redirects=False, timeout=20) as c:
        yield c


def _is_html(antwoord: httpx.Response) -> bool:
    return (antwoord.headers.get("content-type") or "").startswith("text/html")


# --- F1: een stylesheet krijgt HTML terug --------------------------------------------


async def test_f1_stylesheet_krijgt_de_inlogpagina_zonder_api_route(wall: Wall, zet_wall, client) -> None:
    """De controlemeting: zonder --api-route is een stylesheet een HTML-pagina.

    Dit is de storing. ``X-Content-Type-Options: nosniff`` laat de browser deze body niet
    als CSS gebruiken, dus de pagina komt zonder opmaak op het scherm en de consolefouten
    zeggen niets over de oorzaak.
    """
    zet_wall(wall, zonder_vlag(wall.args, "--api-route="))

    css = await client.get(f"{proxy_url()}/bediening.css")
    assert css.status_code == 403
    assert _is_html(css), f"verwacht HTML, kreeg {css.headers.get('content-type')}"
    assert "<!DOCTYPE html>" in css.text
    # Het is de inlogkaart uit het sjabloon, niet een of andere foutpagina.
    assert "beperkte toegang" in css.text
    # Zelfde orde van grootte als de 163 gevallen in de productielogs (2313 tot 2315 bytes).
    assert 2000 < len(css.content) < 3000, f"body van {len(css.content)} bytes"

    # Een navigatie hoort hier NIET van te veranderen: die krijgt de kaart, en moet dat
    # blijven krijgen. Zie het tussenscherm in features/futures/.
    navigatie = await client.get(f"{proxy_url()}/")
    assert navigatie.status_code == 403
    assert _is_html(navigatie)
    assert "beperkte toegang" in navigatie.text


async def test_f1_stylesheet_krijgt_een_401_met_api_route(wall: Wall, zet_wall, client) -> None:
    """Met de vlaggen uit het sjabloon: een 401 zonder HTML, en de kaart blijft op /."""
    zet_wall(wall, wall.args)

    css = await client.get(f"{proxy_url()}/bediening.css")
    assert css.status_code == 401
    assert not _is_html(css), f"stylesheet kreeg nog steeds HTML: {css.headers.get('content-type')}"
    assert len(css.content) < 100, f"body van {len(css.content)} bytes is nog een pagina"

    api = await client.get(f"{proxy_url()}/api/dingen")
    assert api.status_code == 401
    assert not _is_html(api)

    navigatie = await client.get(f"{proxy_url()}/")
    assert navigatie.status_code == 403
    assert _is_html(navigatie)
    assert "beperkte toegang" in navigatie.text


# --- F2: de sessie die niet vernieuwt ------------------------------------------------


async def _lading_per_ronde(client: httpx.AsyncClient) -> list[list[httpx.Response]]:
    """Vuur een paginalading af, elke vier seconden, tot voorbij de cookie-grens."""
    rondes = []
    for ronde in range(F2_RONDES):
        if ronde:
            await asyncio.sleep(F2_INTERVAL_S)
        rondes.append(await paginalading(client, PAGINALADING))
    return rondes


def _f2_args(opstelling: Wall) -> list[str]:
    return met_vlagwaarde(
        met_vlagwaarde(opstelling.args, "--cookie-expire=", F2_EXPIRE), "--cookie-refresh=", F2_REFRESH
    )


async def test_f2_de_sessie_valt_om_zonder_cookie_refresh(wall_kort: Wall, zet_wall, client) -> None:
    """De controlemeting: zonder --cookie-refresh vernieuwt de sessie niet en valt hij om.

    oauth2-proxy logt dan ``refresh:disabled``: hij vernieuwt niet voor de grens, en ook
    niet erna. De sessie leeft op het cookie alleen, en op de grens van ``--cookie-expire``
    is de hele paginalading weg, zonder dat de gebruiker iets deed.
    """
    zet_wall(wall_kort, zonder_vlag(_f2_args(wall_kort), "--cookie-refresh="))
    await login(client, "weinig", WACHTWOORD)

    rondes = await _lading_per_ronde(client)

    assert all(a.status_code == 200 for a in rondes[0]), f"begin was al stuk: {[a.status_code for a in rondes[0]]}"
    laatste = rondes[-1]
    assert all(a.status_code >= 400 for a in laatste), (
        f"de sessie hield het uit zonder --cookie-refresh: {[a.status_code for a in laatste]}"
    )
    # De vorm van de storing: het document krijgt de muur als pagina.
    document = laatste[0]
    assert document.status_code == 403
    assert _is_html(document)


async def test_f2_de_sessie_blijft_met_cookie_refresh(wall_kort: Wall, zet_wall, client) -> None:
    """Met een refresh korter dan de tokenlevensduur schuift de sessie mee.

    Alle aanvragen van elke paginalading slagen, ook voorbij de grens waar de
    controlemeting omvalt, en er is geen nieuwe interactieve aanmelding nodig.
    """
    zet_wall(wall_kort, _f2_args(wall_kort))
    await login(client, "weinig", WACHTWOORD)

    rondes = await _lading_per_ronde(client)

    for nummer, ronde in enumerate(rondes):
        assert all(a.status_code == 200 for a in ronde), f"ronde {nummer}: {[a.status_code for a in ronde]}"
    # Geen enkele aanvraag mag een redirect naar Keycloak zijn geworden: de sessie is
    # vernieuwd, niet opnieuw aangemeld.
    assert not any(a.is_redirect for ronde in rondes for a in ronde)
    # En de vernieuwing is echt gebeurd: oauth2-proxy schrijft het cookie dan opnieuw.
    vernieuwd = sum(len(a.headers.get_list("set-cookie")) for ronde in rondes for a in ronde)
    assert vernieuwd > 0, "geen enkel cookie herschreven, dus er is niets vernieuwd"


# --- F3: het onverklaarde geval ------------------------------------------------------


async def test_f3_de_sessie_splitst_bij_veel_rollen_maar_blijft_werken(wall: Wall, zet_wall, client) -> None:
    """De poort van F3: splitst oauth2-proxy het sessiecookie, en sloopt dat de pagina?

    Gemeten: splitsen gebeurt, al bij 80 realmrollen. Een gesplitste sessie WERKT echter,
    dus de splitsing is geen verklaring voor de halve pagina's. Dat is het resultaat van
    deze poort, en het staat zo in de PR: de oorzaak van F3 blijft onbekend.
    """
    zet_wall(wall, wall.args)

    async with httpx.AsyncClient(follow_redirects=False, timeout=20) as weinig:
        await login(weinig, "weinig", WACHTWOORD)
        kaal = proxy_sessiecookies(weinig)
        omvang = sum(len(weinig.cookies.get(naam) or "") for naam in kaal)
    assert kaal == ["_oauth2_proxy"], f"een gebruiker zonder rollen splitste al: {kaal} ({omvang} bytes)"

    await login(client, "veel", WACHTWOORD)
    gesplitst = proxy_sessiecookies(client)
    assert len(gesplitst) > 1, f"{len(F3_ROLLEN)} realmrollen splitsten het cookie niet: {gesplitst}"
    assert gesplitst[0] == "_oauth2_proxy_0"

    # En toch is de pagina heel: splitsen is niet de storing.
    lading = await paginalading(client, PAGINALADING)
    assert all(a.status_code == 200 for a in lading), [a.status_code for a in lading]


async def test_f3_een_verloren_cookiedeel_sloopt_de_hele_sessie(wall: Wall, zet_wall, client) -> None:
    """Wat een gesplitste sessie wel doet: hij wordt breekbaar.

    Met een cookie valt er niets gedeeltelijk te verliezen. Met twee is elk deel een
    enkelvoudig faalpunt, en wie er een kwijtraakt heeft geen sessie meer. Het beeld dat
    dat oplevert is precies de storing uit de productielogs: de muur op het document en
    een 401 op elke subresource.
    """
    zet_wall(wall, wall.args)
    await login(client, "veel", WACHTWOORD)
    delen = proxy_sessiecookies(client)
    assert len(delen) > 1

    client.cookies.delete(delen[-1])

    lading = await paginalading(client, PAGINALADING)
    assert lading[0].status_code == 403, "het document hield een sessie over aan een half cookie"
    assert _is_html(lading[0])
    for subresource in lading[1:]:
        assert subresource.status_code == 401, [a.status_code for a in lading]
        assert not _is_html(subresource)
