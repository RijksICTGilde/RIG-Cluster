"""Een test die een container start, start de versie die het platform uitrolt.

De opstelling van een toets drijft stil af: ``tests/integration/authwall_harness.py`` pinde
Keycloak 25.0.6 met de tekst "same version as production" terwijl productie 26.8.1 draaide,
en de test-PostgreSQL stond op 16 terwijl het platform 17 draait. Een groene run tegen de
verkeerde versie meet niets over wat er uitgerold is.

Deze toets eist daarom dat elke image die een test, een test-task of de compose-opstelling
start uit een pin in ``infrastructure/`` of ``bootstrap/`` komt, of met een reden op de lijst
hieronder staat.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import yaml
from tests.image_pins import REPO_ROOT, base_image_from_dockerfile, pinned_image, pinned_images

TESTS_ROOT = Path(__file__).resolve().parent
TASKFILE = REPO_ROOT / "Taskfile.yaml"

#: Alleen bestanden die ook een container starten; een image-naam in testdata is geen
#: opstelling. De naam van de constante doet de rest.
STARTS_A_CONTAINER = re.compile(r"\bdocker\b|\bkind\b")

#: Images in een testopstelling die niet uit een pin komen, met de reden.
EXEMPT: dict[str, str] = {
    "zad-test-authwall-proxy:local": "wordt in de taak zelf gebouwd uit de manifesten van de auth-wall",
    "zad/keycloak-zad:toets": "de image die task test-keycloak-image hier bouwt, om hem te toetsen voordat hij gepubliceerd wordt",
    "amazon/aws-cli:2.31.11": "gereedschap van de meting (een S3-client), geen dienst die het platform uitrolt",
    "ghcr.io/minbzk/base-images/rig-backup:latest": "gereedschap van de meting (mc en kopia); de cronjob pint dezelfde image op een digest",
    "busybox:1.37": "gereedschap van de meting (een shell in een pod), geen dienst die het platform uitrolt",
    "nginxinc/nginx-unprivileged:1.27-alpine": "de werklast van tasks_live: een app die blijft draaien, geen dienst die het platform uitrolt",
    "nginxinc/nginx-unprivileged:1.26-alpine": "dezelfde werklast op een ANDERE tag, zodat het update-image-scenario de wijziging kan zien",
    "busybox:latest": "bronimage om een registry te vullen; welke tag dat is, doet voor die meting niet mee",
    "ghcr.io/minbzk/base-images/e2e-allservices:latest": "de applicatie die de e2e-projecten draaien, geen platformdienst",
    "code.overheid.nl/robbert.uittenbroek/zad-deployment-demo:0a611d9d": "testdata: een image-verwijzing in een projectbestand, er start niets",
}


#: De plekken die de PostgreSQL van het platform zelf starten. De CNPG-image heeft geen
#: ENTRYPOINT, dus elk van hen geeft dit script mee.
STARTS_PLATFORM_POSTGRES = (
    "Taskfile.yaml",
    "docker-compose.dev.yaml",
    "operations-manager/python/tests/conftest.py",
)
PG_START_SCRIPT = "scripts/postgres-standalone-start.sh"
#: Waar het script in de container hangt. Alleen een echte mount of een echt commando noemt
#: dit pad, een verwijzing in commentaar niet.
PG_START_IN_CONTAINER = "/zad/postgres-standalone-start.sh"


def _module_image_constants() -> dict[str, str]:
    """De module-constanten met een image in een bestand dat ook containers start."""
    found: dict[str, str] = {}
    for path in sorted(TESTS_ROOT.rglob("*.py")):
        text = path.read_text()
        if not STARTS_A_CONTAINER.search(text):
            continue
        for node in ast.parse(text).body:
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Constant):
                continue
            value = node.value.value
            # Een image-verwijzing heeft geen witruimte; een logregel met "image" erin wel.
            if not isinstance(value, str) or ":" not in value or " " in value:
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and "IMAGE" in target.id:
                    found[f"{path.relative_to(REPO_ROOT)}:{target.id}"] = value
    return found


def _taskfile_image_vars() -> dict[str, str]:
    """De image-vars van de taken die een toets draaien."""
    document = yaml.safe_load(TASKFILE.read_text())
    found: dict[str, str] = {}
    for name, task in document["tasks"].items():
        if not (name.startswith("test") or name.endswith("-live-group")):
            continue
        for key, value in (task.get("vars") or {}).items():
            if "IMAGE" in key and isinstance(value, str) and ":" in value:
                found[f"Taskfile.yaml:{name}:{key}"] = value
    return found


def _compose_images() -> dict[str, str]:
    """De images van de compose-opstelling voor lokale ontwikkeling.

    Compose kan geen pin lezen, dus daar staat de tag voluit; deze toets is wat hem bij de
    pin houdt.
    """
    found: dict[str, str] = {}
    for path in sorted(REPO_ROOT.glob("docker-compose*.yaml")):
        document = yaml.safe_load(path.read_text())
        for name, service in (document.get("services") or {}).items():
            image = service.get("image")
            if isinstance(image, str) and ":" in image:
                found[f"{path.name}:{name}"] = image
    return found


def _started_images() -> dict[str, str]:
    return {**_module_image_constants(), **_taskfile_image_vars(), **_compose_images()}


def test_the_scan_still_finds_the_test_setup() -> None:
    """Een scan die niets vindt, bewaakt niets: dit is de bodem onder de toets hieronder."""
    found = _started_images()
    assert len(found) >= 8, f"De scan vindt bijna niets meer; is de vorm van de opstelling veranderd? {found}"


def test_every_image_a_test_starts_comes_from_a_pin() -> None:
    pins = pinned_images()
    drifted = {where: image for where, image in _started_images().items() if image not in pins and image not in EXEMPT}
    assert not drifted, (
        f"Deze images komen niet uit een pin in infrastructure/ of bootstrap/: {drifted}. Lees de pin "
        "met tests/image_pins.py, of zet de image met een reden in EXEMPT."
    )


def test_the_exemptions_still_apply() -> None:
    """Een uitzondering voor een image die niemand meer start, gaat weg."""
    started = set(_started_images().values())
    stale = sorted(EXEMPT.keys() - started)
    assert not stale, f"EXEMPT noemt images die geen test meer start: {stale}"
    assert all(reason for reason in EXEMPT.values()), "Elke uitzondering noemt een reden"


@pytest.mark.parametrize("where", sorted(EXEMPT))
def test_no_exemption_hides_a_pinned_image(where: str) -> None:
    """Staat de image wel gepind, dan hoort de opstelling die pin te lezen."""
    assert where not in pinned_images(), f"{where} staat gepind; lees die pin in plaats van de uitzondering"


def test_the_platform_postgres_gets_the_opstart_it_needs() -> None:
    """De CNPG-image start niet met een kaal ``docker run``, en dat is stil kapot."""
    assert (REPO_ROOT / PG_START_SCRIPT).is_file(), f"{PG_START_SCRIPT} ontbreekt"
    zonder = [
        where for where in STARTS_PLATFORM_POSTGRES if PG_START_IN_CONTAINER not in (REPO_ROOT / where).read_text()
    ]
    assert not zonder, (
        f"Deze plekken starten de PostgreSQL van het platform zonder {PG_START_SCRIPT}: {zonder}. "
        "De CNPG-image heeft geen ENTRYPOINT, dus hij start dan bash en eindigt direct."
    )


#: De taak die het wegwerpcluster van kubernetes_live en argocd_live opzet, en de vars
#: waarmee hij zijn images kiest, met het bestand waar die pin hoort te staan.
TEST_CLUSTER_TASK = "test-cluster-up"
TEST_CLUSTER_PINS = {
    "NODE_IMAGE": "bootstrap/sandbox/kind-config.yaml",
    "FORGEJO_IMAGE": "infrastructure/bootstrap/infrastructure/forgejo/controller/overlays/sandboxed-local/statefulset-patch.yaml",
    "ARGOCD_IMAGE": "bootstrap/rig-system/kustomize/overlays/sandboxed-local/argocd-deployment.yaml",
    "ARGOCD_VERSION": "bootstrap/rig-system/kustomize/overlays/sandboxed-local/argocd-deployment.yaml",
}


def _test_cluster_vars() -> dict[str, object]:
    document = yaml.safe_load(TASKFILE.read_text())
    return document["tasks"][TEST_CLUSTER_TASK]["vars"]


def test_the_test_cluster_reads_its_images_from_a_pin() -> None:
    """Het wegwerpcluster draait de versies die het platform uitrolt.

    Een tag die hier voluit stond, zou stil afdrijven van de sandbox en van productie; dat
    is precies hoe de authwall-opstelling op Keycloak 25.0.6 bleef staan. Elke var leest
    daarom het manifest, en deze toets noemt welk manifest dat is.
    """
    vars_of_the_task = _test_cluster_vars()
    for name, pin in TEST_CLUSTER_PINS.items():
        entry = vars_of_the_task.get(name)
        assert isinstance(entry, dict), f"{TEST_CLUSTER_TASK} zet {name} niet als var met een `sh:`: {entry}"
        assert "sh" in entry, f"{TEST_CLUSTER_TASK} zet {name} met een vaste waarde: {entry}"
        assert pin in entry["sh"], f"{TEST_CLUSTER_TASK}:{name} leest {pin} niet"
        assert (REPO_ROOT / pin).is_file(), f"{pin} bestaat niet"


def test_the_kind_node_image_of_every_test_cluster_follows_the_sandbox_pin() -> None:
    """De node-image staat op een plek, en de meetclusters lezen die.

    Hij hoort de Kubernetes-versie van ODCN te volgen; die stond bij het schrijven niet
    vast, dus hij volgt de sandbox. Verandert dat, dan is het een regel in
    bootstrap/sandbox/kind-config.yaml.
    """
    pinned = pinned_image("bootstrap/sandbox/kind-config.yaml", contains="kindest/node")
    version = pinned.split(":v", 1)[1]

    taskfile_version = yaml.safe_load(TASKFILE.read_text())["vars"]["KIND_VERSION"]
    assert str(taskfile_version) == version, (
        f"Taskfile.yaml vars.KIND_VERSION is {taskfile_version} en de pin is {version}. "
        "tests/netpol/kind-config.yaml krijgt zijn node-image uit die var."
    )


def test_the_cmp_sidecar_runs_the_argocd_of_the_platform() -> None:
    """The CMP sidecar and argocd-rig are built on the same upstream Argo CD.

    Argo CD is pinned in two places, and Renovate tracks only one: argocd-rig needs its
    patches rebased, so its BASE_TAG moves by hand. The sidecar stayed on v2.14.21, with its
    critical CVEs, while the platform went to v3.5.6.
    """
    rig_vars = yaml.safe_load((REPO_ROOT / "images/argocd-rig/Taskfile.yaml").read_text())["vars"]
    rig_version = re.search(r'default "(v[\d.]+)"', rig_vars["BASE_TAG"])
    assert rig_version, f"images/argocd-rig/Taskfile.yaml BASE_TAG has no default version: {rig_vars['BASE_TAG']}"

    sidecar_base = base_image_from_dockerfile("images/cmp-kustomize-sops/Dockerfile")
    assert sidecar_base == f"quay.io/argoproj/argocd:{rig_version.group(1)}", (
        f"The CMP sidecar is built on {sidecar_base}, argocd-rig on Argo CD {rig_version.group(1)}. Move both together."
    )
