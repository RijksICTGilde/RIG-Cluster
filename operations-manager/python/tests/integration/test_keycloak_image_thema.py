"""Het NLDD-thema van de eigen Keycloak-image, gemeten door de flows echt te doorlopen.

`task test-keycloak-image` bouwt `images/keycloak-zad/Dockerfile`, start hem met de
startargumenten van de basisdeployment en zet de adressen in de omgeving. Zonder
`KEYCLOAK_IMAGE_TEST_URL` slaat deze module zichzelf over.

Waarom de flows echt doorlopen worden en niet per pagina een URL wordt opgevraagd: een
FreeMarker-pagina van Keycloak heeft geen eigen adres. Je komt er alleen door het
voorgaande formulier te versturen, en precies dat is ook wat gemeten moet worden: een
sjabloon dat niet compileert of een veld dat niet post valt pas daar op.

`login-username.ftl` en `login-password.ftl` horen bij een flow die de twee stappen
splitst, en die bouwt geen enkele blauwdruk van OPI. Ze worden hier gemeten op een tweede
realm met zo'n flow, want een pagina die niemand bekijkt is precies de pagina die stil
terugvalt op de parent `base`.

Wat hier NIET gemeten kan worden, en waarom:
- `login-idp-link-confirm` en `login-idp-link-email`: die vragen een echte externe IdP;
- `select-authenticator`: die verschijnt pas bij meer dan een alternatief in de flow;
- `login-page-expired`: die vraagt een verlopen authenticatiesessie.
Die drie staan in features/keycloak-nldd-thema.md als met de hand te bekijken.
"""

from __future__ import annotations

import html
import re
import time
from typing import TYPE_CHECKING

import httpx
import pytest
from opi.utils.totp import TOTP_PERIOD, totp_now
from tests.integration import keycloak_image
from tests.integration.keycloak_image import TIMEOUT, tijdelijke_realm

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.requires_infra

THEMA = "nl-design-system"
REALM = "rc424-thema"
CLIENT = "rc424-probe"

#: Het tweede realm, met een flow die gebruikersnaam en wachtwoord op aparte pagina's zet.
GESPLITST_REALM = "rc424-gesplitst"
GESPLITSTE_FLOW = "rc424-gesplitst-browser"

WACHTWOORD = "Proef-wachtwoord-1234"

#: Wat alleen in ONS thema staat: het wortelelement van de NLDD-schil en het pad van de
#: bundel in de image. Valt een pagina terug op de parent `base`, of geeft Keycloak zijn
#: eigen foutpagina, dan staat er geen van beide in. Het tweede dekt ook de omgekeerde fout:
#: een bundel van een CDN zou een ander pad hebben.
MARKERS = ("<nldd-shell", "/nldd/nldd.min.js")

#: De gebruikers van de proefrealm: naam -> de verplichte acties die hun pagina oproepen.
GEBRUIKERS = {
    "gewoon": [],
    "otp": ["CONFIGURE_TOTP"],
    "wachtwoord": ["UPDATE_PASSWORD"],
    "profiel": ["UPDATE_PROFILE"],
    "mail": ["VERIFY_EMAIL"],
}


#: De velden die elke proefrealm van deze module krijgt.
#: Registratie en wachtwoordherstel staan hier AAN, anders dan in de blauwdrukken van OPI:
#: de pagina's eromheen bestaan in het thema en moeten dus ook gerenderd kunnen worden.
REALM_VELDEN = {
    "loginTheme": THEMA,
    "accountTheme": THEMA,
    "internationalizationEnabled": True,
    "supportedLocales": ["nl", "en"],
    "defaultLocale": "nl",
    "registrationAllowed": True,
    "resetPasswordAllowed": True,
    "rememberMe": True,
    "loginWithEmailAllowed": False,
}


def _redirect(basis_url: str, realm: str = REALM) -> str:
    """De redirect-URI van de proefclient, op Keycloak zelf.

    Deze client volgt redirects, dus een URI naar een dode poort laat httpx op een
    ConnectError vallen zodra een inlog voltooid wordt. Keycloak antwoordt op dit pad met
    een 404, en dat is genoeg: wat gemeten wordt is de URL waar de flow uitkomt.
    """
    return f"{basis_url}/realms/{realm}/rc424-klaar"


def _form_action(pagina: str, form_id: str) -> str:
    """De action-URL van het formulier met dit id, in de volgorde waarin het er staat."""
    tag = re.search(rf"<form[^>]*\bid=\"{re.escape(form_id)}\"[^>]*>", pagina)
    assert tag is not None, f"geen formulier met id {form_id} in de pagina:\n{pagina[:3000]}"
    action = re.search(r'\baction="([^"]+)"', tag.group(0))
    assert action is not None, tag.group(0)
    return html.unescape(action.group(1))


def _is_thema(pagina: str) -> None:
    for marker in MARKERS:
        assert marker in pagina, (marker, pagina[:3000])


@pytest.fixture(scope="module")
def basis_url() -> str:
    return keycloak_image.basis_url()


@pytest.fixture(scope="module")
def admin_token(basis_url: str) -> str:
    return keycloak_image.admin_token(basis_url)


def _maak_proefclient(admin: str, headers: dict[str, str], redirect: str) -> None:
    antwoord = httpx.post(
        f"{admin}/clients",
        headers=headers,
        json={
            "clientId": CLIENT,
            "publicClient": True,
            "standardFlowEnabled": True,
            "redirectUris": [f"{redirect}*"],
            "attributes": {"pkce.code.challenge.method": ""},
        },
        timeout=TIMEOUT,
    )
    assert antwoord.status_code in (201, 204), antwoord.text


def _maak_gebruiker(admin: str, headers: dict[str, str], naam: str, acties: list[str]) -> None:
    antwoord = httpx.post(
        f"{admin}/users",
        headers=headers,
        json={
            "username": naam,
            "enabled": True,
            "email": f"{naam}@toets.invalid",
            "emailVerified": naam != "mail",
            "firstName": "Proef",
            "lastName": naam,
            "requiredActions": acties,
            "credentials": [{"type": "password", "value": WACHTWOORD, "temporary": False}],
        },
        timeout=TIMEOUT,
    )
    assert antwoord.status_code == 201, (naam, antwoord.text)


@pytest.fixture(scope="module")
def realm(basis_url: str, admin_token: str) -> Iterator[str]:
    """Een proefrealm die elke pagina van de inventaris bereikbaar maakt."""
    with tijdelijke_realm(basis_url, admin_token, REALM, **REALM_VELDEN) as headers:
        admin = f"{basis_url}/admin/realms/{REALM}"
        _maak_proefclient(admin, headers, _redirect(basis_url))
        for naam, acties in GEBRUIKERS.items():
            _maak_gebruiker(admin, headers, naam, acties)
        yield REALM


def _stap_verplicht(admin: str, headers: dict[str, str], flow: str, provider: str) -> None:
    """Hangt een authenticator onder een flow en zet hem op REQUIRED.

    Een nieuwe execution komt als DISABLED binnen; de requirement gaat in een tweede
    aanroep mee, met het execution-object dat de flow teruggeeft.
    """
    gemaakt = httpx.post(
        f"{admin}/authentication/flows/{flow}/executions/execution",
        headers=headers,
        json={"provider": provider},
        timeout=TIMEOUT,
    )
    assert gemaakt.status_code in (201, 204), (provider, gemaakt.text)

    executions = httpx.get(f"{admin}/authentication/flows/{flow}/executions", headers=headers, timeout=TIMEOUT)
    assert executions.status_code == 200, executions.text
    stap = next((e for e in executions.json() if e.get("providerId") == provider), None)
    assert stap is not None, (provider, executions.text)

    bijgewerkt = httpx.put(
        f"{admin}/authentication/flows/{flow}/executions",
        headers=headers,
        json={**stap, "requirement": "REQUIRED"},
        timeout=TIMEOUT,
    )
    assert bijgewerkt.status_code in (202, 204), (provider, bijgewerkt.text)


@pytest.fixture(scope="module")
def gesplitst_realm(basis_url: str, admin_token: str) -> Iterator[str]:
    """Een realm met een browser flow die de gebruikersnaam en het wachtwoord splitst.

    Alleen zo'n flow rendert `login-username.ftl` en `login-password.ftl`. Geen blauwdruk
    van OPI bouwt hem, dus hij staat hier: de twee sjablonen bestaan in het thema en mogen
    niet stil terugvallen op de parent `base`.
    """
    with tijdelijke_realm(basis_url, admin_token, GESPLITST_REALM, **REALM_VELDEN) as headers:
        admin = f"{basis_url}/admin/realms/{GESPLITST_REALM}"
        _maak_proefclient(admin, headers, _redirect(basis_url, GESPLITST_REALM))
        _maak_gebruiker(admin, headers, "gewoon", [])

        flow = httpx.post(
            f"{admin}/authentication/flows",
            headers=headers,
            json={
                "alias": GESPLITSTE_FLOW,
                "description": "Gebruikersnaam en wachtwoord op aparte pagina's",
                "providerId": "basic-flow",
                "topLevel": True,
                "builtIn": False,
            },
            timeout=TIMEOUT,
        )
        assert flow.status_code in (201, 204), flow.text

        _stap_verplicht(admin, headers, GESPLITSTE_FLOW, "auth-username-form")
        _stap_verplicht(admin, headers, GESPLITSTE_FLOW, "auth-password-form")

        # De hele representatie terug, met alleen `browserFlow` anders: een PUT met twee
        # velden erin laat de rest staan, maar daar wil deze toets niet van afhangen.
        huidig = httpx.get(f"{basis_url}/admin/realms/{GESPLITST_REALM}", headers=headers, timeout=TIMEOUT)
        assert huidig.status_code == 200, huidig.text
        gebonden = httpx.put(
            f"{basis_url}/admin/realms/{GESPLITST_REALM}",
            headers=headers,
            json={**huidig.json(), "browserFlow": GESPLITSTE_FLOW},
            timeout=TIMEOUT,
        )
        assert gebonden.status_code == 204, gebonden.text

        yield GESPLITST_REALM


def _client() -> httpx.Client:
    # Keycloak 26.8 sets its session cookies `Secure` and renders links relative to the host.
    # A browser treats http://localhost as a secure context and resolves those links. httpx
    # does neither: it keeps Secure cookies in the jar but never sends them over http, so
    # every form post ended in "cookie not found" (400). The hook sends them, as a browser would.
    client = httpx.Client(base_url=keycloak_image.basis_url(), timeout=TIMEOUT, follow_redirects=True)

    def send_secure_cookies(request: httpx.Request) -> None:
        cookies = [f"{c.name}={c.value}" for c in client.cookies.jar if request.url.path.startswith(c.path)]
        if cookies:
            request.headers["Cookie"] = "; ".join(cookies)

    client.event_hooks["request"] = [send_secure_cookies]
    return client


def _inlogpagina(
    http: httpx.Client,
    basis_url: str,
    taal: str = "nl",
    client_id: str = CLIENT,
    realm: str = REALM,
) -> httpx.Response:
    params = {
        "client_id": client_id,
        "response_type": "code",
        "scope": "openid",
        "redirect_uri": _redirect(basis_url, realm),
        "kc_locale": taal,
    }
    return http.get(f"{basis_url}/realms/{realm}/protocol/openid-connect/auth", params=params)


def _log_in(http: httpx.Client, basis_url: str, gebruiker: str, taal: str = "nl") -> httpx.Response:
    """Vult het inlogformulier in en verstuurt het, zoals een browser dat zou doen."""
    pagina = _inlogpagina(http, basis_url, taal=taal)
    assert pagina.status_code == 200, pagina.text[:2000]
    _is_thema(pagina.text)
    return http.post(
        _form_action(pagina.text, "kc-form-login"),
        data={"username": gebruiker, "password": WACHTWOORD, "credentialId": ""},
    )


class TestElkePaginaRendertInHetThema:
    """De inventaris uit features/keycloak-nldd-thema.md, pagina voor pagina.

    Geen van deze pagina's mag terugvallen op de opmaak van de parent `base`: dat is precies
    wat het vorige thema deed voor alles buiten login, register en error.
    """

    @pytest.mark.parametrize("taal", ["nl", "en"])
    def test_de_inlogpagina(self, basis_url: str, realm: str, taal: str) -> None:
        with _client() as http:
            pagina = _inlogpagina(http, basis_url, taal=taal)
            assert pagina.status_code == 200, pagina.text[:2000]
            _is_thema(pagina.text)
            assert 'name="username"' in pagina.text
            assert 'name="password"' in pagina.text
            assert "login-actions/authenticate" in pagina.text

    def test_de_inlogpagina_met_een_afgekeurd_wachtwoord(self, basis_url: str, realm: str) -> None:
        """De foutweg van het thema: `invalid` op het veld en de melding in de lijst."""
        with _client() as http:
            pagina = _inlogpagina(http, basis_url)
            antwoord = http.post(
                _form_action(pagina.text, "kc-form-login"),
                data={"username": "gewoon", "password": "fout-wachtwoord", "credentialId": ""},
            )
            assert antwoord.status_code == 200, antwoord.text[:2000]
            _is_thema(antwoord.text)
            assert "nldd-validation-item" in antwoord.text, antwoord.text[:3000]
            assert 'unmet="error-username"' in antwoord.text, antwoord.text[:3000]

    def test_de_registratiepagina(self, basis_url: str, realm: str) -> None:
        with _client() as http:
            pagina = _inlogpagina(http, basis_url)
            # De link naar registreren staat in de melding onderaan de inlogpagina.
            url = re.search(r'href="([^"]*registration[^"]*)"', pagina.text)
            assert url is not None, pagina.text[:3000]
            registratie = http.get(html.unescape(url.group(1)))
            assert registratie.status_code == 200, registratie.text[:2000]
            _is_thema(registratie.text)
            assert 'id="kc-register-form"' in registratie.text

    def test_de_pagina_voor_een_vergeten_wachtwoord_en_de_melding_erna(self, basis_url: str, realm: str) -> None:
        """After the reset form, Keycloak 26.8 returns to the login page with a success message, not `info.ftl`."""
        with _client() as http:
            pagina = _inlogpagina(http, basis_url)
            url = re.search(r'href="([^"]*reset-credentials[^"]*)"', pagina.text)
            assert url is not None, pagina.text[:3000]
            herstel = http.get(html.unescape(url.group(1)))
            assert herstel.status_code == 200, herstel.text[:2000]
            _is_thema(herstel.text)

            terug = http.post(_form_action(herstel.text, "kc-reset-password-form"), data={"username": "gewoon"})
            assert terug.status_code == 200, terug.text[:2000]
            _is_thema(terug.text)
            assert 'data-page-id="login-login"' in terug.text, terug.text[:3000]
            assert '<nldd-banner variant="success">' in terug.text, terug.text[:3000]

    def test_de_pagina_die_een_authenticator_instelt(self, basis_url: str, realm: str) -> None:
        with _client() as http:
            pagina = _log_in(http, basis_url, "otp")
            assert pagina.status_code == 200, pagina.text[:2000]
            _is_thema(pagina.text)
            assert 'id="kc-totp-settings-form"' in pagina.text, pagina.text[:3000]
            assert 'name="totpSecret"' in pagina.text

    def test_de_pagina_die_een_wachtwoord_laat_wijzigen(self, basis_url: str, realm: str) -> None:
        with _client() as http:
            pagina = _log_in(http, basis_url, "wachtwoord")
            assert pagina.status_code == 200, pagina.text[:2000]
            _is_thema(pagina.text)
            assert 'id="kc-passwd-update-form"' in pagina.text, pagina.text[:3000]
            assert 'name="password-new"' in pagina.text
            assert 'name="password-confirm"' in pagina.text

    def test_de_pagina_die_het_profiel_laat_bijwerken(self, basis_url: str, realm: str) -> None:
        with _client() as http:
            pagina = _log_in(http, basis_url, "profiel")
            assert pagina.status_code == 200, pagina.text[:2000]
            _is_thema(pagina.text)
            assert 'id="kc-update-profile-form"' in pagina.text, pagina.text[:3000]
            assert 'name="firstName"' in pagina.text

    def test_de_pagina_die_om_een_mailbevestiging_vraagt(self, basis_url: str, realm: str) -> None:
        with _client() as http:
            pagina = _log_in(http, basis_url, "mail")
            assert pagina.status_code == 200, pagina.text[:2000]
            _is_thema(pagina.text)
            assert "mail@toets.invalid" in pagina.text, pagina.text[:3000]

    def test_de_foutpagina(self, basis_url: str, realm: str) -> None:
        """Een onbekende client: Keycloak rendert `error.ftl` in het thema van het realm."""
        with _client() as http:
            pagina = _inlogpagina(http, basis_url, client_id="bestaat-niet")
            assert pagina.status_code >= 400, pagina.status_code
            _is_thema(pagina.text)

    def test_de_pagina_die_het_uitloggen_laat_bevestigen(self, basis_url: str, realm: str) -> None:
        with _client() as http:
            klaar = _log_in(http, basis_url, "gewoon")
            assert "code=" in str(klaar.url), klaar.url

            bevestiging = http.get(f"{basis_url}/realms/{REALM}/protocol/openid-connect/logout")
            assert bevestiging.status_code == 200, bevestiging.text[:2000]
            _is_thema(bevestiging.text)
            assert 'id="kc-logout-confirm"' in bevestiging.text, bevestiging.text[:3000]


class TestDeGesplitsteInlogflow:
    """`login-username.ftl` en `login-password.ftl`, op het realm met de gesplitste flow.

    Geen blauwdruk van OPI splitst de twee stappen, dus dit is de enige plek waar deze twee
    sjablonen gerenderd worden. Zonder deze toets zouden ze stil kunnen terugvallen op de
    parent `base` zonder dat iemand het ziet.
    """

    def test_de_pagina_die_alleen_om_de_gebruikersnaam_vraagt(self, basis_url: str, gesplitst_realm: str) -> None:
        with _client() as http:
            pagina = _inlogpagina(http, basis_url, realm=gesplitst_realm)
            assert pagina.status_code == 200, pagina.text[:2000]
            _is_thema(pagina.text)
            assert 'name="username"' in pagina.text, pagina.text[:3000]
            assert 'name="password"' not in pagina.text, pagina.text[:3000]

    def test_de_pagina_die_daarna_alleen_om_het_wachtwoord_vraagt(self, basis_url: str, gesplitst_realm: str) -> None:
        with _client() as http:
            gebruikersnaam = _inlogpagina(http, basis_url, realm=gesplitst_realm)
            wachtwoord = http.post(
                _form_action(gebruikersnaam.text, "kc-form-login"),
                data={"username": "gewoon"},
            )
            assert wachtwoord.status_code == 200, wachtwoord.text[:2000]
            _is_thema(wachtwoord.text)
            assert 'name="password"' in wachtwoord.text, wachtwoord.text[:3000]

            klaar = http.post(
                _form_action(wachtwoord.text, "kc-form-login"),
                data={"password": WACHTWOORD},
            )
            assert str(klaar.url).startswith(_redirect(basis_url, gesplitst_realm)), klaar.url
            assert "code=" in str(klaar.url), klaar.url


class TestDeFormulierenPostenEcht:
    """Een NLDD-invoerveld is een form-associated custom element: zijn waarde komt pas in de
    POST als de bundel is geladen. Deze toetsen doen de POST zelf, dus wat ze meten is dat de
    VELDNAMEN kloppen die Keycloak verwacht. Dat de componenten ze in een browser ook echt
    meesturen meet de Playwright-ronde van `scripts/keycloak_thema_shots.py`."""

    def test_inloggen_met_gebruikersnaam_en_wachtwoord_komt_bij_de_redirect_uri_uit(
        self, basis_url: str, realm: str
    ) -> None:
        with _client() as http:
            klaar = _log_in(http, basis_url, "gewoon")
            assert str(klaar.url).startswith(_redirect(basis_url)), klaar.url
            assert "code=" in str(klaar.url), klaar.url

    def test_inloggen_met_een_otp_komt_bij_de_redirect_uri_uit(self, basis_url: str, realm: str) -> None:
        """Instellen en daarna gebruiken, allebei via het formulier.

        De gebruiker `otp` heeft CONFIGURE_TOTP, dus de eerste inlog levert het geheim en de
        tweede gebruikt het. Zo is de OTP-weg in zijn geheel gemeten en niet alleen de
        pagina die hem instelt.
        """
        with _client() as http:
            instellen = _log_in(http, basis_url, "otp")
            geheim = re.search(r'name="totpSecret" value="([^"]+)"', instellen.text)
            assert geheim is not None, instellen.text[:3000]

            klaar = http.post(
                _form_action(instellen.text, "kc-totp-settings-form"),
                data={
                    "totp": totp_now(geheim.group(1))[0],
                    "totpSecret": geheim.group(1),
                    "userLabel": "proef-app",
                },
            )
            assert str(klaar.url).startswith(_redirect(basis_url)), klaar.text[:3000]

        with _client() as http:
            otp_pagina = _log_in(http, basis_url, "otp")
            assert 'id="kc-otp-login-form"' in otp_pagina.text, otp_pagina.text[:3000]
            _is_thema(otp_pagina.text)

            # Keycloak refuses a code that was already used, and setup used the current one.
            # The next period's code falls inside Keycloak's look-around window.
            klaar = http.post(
                _form_action(otp_pagina.text, "kc-otp-login-form"),
                data={"otp": totp_now(geheim.group(1), at=time.time() + TOTP_PERIOD)[0]},
            )
            assert str(klaar.url).startswith(_redirect(basis_url)), klaar.text[:3000]
            assert "code=" in str(klaar.url), klaar.url


class TestDePaginaHaaltNietsBuitenDeEigenHost:
    """De inlogpagina is de plek waar een derde partij het minst mag meekijken.

    Een CDN valt niet op zolang de browser er wel bij kan; op een afgesloten cluster is het
    een pagina zonder opmaak, zonder lettertype en zonder werkende invoervelden.
    """

    def test_elke_verwijzing_in_de_inlogpagina_blijft_op_de_eigen_host(self, basis_url: str, realm: str) -> None:
        with _client() as http:
            pagina = _inlogpagina(http, basis_url)
            verwijzingen = re.findall(r'(?:src|href)="(https?://[^"]+)"', pagina.text)
            vreemd = [u for u in verwijzingen if not u.startswith(basis_url)]
            assert vreemd == [], vreemd

    def test_de_stijlbladen_en_de_bundel_halen_zelf_ook_niets_op(self, basis_url: str, realm: str) -> None:
        """Een @import of een url() naar een andere host zou pas in de browser blijken."""
        with _client() as http:
            pagina = _inlogpagina(http, basis_url)
            # Keycloak 26.8 renders resource URLs relative to the host; the client resolves them.
            bronnen = [
                str(http.base_url.join(bron)) for bron in re.findall(r'(?:src|href)="([^"]+\.(?:css|js))"', pagina.text)
            ]
            assert bronnen, pagina.text[:3000]

            gezien: set[str] = set()
            te_doen = list(bronnen)
            while te_doen:
                url = te_doen.pop()
                if url in gezien:
                    continue
                gezien.add(url)
                bestand = http.get(url)
                assert bestand.status_code == 200, (url, bestand.status_code)
                for geimporteerd in re.findall(r'@import\s+(?:url\()?["\']([^"\']+)["\']', bestand.text):
                    assert not geimporteerd.startswith(("http://", "https://")), (url, geimporteerd)
                    te_doen.append(str(httpx.URL(url).join(geimporteerd)))
                for verwezen in re.findall(r"url\(\s*['\"]?(https?://[^'\")]+)", bestand.text):
                    raise AssertionError(f"{url} verwijst naar {verwezen}")

            # De bundel en de volledige stijlketen zijn langsgekomen, niet alleen global.css.
            assert any(u.endswith("nldd.min.js") for u in gezien), sorted(gezien)
            assert any(u.endswith("variables.css") for u in gezien), sorted(gezien)
