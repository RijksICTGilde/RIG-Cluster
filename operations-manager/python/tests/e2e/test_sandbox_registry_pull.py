"""Een eigen private registry, de gewone weg: van invullen tot een pod die draait.

`test_sandbox_registry_ownership.py` dekt de tenantscheiding (twee projecten, dezelfde
imagenaam). Wat daar niet in zit is de weg die een afnemer echt loopt: registry en token
invullen, bij een image aanwijzen welke registry erbij hoort, en dan een pod die het image
eruit haalt. Dat is het grootste blok sinds 2 september en het had geen clustertoets.

**Let op wat een draaiende pod hier WEL en NIET bewijst.** De eerste versie van dit bestand
ging ervan uit dat de registry privaat is, want `GET /v2/` geeft anoniem 401. Dat is
nagemeten en die aanname klopt niet: die 401 is de auth-UITDAGING die elke Docker-registry
geeft, publiek of niet. Doe je de tokendans die een client ook doet, dan geeft
`/v2/token?scope=repository:<org>/<image>:pull` ANONIEM een token, en daarmee komt de
manifest met 200 terug. Het image is dus publiek te halen.

Dat is twee keer waargenomen aan de kubelet-kant, met een vaste en met een eigen tag, en met
`imagePullPolicy: Always`: het image kwam er "in 50ms" terwijl kubelet in dezelfde events
`FailedToRetrieveImagePullSecret` meldde en het secret nog niet bestond. Een draaiende pod
bewijst hier dus NIET dat de inloggegevens gebruikt zijn.

Wat dit bestand wel meet, en dat is de kern van het blok: de entry landt AGE-versleuteld in
het projectbestand, ZAD zet een `dockerconfigjson`-secret in de namespace van het project met
de juiste upstream en gebruiker, en de deployment komt op met het image uit die registry en
met dat secret erbij. Alleen de stap "en zonder dat secret zou het niet lukken" is op deze
registry niet te meten. Wie dat wil, heeft een image nodig dat anoniem echt geweigerd wordt.

Draaien:

    E2E_BASE_URL=https://zad.sandbox.rijksapp.dev \
    E2E_SECRET_KEY=sandbox-dev-secret-key-fixed-for-stable-sessions-32min \
    uv run pytest tests/e2e/test_sandbox_registry_pull.py -m "e2e and sandbox" -v
"""

from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import subprocess
import uuid
from typing import TYPE_CHECKING

import httpx
import pytest
from opi.utils.age import carries_encrypted_value
from tests.e2e.conftest import FORGEJO_PASSWORD, FORGEJO_USER, SANDBOX_TEST_USER
from tests.e2e.helpers import cluster, sandbox_api
from tests.e2e.helpers.lifecycle import create_project_via_wizard
from tests.e2e.helpers.wizard import unique_project_name
from tests.e2e.helpers.zad_cli import GEEN_CLI, ZadCli, cli_pad, skip_zonder_cli

if TYPE_CHECKING:
    from collections.abc import Generator

    from playwright.sync_api import BrowserContext
    from tests.e2e.helpers.forgejo import ForgejoClient
    from tests.e2e.helpers.lifecycle import CreatedProject

logger = logging.getLogger(__name__)

# Zonder de CLI komt de registry er niet in, en dan meet de pod-toets een component die
# nooit is toegevoegd. Zonder docker komt het image niet in de registry, en dan is er geen
# image om uit te halen. Beide skips staan op de MODULE, zie `features/e2e-sandbox-tests.md`.
# `serial`: de registry moet er zijn voor de component hem kan kiezen, en het pull-secret
# voor de pod kan starten. Dat is de keten die deze module meet.
# Eigen tijdsbudget, en waarom dat moet staat in `features/e2e-sandbox-tests.md`.
# De ruimste van de zes: de setup doet een docker-login (120s) plus pull, tag en push
# (600s elk) en maakt daarna het project aan (600s).
pytestmark = [
    pytest.mark.e2e,
    pytest.mark.sandbox,
    pytest.mark.slow,
    pytest.mark.serial,
    pytest.mark.timeout(2700),
    pytest.mark.skipif(cli_pad() is None, reason=GEEN_CLI),
    pytest.mark.skipif(
        shutil.which("docker") is None,
        reason="docker ontbreekt; het testimage kan niet in de registry gezet worden",
    ),
]

_VERIFY_SSL = os.environ.get("E2E_API_VERIFY_SSL", "false").lower() in ("1", "true", "yes")

#: De registry op dit cluster: Forgejo's eigen container registry. Het pushen vraagt
#: inloggegevens en de node kan hem bereiken, en hij overleeft geen clusterherbouw (net als
#: de rest van de sandbox). Hij weigert een anonieme PULL niet, zie de module-docstring:
#: dat is de reden dat de pod-toets hieronder op het secret eindigt en niet op de pull.
#: Gebruiker en token komen uit de conftest, die ze al uit de omgeving leest.
_REGISTRY_HOST = os.environ.get("FORGEJO_REGISTRY_HOST", "forgejo.sandbox.rijksapp.dev")
_REGISTRY_ORG = FORGEJO_USER
_REGISTRY_USER = _REGISTRY_ORG
_REGISTRY_PASSWORD = FORGEJO_PASSWORD

#: De naam die het project aan deze registry geeft. Vrij te kiezen, en dat is het punt:
#: de component verwijst met deze naam, niet met de host.
_REGISTRY_NAAM = "forgejo-prive"

#: Wat er in het projectbestand en in de `auths` van het pull-secret terechtkomt.
_UPSTREAM = f"{_REGISTRY_HOST}/{_REGISTRY_ORG}"

_BRON_IMAGE = "ghcr.io/minbzk/base-images/e2e-allservices:latest"

#: Een eigen tag per run, zodat de pod niet uit een verwijzing komt die een vorige run heeft
#: achtergelaten. Dat is hygiene en geen bewijs, zie de module-docstring. De lagen zijn
#: gedeeld, dus dit kost geen extra overdracht.
_PRIVE_TAG = f"rc227-{uuid.uuid4().hex[:8]}"
_PRIVE_IMAGE = f"{_REGISTRY_HOST}/{_REGISTRY_ORG}/e2e-allservices:{_PRIVE_TAG}"


def _docker(*args: str, timeout: float = 600.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, check=False)


def _anoniem_te_halen() -> bool:
    """Kan een client zonder inloggegevens de manifest van het testimage ophalen?

    De tokendans zoals containerd hem ook doet: eerst een token vragen bij het
    token-endpoint, dan de manifest met dat token. Zonder die twee stappen meet je de
    auth-uitdaging en niet de toegang.
    """
    with httpx.Client(verify=_VERIFY_SSL, timeout=30.0) as client:
        token = client.get(
            f"https://{_REGISTRY_HOST}/v2/token",
            params={"scope": f"repository:{_REGISTRY_ORG}/e2e-allservices:pull", "service": "container_registry"},
        )
        if token.status_code != 200 or not token.json().get("token"):
            return False
        manifest = client.get(
            f"https://{_REGISTRY_HOST}/v2/{_REGISTRY_ORG}/e2e-allservices/manifests/{_PRIVE_TAG}",
            headers={
                "Authorization": f"Bearer {token.json()['token']}",
                "Accept": (
                    "application/vnd.oci.image.manifest.v1+json,"
                    "application/vnd.docker.distribution.manifest.v2+json,"
                    "application/vnd.oci.image.index.v1+json"
                ),
            },
        )
    return manifest.status_code == 200


@pytest.fixture(scope="module")
def prive_image() -> str:
    """Zet het image in de registry, en meet of die registry anoniem iets weggeeft.

    Die meting staat hier omdat ze bepaalt wat de toetsen hieronder kunnen betekenen, en ze
    doet de TOKENDANS in plaats van naar de kale 401 te kijken: `GET /v2/` antwoordt met 401
    bij elke Docker-registry, ook een publieke, want dat is de auth-uitdaging. De vraag is of
    een anonieme client daarna een token krijgt dat pull toestaat. De uitkomst is een
    waarschuwing en geen weigering: de rest van deze module meet nog steeds wat ZAD doet, maar
    de pod-toets kan er zijn sterkste claim niet op bouwen.
    """
    inlog = _docker("login", _REGISTRY_HOST, "-u", _REGISTRY_USER, "-p", _REGISTRY_PASSWORD, timeout=120.0)
    assert inlog.returncode == 0, f"inloggen op {_REGISTRY_HOST} mislukte: {inlog.stderr[:400]}"

    for argv in (("pull", _BRON_IMAGE), ("tag", _BRON_IMAGE, _PRIVE_IMAGE), ("push", _PRIVE_IMAGE)):
        stap = _docker(*argv)
        assert stap.returncode == 0, f"docker {argv[0]} mislukte: {(stap.stderr or stap.stdout)[:400]}"

    logger.info("anoniem te halen: %s", _anoniem_te_halen())
    return _PRIVE_IMAGE


@pytest.fixture(scope="module")
def registry_project(
    sandbox_context: BrowserContext,
    sandbox_url: str,
    forgejo: ForgejoClient,
) -> Generator[CreatedProject]:
    page = sandbox_context.new_page()
    gemaakt: CreatedProject | None = None
    try:
        gemaakt = create_project_via_wizard(
            page,
            sandbox_url,
            forgejo,
            unique_project_name(prefix="registry"),
            user_email=SANDBOX_TEST_USER["email"],
            # 240s is de default van de helper; op dit GEDEELDE cluster haalt de wizard
            # dat niet altijd.
            create_timeout=600.0,
        )
        yield gemaakt
    finally:
        page.close()
        if gemaakt is not None:
            sandbox_api.delete_project_via_api(sandbox_url, gemaakt.name, gemaakt.api_key, verify_ssl=_VERIFY_SSL)


@pytest.fixture(scope="module")
def cli(sandbox_url: str, registry_project: CreatedProject) -> ZadCli:
    return ZadCli(skip_zonder_cli(), sandbox_url, api_key=registry_project.api_key, project=registry_project.name)


def _pull_secrets(namespace: str) -> dict[str, dict]:
    """Elke dockerconfigjson-secret in de namespace, met zijn ontcijferde inhoud."""
    gevonden: dict[str, dict] = {}
    for item in cluster.get_json_strict("get", "secrets", "-n", namespace)["items"]:
        if item.get("type") != "kubernetes.io/dockerconfigjson":
            continue
        ruw = (item.get("data") or {}).get(".dockerconfigjson")
        if not ruw:
            continue
        gevonden[item["metadata"]["name"]] = json.loads(base64.b64decode(ruw).decode())
    return gevonden


def _registries_van(yaml: dict) -> list[dict]:
    """De registry-entries uit de dienstconfig van het project."""
    for dienst in yaml.get("services") or []:
        if isinstance(dienst, str) or dienst.get("name") != "image-registries":
            continue
        return (dienst.get("config") or {}).get("registries") or []
    return []


def _entry_staat_er(yaml: dict) -> bool:
    return any(entry.get("name") == _REGISTRY_NAAM for entry in _registries_van(yaml))


def _registry_entry(forgejo: ForgejoClient, project: str) -> dict:
    yaml = forgejo.get_project_yaml(project) or {}
    for entry in _registries_van(yaml):
        if entry.get("name") == _REGISTRY_NAAM:
            return entry
    return {}


def test_de_registry_wordt_opgeslagen_via_de_cli(
    cli: ZadCli,
    registry_project: CreatedProject,
    forgejo: ForgejoClient,
) -> None:
    """`registry add` met url, gebruikersnaam en token; het token komt versleuteld op schijf.

    Dat laatste is geen bijzaak: de entry draagt een echt token, en als dat in klare tekst
    in `zad-projects` zou belanden staat het in de git-historie van een repo die meer mensen
    kunnen lezen.

    Vraagt ``prive_image`` met opzet NIET: deze toets leest het projectbestand, en het image in
    de registry zetten kost een pull, een tag en een push. De toetsen hieronder hebben hem wel
    nodig, en daar wordt hij dus aangemaakt.
    """
    resultaat = cli.run(
        "registry",
        "add",
        _REGISTRY_NAAM,
        "--url",
        _UPSTREAM,
        "--username",
        _REGISTRY_USER,
        "--password",
        _REGISTRY_PASSWORD,
        "--yes",
    )
    logger.info("registry add: exit %d %s", resultaat.exitcode, resultaat.uitvoer.strip()[:400])
    resultaat.assert_ok()

    assert forgejo.wait_for_condition(registry_project.name, _entry_staat_er, timeout=180.0), (
        f"de registry-entry '{_REGISTRY_NAAM}' staat niet in het projectbestand"
    )

    # Op het VELD van deze entry en niet op het hele bestand. Dat laatste stond hier eerst
    # en gaf een vals alarm: de sandbox zet de git-inloggegevens van het project als
    # `password: plain:admin1234` in datzelfde bestand, en dat is hetzelfde wachtwoord als
    # dat van deze registry. De meting sloeg dus aan op een regel die er niets mee te maken
    # heeft.
    entry = _registry_entry(forgejo, registry_project.name)
    assert entry, f"de entry '{_REGISTRY_NAAM}' is niet terug te lezen"
    opgeslagen = str(entry.get("password") or "")
    assert opgeslagen != _REGISTRY_PASSWORD, "het token staat in klare tekst in het projectbestand"
    # Het schema laat TWEE opslagvormen toe (`$defs/age-encrypted`): het armored blok en de
    # `base64+age:`-regel. Welke van de twee dit veld draagt is een opslagkeuze, dus op een
    # ervan pinnen maakt deze toets rood op een wijziging die niets met het geheim te maken
    # heeft. `carries_encrypted_value` waarschuwt daar in zijn docstring precies voor.
    assert carries_encrypted_value(opgeslagen), (
        f"het token is in geen van beide AGE-vormen opgeslagen: {opgeslagen[:80]!r}"
    )


def test_een_component_uit_de_private_registry_krijgt_een_pull_secret(
    cli: ZadCli,
    registry_project: CreatedProject,
    prive_image: str,
) -> None:
    """De registrykeuze staat bij de image, en die keuze is de selectie.

    Na het toevoegen hoort er in de namespace van het project een dockerconfigjson-secret
    te staan dat precies deze registry noemt, met deze gebruikersnaam.
    """
    toevoegen = cli.run(
        "component",
        "add",
        "prive",
        "--image",
        prive_image,
        "--deployment",
        registry_project.deployment_name,
        "--port",
        "8080",
    )
    logger.info("component add: exit %d %s", toevoegen.exitcode, toevoegen.uitvoer.strip()[:400])
    toevoegen.assert_ok()

    # De keuze staat op de COMPONENTLAAG van de dienst en niet als vlag op `component add`:
    # `zad component add` heeft geen --registry (gemeten op zad-cli 0.13.1), en de laag die
    # de dienst ervoor openzet is image-registries/config/component/<naam>.
    kiezen = cli.run(
        "service",
        "config",
        "set",
        "image-registries",
        "--target",
        "component",
        "-c",
        "prive",
        "--set",
        f"registry={_REGISTRY_NAAM}",
        "--yes",
    )
    logger.info("registrykeuze bij de image: exit %d %s", kiezen.exitcode, kiezen.uitvoer.strip()[:400])
    kiezen.assert_ok()

    namespace = f"rig-{registry_project.name}"
    gevonden: dict[str, dict] = {}

    # De sleutel in `auths` is de UPSTREAM en niet de host: `backends.py:73` zet
    # `registry_url=str(upstream)`, dus `forgejo.sandbox.rijksapp.dev/rig-admin`. Op de kale
    # host zoeken vond niets, terwijl het secret er gewoon stond.
    def _secret_met_de_registry() -> bool:
        nonlocal gevonden
        gevonden = _pull_secrets(namespace)
        return any(_UPSTREAM in (inhoud.get("auths") or {}) for inhoud in gevonden.values())

    assert cluster.wait_for(_secret_met_de_registry, timeout=420.0), (
        f"geen dockerconfigjson-secret voor {_UPSTREAM} in {namespace}; wel gevonden: "
        f"{ {naam: sorted(inhoud.get('auths') or {}) for naam, inhoud in gevonden.items()} }"
    )

    auths = next(inhoud["auths"][_UPSTREAM] for inhoud in gevonden.values() if _UPSTREAM in inhoud["auths"])
    gebruiker = auths.get("username") or base64.b64decode(auths.get("auth", "")).decode().split(":", 1)[0]
    assert gebruiker == _REGISTRY_USER, f"het pull-secret draagt gebruiker '{gebruiker}' en niet '{_REGISTRY_USER}'"


def test_de_pod_haalt_het_image_uit_de_private_registry(
    registry_project: CreatedProject,
    prive_image: str,
) -> None:
    """De pod komt op met het image uit de registry, en noemt een secret dat er echt is.

    Wat hij NIET bewijst is dat de inloggegevens gebruikt zijn: deze registry laat een
    anonieme pull toe, zie de module-docstring. Daarom eindigt deze toets op de aanwezigheid
    van het secret dat de pod noemt.
    """
    namespace = f"rig-{registry_project.name}"

    def _pods_uit_de_registry() -> list[dict]:
        """Pods die DIT image draaien.

        Op de naam selecteren gaat mis: een pod heet ``<deployment>-<component>-<hash>``,
        dus hij begint met de deployment en niet met de componentnaam. Het image is
        eenduidig en is bovendien precies waar deze toets over gaat.
        """
        return [
            pod
            for pod in cluster.get_json_strict("get", "pods", "-n", namespace)["items"]
            if any(
                houder.get("image", "").startswith(prive_image.split(":")[0]) for houder in pod["spec"]["containers"]
            )
        ]

    def _pod_draait() -> bool:
        for pod in _pods_uit_de_registry():
            statussen = pod.get("status", {}).get("containerStatuses") or []
            if statussen and all(status.get("ready") for status in statussen):
                return True
        return False

    if not cluster.wait_for(_pod_draait, timeout=420.0):
        beeld = [
            (pod["metadata"]["name"], [s.get("state") for s in (pod.get("status", {}).get("containerStatuses") or [])])
            for pod in cluster.get_json_strict("get", "pods", "-n", namespace)["items"]
        ]
        pytest.fail(f"geen draaiende pod uit de private registry in {namespace}: {beeld}")

    namen = [
        verwijzing["name"]
        for pod in _pods_uit_de_registry()
        for verwijzing in (pod["spec"].get("imagePullSecrets") or [])
    ]
    assert namen, (
        "de pod draait maar noemt geen imagePullSecret; dan heeft ZAD de registrykeuze niet "
        "op de deployment doorgezet, en haalt kubelet het image anoniem of uit de cache van de node"
    )

    # En het secret dat hij noemt moet er ECHT zijn. Dit is de regel die de valse groene vangt:
    # kubelet meldt een ontbrekend pull-secret als een waarschuwing en gaat door, dus een pod
    # kan draaien terwijl het secret nooit is aangekomen.
    aanwezig = {
        item["metadata"]["name"] for item in cluster.get_json_strict("get", "secrets", "-n", namespace)["items"]
    }
    ontbreekt = [naam for naam in namen if naam not in aanwezig]
    assert not ontbreekt, (
        f"de pod noemt {ontbreekt} als imagePullSecret maar die staat niet in {namespace}; "
        f"dan kwam het image uit de cache van de node en is er niets over de registry gemeten. "
        f"Aanwezig: {sorted(aanwezig)}"
    )
