"""Een eigen private registry, de gewone weg: van invullen tot een pod die draait.

`test_sandbox_registry_ownership.py` dekt de tenantscheiding (twee projecten, dezelfde
imagenaam). Wat daar niet in zit is de weg die een afnemer echt loopt: registry en token
invullen, bij een image aanwijzen welke registry erbij hoort, en dan een pod die het image
eruit haalt. Dat is het grootste blok sinds 2 september en het had geen clustertoets.

**De registry hier is echt privaat.** Forgejo op dit cluster draagt een container registry
die anoniem 401 geeft (`GET /v2/` zonder inloggegevens), en de node kan hem bereiken. Het
image wordt door de fixture naar die registry gekopieerd. Een pod die daarna start kan dat
alleen met de credentials die ZAD in het pull-secret zette: zonder dat secret geeft de pull
401 en komt de pod niet verder dan `ImagePullBackOff`. Dat is wat deze toets tot een toets
maakt in plaats van een rondleiding, en het is ook waarom hier niet met een publieke image
gewerkt wordt: die zou ook zonder pull-secret starten.

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
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from tests.e2e.conftest import SANDBOX_TEST_USER
from tests.e2e.helpers import cluster, sandbox_api
from tests.e2e.helpers.lifecycle import create_project_via_wizard
from tests.e2e.helpers.wizard import unique_project_name
from tests.e2e.helpers.zad_cli import ZadCli, skip_zonder_cli

if TYPE_CHECKING:
    from collections.abc import Generator

    from playwright.sync_api import BrowserContext
    from tests.e2e.helpers.forgejo import ForgejoClient
    from tests.e2e.helpers.lifecycle import CreatedProject

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.e2e, pytest.mark.sandbox, pytest.mark.slow]

_VERIFY_SSL = os.environ.get("E2E_API_VERIFY_SSL", "false").lower() in ("1", "true", "yes")

#: De private registry op dit cluster. Forgejo's eigen container registry: hij weigert
#: anoniem, de node kan hem bereiken, en hij overleeft geen clusterherbouw (net als de
#: rest van de sandbox).
_REGISTRY_HOST = os.environ.get("FORGEJO_REGISTRY_HOST", "forgejo.sandbox.rijksapp.dev")
_REGISTRY_ORG = os.environ.get("FORGEJO_USER", "rig-admin")
_REGISTRY_USER = _REGISTRY_ORG
_REGISTRY_PASSWORD = os.environ.get("FORGEJO_PASSWORD", "admin1234")

#: De naam die het project aan deze registry geeft. Vrij te kiezen, en dat is het punt:
#: de component verwijst met deze naam, niet met de host.
_REGISTRY_NAAM = "forgejo-prive"

_BRON_IMAGE = "ghcr.io/minbzk/base-images/e2e-allservices:latest"
_PRIVE_IMAGE = f"{_REGISTRY_HOST}/{_REGISTRY_ORG}/e2e-allservices:rc227"


def _docker(*args: str, timeout: float = 600.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, check=False)


@pytest.fixture(scope="module")
def prive_image() -> str:
    """Zorg dat het image in de private registry staat, en dat hij echt privaat is.

    De controle op 401 staat hier en niet in een toets: is de registry niet privaat, dan
    meet elke toets hieronder niets en hoort de suite dat meteen te zeggen.
    """
    if not shutil.which("docker"):
        pytest.skip("docker ontbreekt; de fixture kan het image niet in de registry zetten")

    with httpx.Client(verify=_VERIFY_SSL, timeout=30.0) as client:
        anoniem = client.get(f"https://{_REGISTRY_HOST}/v2/")
    assert anoniem.status_code == 401, (
        f"de registry op {_REGISTRY_HOST} weigert anoniem niet (HTTP {anoniem.status_code}); "
        "dan bewijst een startende pod niets over het pull-secret"
    )

    inlog = _docker("login", _REGISTRY_HOST, "-u", _REGISTRY_USER, "-p", _REGISTRY_PASSWORD, timeout=120.0)
    assert inlog.returncode == 0, f"inloggen op {_REGISTRY_HOST} mislukte: {inlog.stderr[:400]}"

    for argv in (("pull", _BRON_IMAGE), ("tag", _BRON_IMAGE, _PRIVE_IMAGE), ("push", _PRIVE_IMAGE)):
        stap = _docker(*argv)
        assert stap.returncode == 0, f"docker {argv[0]} mislukte: {(stap.stderr or stap.stdout)[:400]}"

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
            # 240s is de default van de helper; op dit GEDEELDE cluster haalt een project
            # met diensten dat niet altijd. Een ruimere wacht is hier geen verdoezeling: de
            # toets meet wat er daarna gebeurt, niet hoe snel de wizard is.
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


def _kubectl_json(args: list[str]) -> dict[str, Any]:
    ruw = subprocess.run(["kubectl", *args, "-o", "json"], capture_output=True, text=True, timeout=60, check=True)
    return json.loads(ruw.stdout)


def _pull_secrets(namespace: str) -> dict[str, dict]:
    """Elke dockerconfigjson-secret in de namespace, met zijn ontcijferde inhoud."""
    gevonden: dict[str, dict] = {}
    for item in _kubectl_json(["get", "secrets", "-n", namespace])["items"]:
        if item.get("type") != "kubernetes.io/dockerconfigjson":
            continue
        ruw = (item.get("data") or {}).get(".dockerconfigjson")
        if not ruw:
            continue
        gevonden[item["metadata"]["name"]] = json.loads(base64.b64decode(ruw).decode())
    return gevonden


def test_de_registry_wordt_opgeslagen_via_de_cli(
    cli: ZadCli,
    registry_project: CreatedProject,
    forgejo: ForgejoClient,
    prive_image: str,
) -> None:
    """`registry add` met url, gebruikersnaam en token; het token komt versleuteld op schijf.

    Dat laatste is geen bijzaak: de entry draagt een echt token, en als dat in klare tekst
    in `zad-projects` zou belanden staat het in de git-historie van een repo die meer mensen
    kunnen lezen.
    """
    resultaat = cli.run(
        "registry",
        "add",
        _REGISTRY_NAAM,
        "--url",
        f"{_REGISTRY_HOST}/{_REGISTRY_ORG}",
        "--username",
        _REGISTRY_USER,
        "--password",
        _REGISTRY_PASSWORD,
        "--yes",
    )
    logger.info("registry add: exit %d %s", resultaat.exitcode, resultaat.uitvoer.strip()[:400])
    resultaat.assert_ok()

    def _entry_staat_er(yaml: dict) -> bool:
        for dienst in yaml.get("services") or []:
            if isinstance(dienst, str):
                continue
            config = dienst.get("config") or (dienst.get("image-registries") or {}).get("config") or {}
            for entry in config.get("registries") or []:
                if entry.get("name") == _REGISTRY_NAAM:
                    return True
        return False

    assert forgejo.wait_for_condition(registry_project.name, _entry_staat_er, timeout=180.0), (
        f"de registry-entry '{_REGISTRY_NAAM}' staat niet in het projectbestand"
    )

    inhoud = forgejo.get_project_file(registry_project.name) or ""
    assert _REGISTRY_PASSWORD not in inhoud, "het token staat in klare tekst in het projectbestand"


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
    # `zad component add` heeft geen --registry (gemeten op zad-cli 1.0.0), en de laag die
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

    def _secret_met_de_registry() -> bool:
        nonlocal gevonden
        gevonden = _pull_secrets(namespace)
        return any(_REGISTRY_HOST in (inhoud.get("auths") or {}) for inhoud in gevonden.values())

    assert cluster.wait_for(_secret_met_de_registry, timeout=240.0), (
        f"geen dockerconfigjson-secret voor {_REGISTRY_HOST} in {namespace}; wel gevonden: {sorted(gevonden)}"
    )

    auths = next(inhoud["auths"][_REGISTRY_HOST] for inhoud in gevonden.values() if _REGISTRY_HOST in inhoud["auths"])
    gebruiker = auths.get("username") or base64.b64decode(auths.get("auth", "")).decode().split(":", 1)[0]
    assert gebruiker == _REGISTRY_USER, f"het pull-secret draagt gebruiker '{gebruiker}' en niet '{_REGISTRY_USER}'"


def test_de_pod_haalt_het_image_uit_de_private_registry(
    registry_project: CreatedProject,
    prive_image: str,
) -> None:
    """De echte uitkomst: de pod draait.

    Anoniem geeft deze registry 401, dus een draaiende pod betekent dat het pull-secret
    gebruikt IS. Zonder de vorige toets zou een groene regel hier ook kunnen betekenen dat
    de node het image nog in zijn cache had; daarom staat het secret daar apart gemeten.
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
            for pod in _kubectl_json(["get", "pods", "-n", namespace])["items"]
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
            for pod in _kubectl_json(["get", "pods", "-n", namespace])["items"]
        ]
        pytest.fail(f"geen draaiende pod uit de private registry in {namespace}: {beeld}")

    namen = [
        verwijzing["name"]
        for pod in _pods_uit_de_registry()
        for verwijzing in (pod["spec"].get("imagePullSecrets") or [])
    ]
    assert namen, "de pod draait maar noemt geen imagePullSecret; dan is de registry niet privaat genoeg"
