"""De TLS-override per deployment-component, van buiten gemeten (RC-96).

RC-78 bouwde de override en toetste hem op het model, de haak en het gerenderde
sjabloon (``tests/test_deployment_certificate_override.py``). RC-89 liep er op de
sandbox een pad doorheen: ``passthrough`` op een tweede deployment, gemeten aan de
annotatie op het ingress-object. Wat in beide ontbrak is het bewijs dat een browser
werkelijk een ander certificaat krijgt, en dat is bij een certificaat het enige dat
telt: wat in het projectbestand staat is de bedoeling, niet de uitkomst.

Deze module loopt het vermogen af zoals het plan het stelt, en meet het certificaat
**op de verbinding** met een echte TLS-handshake per hostnaam:

1. leeg laten verandert niets -- beide deployments krijgen het platformcertificaat;
2. een eigen certificaat op de ene deployment en het platformcertificaat op de andere;
3. ``provided`` uitzetten met een override, op een draaiende deployment;
4. ``provided`` zonder bijlage wordt geweigerd, met een melding die zegt wat ontbreekt;
5. de bijlage is projectbreed: een override telt mee in de verwijdercontrole;
6. de UI-weg en de API-weg;
7. herverwerken levert dezelfde ingress met datzelfde certificaat op.

Het platformcertificaat van de sandbox is een echt Let's Encrypt-wildcard voor
``*.sandbox.rijksapp.dev``; een zelfondertekend certificaat ernaast is daardoor op de
verbinding te onderscheiden, en dat is wat punt 2 meetbaar maakt.

Slaat over zonder E2E_BASE_URL. Draaien:

    E2E_BASE_URL=https://zad.sandbox.rijksapp.dev \
    E2E_SECRET_KEY=sandbox-dev-secret-key-fixed-for-stable-sessions-32min \
    FORGEJO_URL=https://forgejo.sandbox.rijksapp.dev \
    FORGEJO_USER=rig-admin FORGEJO_PASSWORD=admin1234 FORGEJO_VERIFY_SSL=false \
    uv run pytest tests/e2e/test_sandbox_tls_override.py -m "e2e and sandbox" -q -o addopts=""
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from opi.core.project_schema import validate_project_schema
from opi.handlers.project_file_handler import validate_attachment_references
from tests.e2e.helpers import sandbox_api
from tests.e2e.helpers.edit_modal import EditModalHelper
from tests.e2e.helpers.lifecycle import RUNNABLE_IMAGE, create_project_with_services

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from playwright.sync_api import BrowserContext, Page
    from tests.e2e.helpers.forgejo import ForgejoClient

pytestmark = [pytest.mark.e2e, pytest.mark.sandbox]

_API_VERIFY_SSL = os.environ.get("E2E_API_VERIFY_SSL", "false").lower() in ("1", "true", "yes")
_USER_EMAIL = os.environ.get("E2E_SANDBOX_USER", "admin@sandbox.rijksapp.dev")

#: Waar de TLS-handshake naartoe gaat. Het hele wildcard wijst naar 127.0.0.1 (de sandbox
#: is een lokaal Kind-cluster), dus de hostnaam gaat als SNI mee en niet als adres.
#:
#: De POORT is hier geen detail. Op de gedeelde dev-server staat Caddy op 443 en Kind op
#: 8843; Caddy termineert TLS zelf met hetzelfde wildcard-certificaat, dus een meting op
#: 443 levert voor ELKE hostnaam het platformcertificaat op -- ook voor een deployment die
#: zijn eigen certificaat aanbiedt. Dat is precies de meting die 'de bedoeling' voor 'de
#: uitkomst' aanziet. Daarom worden de Kind-poorten eerst geprobeerd, en pas daarna 443
#: (een lokale sandbox zonder Caddy staat daar wel zelf op). Met E2E_TLS_ENDPOINT te
#: overrulen.
_TLS_KANDIDATEN = ["127.0.0.1:8843", "127.0.0.1:8443", "127.0.0.1:443"]

#: De tweede deployment. Twee zijn er nodig: een override die op beide zou landen is met
#: een is niet van een projectbrede instelling te onderscheiden.
STAGING = "staging"

#: De naam waaronder het eigen certificaat in de projectcatalogus komt te staan.
BIJLAGE_ID = "doorloop-cert"

#: Het onderwerp van het zelfondertekende certificaat. Uniek genoeg om het op de
#: verbinding zonder twijfel van het platformcertificaat te onderscheiden.
EIGEN_CN = "rc96-doorloop-eigen-certificaat"

#: Waaraan het platformcertificaat te herkennen is (echt Let's Encrypt-wildcard).
PLATFORM_UITGEVER = "Let's Encrypt"

COMPONENT = "web"


# --- meten -----------------------------------------------------------------------------


def _kubectl_json(*args: str) -> dict:
    """Vraag het cluster zelf naar een object; {} als kubectl er niet is of het er niet is."""
    try:
        result = subprocess.run(
            ["kubectl", *args, "-o", "json"], capture_output=True, text=True, timeout=60, check=False
        )
    except OSError, subprocess.SubprocessError:
        return {}
    if result.returncode != 0:
        return {}
    with contextlib.suppress(json.JSONDecodeError):
        return json.loads(result.stdout or "{}")
    return {}


def _ingress(project_name: str, naam: str) -> dict:
    return _kubectl_json("get", "ingress", naam, "-n", f"rig-{project_name}")


def _ingress_hostnaam(project_name: str, naam: str) -> str:
    regels = (_ingress(project_name, naam).get("spec") or {}).get("rules") or []
    return (regels[0] or {}).get("host", "") if regels else ""


def _ingress_tls_secret(project_name: str, naam: str) -> str:
    tls = (_ingress(project_name, naam).get("spec") or {}).get("tls") or []
    return (tls[0] or {}).get("secretName", "") if tls else ""


def _secret_certificaat(project_name: str, naam: str) -> dict[str, str]:
    """Het certificaat zoals het als kubernetes.io/tls-secret in de namespace staat, of {}.

    De schakel tussen 'het ingress wijst ernaar' en 'de verbinding levert het': een ingress
    dat naar een secret wijst dat er niet is, valt stil terug op het standaardcertificaat
    van de ingress-controller -- en dat ziet er van buiten uit als 'de override deed niets'.
    """
    obj = _kubectl_json("get", "secret", naam, "-n", f"rig-{project_name}")
    ruwe_crt = (obj.get("data") or {}).get("tls.crt")
    if not ruwe_crt:
        return {}
    try:
        ontleed = subprocess.run(
            ["openssl", "x509", "-noout", "-subject", "-issuer"],
            input=base64.b64decode(ruwe_crt).decode("utf-8"),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return {}
    if ontleed.returncode != 0:
        return {}
    velden: dict[str, str] = {}
    for regel in ontleed.stdout.splitlines():
        sleutel, _, waarde = regel.partition("=")
        velden[sleutel.strip()] = waarde.strip()
    return velden


def _tls_endpoint() -> str:
    """Het adres waarop de ingress-controller zelf de handshake doet.

    Zie ``_TLS_KANDIDATEN``: de eerste kandidaat die een certificaat teruggeeft wint, en
    de Kind-poorten staan vooraan omdat een proxy ervoor het antwoord zou vervangen.
    """
    gekozen = os.environ.get("E2E_TLS_ENDPOINT")
    if gekozen:
        return gekozen
    for kandidaat in _TLS_KANDIDATEN:
        if _certificaat_van(kandidaat, "zad.sandbox.rijksapp.dev"):
            return kandidaat
    return _TLS_KANDIDATEN[-1]


def _certificaat_op_de_verbinding(hostnaam: str) -> dict[str, str]:
    """Het certificaat dat een client voor deze hostnaam werkelijk aangeboden krijgt."""
    return _certificaat_van(_tls_endpoint(), hostnaam)


def _certificaat_van(endpoint: str, hostnaam: str) -> dict[str, str]:
    """Een echte TLS-handshake met SNI, en daarna ``openssl x509`` over wat er terugkwam.

    Dit is het bewijs; het projectbestand is de bedoeling. Geeft {} als er geen
    certificaat uit de handshake komt (host onbekend, ingress nog niet uitgerold,
    niets dat op die poort luistert).
    """
    host, _, poort = endpoint.partition(":")
    try:
        handshake = subprocess.run(
            [
                "openssl",
                "s_client",
                "-connect",
                f"{host}:{poort or '443'}",
                "-servername",
                hostnaam,
                "-showcerts",
            ],
            input="",
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        ontleed = subprocess.run(
            ["openssl", "x509", "-noout", "-subject", "-issuer", "-dates"],
            input=handshake.stdout,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return {}
    if ontleed.returncode != 0:
        return {}
    velden: dict[str, str] = {}
    for regel in ontleed.stdout.splitlines():
        sleutel, _, waarde = regel.partition("=")
        velden[sleutel.strip()] = waarde.strip()
    return velden


def _is_platformcertificaat(cert: dict[str, str]) -> bool:
    return PLATFORM_UITGEVER in cert.get("issuer", "")


def _is_eigen_certificaat(cert: dict[str, str]) -> bool:
    return EIGEN_CN in cert.get("subject", "")


def _wacht_op(voorwaarde: Callable[[], Any], timeout_s: float = 240.0, interval: float = 5.0) -> Any:
    """Pollen tot de voorwaarde iets waars teruggeeft; geeft de laatste waarde terug.

    Op de VOORWAARDE wachten en niet op de klok: een opslag commit, verwerkt opnieuw en
    rolt uit, en dat is soms in tien seconden klaar. Een vaste sleep kost elke keer de
    volle tijd en verbergt een mislukking net zo goed als een vertraging.
    """
    deadline = time.monotonic() + timeout_s
    waarde = voorwaarde()
    while not waarde and time.monotonic() < deadline:
        time.sleep(interval)
        waarde = voorwaarde()
    return waarde


def _zelfondertekend_certificaat(hostnamen: list[str]) -> bytes:
    """Een PEM met certificaat EN sleutel, zoals de bijlage hem moet dragen.

    Zelfondertekend en met een eigen CN, want het punt is juist dat het op de verbinding
    van het platformcertificaat te onderscheiden is.
    """
    with tempfile.TemporaryDirectory() as tmp:
        crt, key = Path(tmp) / "tls.crt", Path(tmp) / "tls.key"
        san = ",".join(f"DNS:{h}" for h in hostnamen)
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "30",
                "-subj",
                f"/CN={EIGEN_CN}",
                "-addext",
                f"subjectAltName={san}",
                "-keyout",
                str(key),
                "-out",
                str(crt),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        return crt.read_bytes() + key.read_bytes()


# --- projectbestand ---------------------------------------------------------------------


def _override_config(forgejo: ForgejoClient, project_name: str, deployment: str) -> dict:
    """De publish-on-web-config op de deployment-component-laag, of {}.

    Die laag bewaart zijn diensten als een echte MAP (de component-laag gebruikt de
    gemengde lijst); dat verschil is precies waarom de override een eigen pad heeft.
    """
    data = forgejo.get_project_yaml(project_name) or {}
    for dep in data.get("deployments") or []:
        if dep.get("name") != deployment:
            continue
        for component in dep.get("components") or []:
            diensten = component.get("services")
            if isinstance(diensten, dict):
                return (diensten.get("publish-on-web") or {}).get("config") or {}
    return {}


def _api(base_url: str, api_key: str) -> httpx.Client:
    return httpx.Client(
        base_url=base_url.rstrip("/"),
        verify=_API_VERIFY_SSL,
        timeout=60.0,
        headers={"X-API-Key": api_key},
    )


# --- opzet -------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def tls_project(
    sandbox_context: BrowserContext, sandbox_url: str, forgejo: ForgejoClient
) -> Generator[tuple[str, str, str]]:
    """Een project met publish-on-web en attachments, en een TWEEDE deployment.

    Geeft (projectnaam, api-sleutel, naam van de eerste deployment).
    """
    page = sandbox_context.new_page()
    try:
        project = create_project_with_services(
            page,
            sandbox_url,
            forgejo,
            "tlsdoorloop",
            user_email=_USER_EMAIL,
            services=["publish-on-web", "attachments"],
            component_name=COMPONENT,
            create_timeout=600.0,
        )
    finally:
        page.close()

    try:
        taak = sandbox_api.start_task(
            sandbox_url,
            "POST",
            f"/api/v2/projects/{project.name}/:upsert-deployment",
            project.api_key,
            {
                "deploymentName": STAGING,
                "components": [{"reference": COMPONENT, "image": RUNNABLE_IMAGE}],
            },
            verify_ssl=_API_VERIFY_SSL,
        )
        sandbox_api.wait_for_task(sandbox_url, taak, project.api_key, verify_ssl=_API_VERIFY_SSL, timeout=600)
        yield project.name, project.api_key, project.deployment_name
    finally:
        with contextlib.suppress(Exception):
            sandbox_api.delete_project_via_api(
                sandbox_url, project.name, project.api_key, verify_ssl=_API_VERIFY_SSL, timeout=600
            )


# --- punt 6 en punt 4: de wegen naar binnen ---------------------------------------------


def test_de_api_zegt_zelf_dat_deze_laag_geen_schrijfroute_heeft(sandbox_url: str) -> None:
    """Punt 6, de API-helft: er IS geen API-weg voor deze laag, en de API liegt daar niet over.

    De generieke config-routes worden per laag gegenereerd, en de deployment-component-laag
    staat niet in die verzameling. Een klant die op ``config_endpoint`` afgaat wordt dus
    correct voorgelicht; een klant die naar analogie van de deployment-laag een pad
    verzint, krijgt niets. Beide kanten staan hier vast, want zij zijn samen de uitkomst
    van punt 6: de UI-weg werkt, de API-weg bestaat niet.
    """
    with httpx.Client(verify=_API_VERIFY_SSL, timeout=30.0) as client:
        beschrijving = client.get(f"{sandbox_url.rstrip('/')}/api/v2/services/publish-on-web")
        assert beschrijving.status_code == 200, beschrijving.text
        lagen = {laag["target"]: laag for laag in beschrijving.json()["layers"]}
        spec = client.get(f"{sandbox_url.rstrip('/')}/openapi.json").json()

    assert "deployment-component" in lagen, (
        f"publish-on-web meldt de deployment-component-laag niet meer: {sorted(lagen)}"
    )
    laag = lagen["deployment-component"]
    assert laag["has_form"] is True, "de laag is via het formulier bereikbaar, dat is de weg die er wel is"
    assert laag["config_endpoint"] is None, (
        f"de API belooft een schrijfroute voor deze laag ({laag['config_endpoint']}) -- als die er inmiddels "
        f"is, hoort deze doorloop hem ook door te meten in plaats van hem alleen vast te leggen"
    )
    verzonnen = [
        pad
        for pad in spec["paths"]
        if "publish-on-web/config/deployment/" in pad and "component" in pad.rsplit("deployment/", 1)[-1]
    ]
    assert not verzonnen, f"er is toch een route voor deze laag: {verzonnen}"


def test_provided_zonder_certificaat_wordt_geweigerd_met_de_reden(
    sandbox_url: str, tls_project: tuple[str, str, str]
) -> None:
    """Punt 4: het model weigert ``provided`` zonder bijlage, en zegt wat er ontbreekt.

    Gemeten op de weg die een klant loopt (de component-config-route van publish-on-web,
    hetzelfde model als de override). De melding moet naar het ONTBREKENDE CERTIFICAAT
    wijzen; 'het projectbestand is ongeldig' zou de gebruiker met een onvindbare fout
    achterlaten.
    """
    project_name, api_key, _ = tls_project
    with _api(sandbox_url, api_key) as client:
        antwoord = client.put(
            f"/api/v2/projects/{project_name}/services/publish-on-web/config/component/{COMPONENT}",
            json={"tls": "provided"},
        )

    print(f"[punt 4] {antwoord.status_code}: {antwoord.text}")
    assert antwoord.status_code == 422, f"verwachtte een weigering, kreeg {antwoord.status_code}: {antwoord.text}"
    tekst = antwoord.text.lower()
    assert "attachment" in tekst or "certificaat" in tekst, (
        f"de weigering noemt niet dat er een certificaat ontbreekt: {antwoord.text}"
    )
    assert "projectbestand" not in tekst, (
        f"de weigering wijst naar het projectbestand in plaats van naar het ontbrekende certificaat: {antwoord.text}"
    )


# --- de doorloop ------------------------------------------------------------------------


def _zet_override_via_de_modal(
    page: Page,
    sandbox_url: str,
    project_name: str,
    deployment_index: int,
    *,
    tls: str,
    attachment: str | None = None,
) -> None:
    """De UI-weg: modal-edit-deployment-<n>, de TLS-keuze van component 0."""
    modal = EditModalHelper(page, sandbox_url, project_name)
    modal.open_detail_page()
    modal.open_edit_modal(f"modal-edit-deployment-{deployment_index}", "Deployment bewerken")

    veld = f"deployments[{deployment_index}]/components[0]/services/publish-on-web/config/tls"
    keuze = page.locator(f"[name='{veld}']").first
    assert keuze.count() > 0, f"de deploymentmodal biedt geen TLS-keuze per component aan (verwacht veld: {veld})"
    modal.select_with_rerender(keuze, tls)

    if attachment is not None:
        bijlageveld = f"deployments[{deployment_index}]/components[0]/services/publish-on-web/config/attachment"
        bijlage = page.locator(f"[name='{bijlageveld}']").first
        assert bijlage.count() > 0, (
            f"na de keuze 'provided' verschijnt er geen certificaatveld ({bijlageveld}); zonder dat veld kan "
            f"een gebruiker de override niet afmaken en weigert het model hem"
        )
        # Gewoon kiezen en niet via select_with_rerender: het certificaatveld is het EINDE
        # van de keten (er hangt geen veld van af), dus het zet geen her-render in gang en
        # wachten op een /step/-antwoord loopt hier in een timeout.
        bijlage.select_option(attachment)

    modal.submit_step_expect_progress()
    modal.wait_for_progress_complete(timeout=600000)


def test_doorloop_van_de_tls_override(
    sandbox_page: Page,
    sandbox_url: str,
    tls_project: tuple[str, str, str],
    forgejo: ForgejoClient,
) -> None:
    """De zeven punten van het plan, op een rij en op de verbinding gemeten.

    Een doorloop en niet zeven losse tests: elke stap bouwt op de vorige toestand van
    hetzelfde draaiende project (dat is de opzet -- 'op een draaiende deployment'), en
    elke stap kost een uitrol. Zeven onafhankelijke tests zouden zeven projecten en
    zeven keer die uitrol vragen, en de sandbox is een gedeeld cluster.
    """
    project_name, api_key, productie = tls_project
    data = forgejo.get_project_yaml(project_name) or {}
    namen = [dep.get("name") for dep in data.get("deployments") or []]
    assert STAGING in namen, f"de tweede deployment staat niet in het bestand: {namen}"
    assert productie in namen, f"de eerste deployment staat niet in het bestand: {namen}"
    staging_index = namen.index(STAGING)

    host_productie = _ingress_hostnaam(project_name, f"{productie}-{COMPONENT}")
    host_staging = _ingress_hostnaam(project_name, f"{STAGING}-{COMPONENT}")
    if not host_productie or not host_staging:
        pytest.skip("kubectl geeft de ingressen niet terug; deze doorloop meet op het cluster en op de verbinding")

    # --- 1. leeg laten verandert niets ---------------------------------------------------
    assert _override_config(forgejo, project_name, STAGING) == {}, (
        "er staat al een override in het bestand terwijl er nog niets gezet is"
    )
    voor_productie = _wacht_op(lambda: _certificaat_op_de_verbinding(host_productie) or None)
    voor_staging = _wacht_op(lambda: _certificaat_op_de_verbinding(host_staging) or None)
    print(f"[punt 1] {_tls_endpoint()} | {host_productie}: {voor_productie}")
    print(f"[punt 1] {_tls_endpoint()} | {host_staging}: {voor_staging}")
    assert _is_platformcertificaat(voor_productie or {}), (
        f"zonder override hoort {host_productie} het platformcertificaat te krijgen: {voor_productie}"
    )
    assert _is_platformcertificaat(voor_staging or {}), (
        f"zonder override hoort {host_staging} het platformcertificaat te krijgen: {voor_staging}"
    )

    # En op het scherm is te zien dat leeg 'erven' betekent, met de modus die geerfd wordt.
    modal = EditModalHelper(sandbox_page, sandbox_url, project_name)
    modal.open_detail_page()
    modal.open_edit_modal(f"modal-edit-deployment-{staging_index}", "Deployment bewerken")
    veld = f"deployments[{staging_index}]/components[0]/services/publish-on-web/config/tls"
    opties = sandbox_page.eval_on_selector_all(
        f"[name='{veld}'] option", "els => els.map(e => ({value: e.value, label: e.textContent.trim()}))"
    )
    assert opties, f"de TLS-keuze van de deployment-component-laag staat niet op de modal ({veld})"
    assert opties[0]["value"] == "", f"de eerste keuze is niet de erf-optie: {opties}"
    assert "rven" in opties[0]["label"], f"de lege keuze zegt niet dat hij erft: {opties[0]}"
    # De geerfde modus staat er in de bewoording van de keuzelijst zelf ("Standaard
    # certificaat (platform regelt het)"), niet als de ruwe waarde 'standard': het label
    # is voor een lezer en niet voor een yaml-sleutel. Waar het om gaat is dat er IETS
    # genoemd wordt, anders is 'leeg' niet van 'geen TLS' te onderscheiden.
    geerfd = opties[0]["label"].split(":", 1)[-1].strip().lower()
    andere_labels = {optie["label"].strip().lower() for optie in opties[1:]}
    assert geerfd in andere_labels, (
        f"de erf-optie noemt niet WAT er geerfd wordt (of noemt iets dat geen keuze is): {opties}"
    )
    sandbox_page.keyboard.press("Escape")

    # --- de bijlage: een eigen certificaat in de projectcatalogus -------------------------
    pem = _zelfondertekend_certificaat([host_productie, host_staging])
    with _api(sandbox_url, api_key) as client:
        upload = client.post(
            f"/api/v2/projects/{project_name}/services/attachments/attachment",
            data={"attachment_id": BIJLAGE_ID},
            files={"file": (f"{BIJLAGE_ID}.pem", pem, "application/x-pem-file")},
        )
    assert upload.status_code in (200, 201), f"het certificaat kwam niet in de catalogus: {upload.text}"

    # --- 2. een eigen certificaat op de ene deployment, het platform op de andere ---------
    _zet_override_via_de_modal(
        sandbox_page, sandbox_url, project_name, staging_index, tls="provided", attachment=BIJLAGE_ID
    )

    config = _wacht_op(lambda: _override_config(forgejo, project_name, STAGING) or None, timeout_s=240)
    assert (config or {}).get("tls") == "provided", f"de override staat niet in het bestand: {config}"
    assert (config or {}).get("attachment") == BIJLAGE_ID, f"de override noemt de bijlage niet: {config}"
    assert _override_config(forgejo, project_name, productie) == {}, (
        "de andere deployment kreeg de override er ook bij; dan is het geen override per deployment"
    )

    secret = f"{STAGING}-{COMPONENT}-provided-tls"
    assert _wacht_op(lambda: _ingress_tls_secret(project_name, f"{STAGING}-{COMPONENT}") == secret, timeout_s=300), (
        f"het ingress van {STAGING} wijst niet naar het eigen certificaat-secret "
        f"(gevonden: {_ingress_tls_secret(project_name, f'{STAGING}-{COMPONENT}')!r}, verwacht {secret!r})"
    )

    # De schakel ertussen: het secret waar dat ingress naar wijst bestaat en draagt ONS
    # certificaat. Zonder deze stap zou een mislukte meting op de verbinding niet zeggen of
    # het certificaat nooit is aangekomen of onderweg is overschreven.
    uit_secret = _wacht_op(lambda: _secret_certificaat(project_name, secret) or None, timeout_s=300) or {}
    assert _is_eigen_certificaat(uit_secret), (
        f"het secret '{secret}' draagt niet het aangeleverde certificaat: {uit_secret or 'het staat er niet'}. "
        f"Een ingress dat naar een ontbrekend secret wijst valt stil terug op het standaardcertificaat van de "
        f"ingress-controller, en dat is van buiten niet van 'de override deed niets' te onderscheiden"
    )

    # DE meting: wat een client op de verbinding aangeboden krijgt, per deployment.
    def _eigen_op_staging() -> dict[str, str] | None:
        cert = _certificaat_op_de_verbinding(host_staging)
        return cert if _is_eigen_certificaat(cert) else None

    eigen = _wacht_op(_eigen_op_staging, timeout_s=420) or {}
    assert _is_eigen_certificaat(eigen), (
        f"{host_staging} biedt niet het eigen certificaat aan maar {_certificaat_op_de_verbinding(host_staging)}; "
        f"het projectbestand en het ingress zeggen 'provided', dus dit is het verschil tussen bedoeling en uitkomst"
    )
    ander = _certificaat_op_de_verbinding(host_productie)
    print(f"[punt 2] {host_staging} (override provided): {eigen}")
    print(f"[punt 2] {host_productie} (geen override): {ander}")
    assert _is_platformcertificaat(ander), (
        f"{host_productie} kreeg het eigen certificaat er ook bij ({ander}); de override geldt voor een deployment"
    )

    # --- 5. de bijlage is projectbreed: de verwijdercontrole telt de override mee ---------
    with _api(sandbox_url, api_key) as client:
        weigering = client.delete(f"/api/v2/projects/{project_name}/services/attachments/attachment/{BIJLAGE_ID}")
        assert weigering.status_code == 409, (
            f"een certificaat dat door een override gebruikt wordt is zomaar te verwijderen "
            f"({weigering.status_code}): {weigering.text}"
        )
        assert STAGING in weigering.text, (
            f"de weigering noemt niet WAAR het certificaat gebruikt wordt: {weigering.text}"
        )

        alsnog = client.delete(
            f"/api/v2/projects/{project_name}/services/attachments/attachment/{BIJLAGE_ID}",
            params={"confirm_in_use": "true"},
        )
        print(f"[punt 5] delete: {weigering.status_code} {weigering.text}")
        print(f"[punt 5] delete met confirm_in_use: {alsnog.status_code} {alsnog.text}")
        assert alsnog.status_code == 409, (
            f"met de bevestiging verdwijnt het certificaat alsnog ({alsnog.status_code}: {alsnog.text}); een site "
            f"van zijn certificaat halen is een besluit, geen bijwerking van een verwijdering"
        )

    # --- 3. provided uitzetten met een override, op een draaiende deployment -------------
    # Eerst het COMPONENT op 'provided' (de API-weg die voor die laag wel bestaat): dan
    # serveren beide deployments het eigen certificaat...
    with _api(sandbox_url, api_key) as client:
        component_config = client.put(
            f"/api/v2/projects/{project_name}/services/publish-on-web/config/component/{COMPONENT}",
            json={"tls": "provided", "attachment": BIJLAGE_ID},
        )
    assert component_config.status_code in (200, 202), component_config.text
    if component_config.status_code == 202:
        taak = (component_config.headers.get("Location") or "").rsplit("/", 1)[-1] or component_config.json().get(
            "task_id"
        )
        sandbox_api.wait_for_task(sandbox_url, taak, api_key, verify_ssl=_API_VERIFY_SSL, timeout=600)

    assert _wacht_op(lambda: _is_eigen_certificaat(_certificaat_op_de_verbinding(host_productie)), timeout_s=420), (
        f"{host_productie} volgt het component niet: het component staat op 'provided' maar de verbinding levert "
        f"{_certificaat_op_de_verbinding(host_productie)}"
    )

    # Nu wijzen er TWEE plekken naar hetzelfde certificaat: het component (en daarmee
    # productie) en de override van staging. Dat is de tweede helft van punt 5 -- de
    # verwijdercontrole moet ze allebei noemen, want een override die niet meetelt maakt
    # van een gebruikt certificaat een 'ongebruikt' certificaat.
    with _api(sandbox_url, api_key) as client:
        beide = client.delete(f"/api/v2/projects/{project_name}/services/attachments/attachment/{BIJLAGE_ID}")
    print(f"[punt 5] delete met twee gebruikers: {beide.status_code} {beide.text}")
    assert beide.status_code == 409, f"het certificaat is nu wel te verwijderen: {beide.status_code} {beide.text}"
    plekken = {(plek.get("component"), plek.get("deployment")) for plek in beide.json().get("used_by") or []}
    assert (COMPONENT, None) in plekken, f"de component-laag ontbreekt in de gebruikslijst: {plekken}"
    assert (COMPONENT, STAGING) in plekken, f"de override van {STAGING} ontbreekt in de gebruikslijst: {plekken}"

    # ... en dan zet de override op staging dat weer uit.
    _zet_override_via_de_modal(sandbox_page, sandbox_url, project_name, staging_index, tls="standard")
    config = _wacht_op(
        lambda: (_override_config(forgejo, project_name, STAGING) or {}).get("tls") == "standard", timeout_s=240
    )
    assert config, f"de override staat niet op 'standard': {_override_config(forgejo, project_name, STAGING)}"
    assert _wacht_op(lambda: _is_platformcertificaat(_certificaat_op_de_verbinding(host_staging)), timeout_s=420), (
        f"{host_staging} draagt nog steeds het eigen certificaat "
        f"({_certificaat_op_de_verbinding(host_staging)}); een override die 'provided' uitzet doet dat dan alleen "
        f"in het bestand en niet op de verbinding"
    )
    # en productie bleef ondertussen op zijn eigen certificaat
    assert _is_eigen_certificaat(_certificaat_op_de_verbinding(host_productie)), (
        f"{host_productie} verloor zijn eigen certificaat toen staging de zijne uitzette: "
        f"{_certificaat_op_de_verbinding(host_productie)}"
    )

    # --- 7. herverwerken levert dezelfde ingress met datzelfde certificaat op -------------
    # Het HELE project opnieuw verwerken, want de vraag is of de manifestgeneratie de
    # override opnieuw oppikt en niet terugvalt op het component.
    taak = sandbox_api.start_task(
        sandbox_url,
        "POST",
        f"/api/v2/projects/{project_name}/:refresh",
        api_key,
        {},
        verify_ssl=_API_VERIFY_SSL,
    )
    sandbox_api.wait_for_task(sandbox_url, taak, api_key, verify_ssl=_API_VERIFY_SSL, timeout=600)

    na_productie = _certificaat_op_de_verbinding(host_productie)
    assert _is_eigen_certificaat(na_productie), (
        f"na het herverwerken viel {host_productie} terug op iets anders dan zijn eigen certificaat: {na_productie}"
    )
    assert _ingress_tls_secret(project_name, f"{productie}-{COMPONENT}") == f"{productie}-{COMPONENT}-provided-tls", (
        "het herverwerkte ingress wijst niet meer naar het eigen certificaat-secret"
    )
    # En de kant die de override juist UITzet: die mag na het herverwerken niet alsnog
    # terugvallen op het component, want dat component staat op 'provided'.
    na_staging = _certificaat_op_de_verbinding(host_staging)
    print(f"[punt 7] na herverwerken | {host_productie}: {na_productie}")
    print(f"[punt 7] na herverwerken | {host_staging}: {na_staging}")
    assert _is_platformcertificaat(na_staging), (
        f"na het herverwerken volgt {host_staging} weer het component in plaats van zijn eigen override: {na_staging}"
    )

    # --- het bestand klopt na afloop -----------------------------------------------------
    eind = forgejo.get_project_yaml(project_name) or {}
    validate_project_schema(eind)
    assert validate_attachment_references(eind) == [], (
        f"het projectbestand verwijst naar een bijlage die niet bestaat: {validate_attachment_references(eind)}"
    )
