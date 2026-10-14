"""Toetsen bij het verdwijnen van de clustersoort `local` (het kale kind-cluster gitops-fluxcd).

Wat die opruiming moet opleveren, en wat hier gemeten wordt:
  1. er is geen overlay, clustermap of env-bestand van dat cluster meer in de boom;
  2. een taak die een overlaypad uit `CLUSTER_TYPE` bouwt, weigert zonder die variabele in
     plaats van stil op `local` te vallen. Dat was de schade: `task skaffold-dev` renderde
     overlays/local en deployde naar kind-rig-sandbox;
  3. `KIND_CLUSTER_NAME` valt terug op `rig-sandbox`, niet op het verdwenen gitops-fluxcd;
  4. `sandbox:setup` loopt zonder `.env-taskfile-current`, dat hij zelf pas aanmaakt;
  5. `select-cluster` stopt op een ongeldige keuze in plaats van op `local` terug te vallen.

De Taskfile wordt echt gedraaid, met stubs voor het clustergereedschap zodat een run die te
ver komt geen echt cluster raakt.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from tests.programma import echt_programma

REPO_ROOT = Path(__file__).resolve().parents[3]
TASKFILE = REPO_ROOT / "Taskfile.yaml"
BRON_TAG = REPO_ROOT / "scripts/bron-tag.sh"

STUB_TOOLS = (
    "docker",
    "kind",
    "kubectl",
    "kustomize",
    "sops",
    "age",
    "ksops",
    "pwgen",
    "jq",
    "yq",
    "rsync",
    "skaffold",
)


@pytest.fixture(scope="module")
def taskfile() -> dict:
    return yaml.safe_load(TASKFILE.read_text())


@pytest.fixture
def boom(tmp_path: Path) -> Path:
    """Een git-repo met de Taskfile en wat zijn globale vars lezen, zonder .env-taskfile-current.

    Git hoort erbij: de globale var SANDBOX_OM_TAG leest de commit, en zonder repo valt elke
    taak om voordat hij aan zijn eigen weigering komt. Hij rekent dat uit in
    `scripts/bron-tag.sh`, dus dat script gaat om dezelfde reden mee.
    """
    if shutil.which("bash") is None or shutil.which("git") is None:
        pytest.skip("bash of git ontbreekt")
    shutil.copy(TASKFILE, tmp_path / "Taskfile.yaml")
    (tmp_path / "scripts").mkdir()
    shutil.copy(BRON_TAG, tmp_path / "scripts" / BRON_TAG.name)
    # De env-bestanden van de twee clusters die er nog zijn, zodat select-cluster een
    # geldige keuze echt kan afmaken. Niet .env-taskfile-current: die hoort te ontbreken.
    for naam in (".env-taskfile-odcn-production", ".env-taskfile-sandboxed-local"):
        shutil.copy(REPO_ROOT / naam, tmp_path / naam)
    for args in (("init", "-q"), ("add", "."), ("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for tool in STUB_TOOLS:
        (stubs / tool).write_text("#!/bin/sh\nexit 0\n")
        (stubs / tool).chmod(0o755)
    return tmp_path


def _task(boom: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k not in ("CLUSTER_TYPE", "KIND_CLUSTER_NAME")}
    env["PATH"] = f"{boom / 'stubs'}{os.pathsep}{env['PATH']}"
    return subprocess.run([echt_programma("task"), *args], cwd=boom, env=env, capture_output=True, text=True)


def test_geen_overlay_clustermap_of_envbestand_van_local_meer() -> None:
    """Op het patroon en niet op een namenlijst: een nieuwe local-overlay valt hier ook op."""
    gevolgd = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files"], check=True, capture_output=True, text=True
    ).stdout.splitlines()

    achtergebleven = [
        naam
        for naam in gevolgd
        if "/overlays/local/" in naam
        or naam.startswith("infrastructure/bootstrap/clusters/local/")
        or naam in {".env-taskfile-local", "kind-config.yaml", "operations-manager/skaffold.yaml"}
    ]

    assert achtergebleven == []


@pytest.mark.parametrize(
    "taak",
    [
        "deploy-operations-manager",
        "bootstrap-argo-system",
        "generate-env-secrets-for-operations-manager",
        "publish-operations-manager",
    ],
)
def test_een_overlaypad_zonder_clustersoort_weigert(boom: Path, taak: str) -> None:
    """Zonder default valt zo'n taak niet meer op een clustersoort die niet bestaat.

    publish-operations-manager heeft zelf geen `requires` en erft de weigering via `deps`.
    """
    uitslag = _task(boom, "--dry", taak)

    assert uitslag.returncode != 0
    assert "CLUSTER_TYPE" in uitslag.stderr + uitslag.stdout


def test_een_overlaypad_met_een_geldige_clustersoort_loopt_wel(boom: Path) -> None:
    uitslag = _task(boom, "--dry", "deploy-operations-manager", "CLUSTER_TYPE=odcn-production")

    assert uitslag.returncode == 0, uitslag.stderr


def test_local_wordt_als_clustersoort_geweigerd(boom: Path) -> None:
    uitslag = _task(boom, "--dry", "deploy-operations-manager", "CLUSTER_TYPE=local")

    assert uitslag.returncode != 0
    assert "local" in uitslag.stderr + uitslag.stdout


def test_de_wezensweep_eist_ook_een_clustersoort(boom: Path) -> None:
    """Hij gaf CLUSTER_TYPE door als CLUSTER_MANAGER, dus zonder default was dat leeg.

    Dat is erger dan de oude default: een leeg CLUSTER_MANAGER laat het script op zoek gaan
    in een cluster dat niemand koos, waar `local` nog een naam was die ergens op sloeg.
    """
    uitslag = _task(boom, "--dry", "argocd-orphan-sweep")

    assert uitslag.returncode != 0
    assert "CLUSTER_TYPE" in uitslag.stderr + uitslag.stdout


def test_kind_cluster_name_valt_terug_op_de_sandbox(boom: Path) -> None:
    """De odcn-env laat KIND_CLUSTER_NAME leeg, en dan laadde de sandbox in gitops-fluxcd."""
    uitslag = _task(boom, "--dry", "--verbose", "sandbox:build-cmp-image")

    assert uitslag.returncode == 0, uitslag.stderr
    assert "--name rig-sandbox" in uitslag.stdout + uitslag.stderr
    assert "gitops-fluxcd" not in uitslag.stdout + uitslag.stderr


def test_sandbox_setup_loopt_zonder_env_bestand(boom: Path) -> None:
    """Hij maakt .env-taskfile-current zelf aan, maar dotenv is dan al gelezen."""
    uitslag = _task(boom, "--dry", "sandbox:setup")

    assert uitslag.returncode == 0, uitslag.stderr + uitslag.stdout


def _select_cluster(boom: Path, taskfile: dict, keuze: str) -> subprocess.CompletedProcess[str]:
    script = "\n".join(str(cmd) for cmd in taskfile["tasks"]["select-cluster"]["cmds"])
    return subprocess.run(["bash", "-c", script], cwd=boom, input=f"{keuze}\n", capture_output=True, text=True)


def test_select_cluster_weigert_een_ongeldige_keuze(boom: Path, taskfile: dict) -> None:
    """Viel terug op local, inclusief het kopieren van .env-taskfile-local naar current."""
    uitslag = _select_cluster(boom, taskfile, "3")

    assert uitslag.returncode != 0
    assert not (boom / ".env-taskfile-current").exists()


def test_select_cluster_kiest_nog_wel_een_geldig_cluster(boom: Path, taskfile: dict) -> None:
    """De andere kant: zonder dit is de weigering hierboven ook waar als niets meer werkt."""
    uitslag = _select_cluster(boom, taskfile, "2")

    assert uitslag.returncode == 0, uitslag.stderr
    assert "CLUSTER_TYPE=sandboxed-local" in (boom / ".env-taskfile-current").read_text()
