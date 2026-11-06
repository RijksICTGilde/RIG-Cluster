"""De bootstrap van ODCN zet de RON-namespace neer, en geen sleutel erin.

RC-261 kopieerde de platformsleutel nog naar `rig-prd-ron`, omdat de CMP de sleutel uit
de bestemmingsnamespace las. Sinds RC-351 rendert ron-infrastructure op de
platform-plugin, die de sleutel uit een mount leest, en hoort die kopie er juist niet
meer te staan: een tweede plek met de platformsleutel is een plek die bij een rotatie
wordt vergeten (`docs/development/sops-sleutel-in-het-cluster.md`).

De shell uit de Taskfile wordt echt gedraaid, met een `kubectl` die zijn aanroepen
opschrijft. Een test op de TEKST van de taak zou niet zien dat de aanroep achter de
dry-run-guard of achter de verkeerde `if` staat.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from tests.programma import echt_programma

REPO_ROOT = Path(__file__).resolve().parents[3]
TASKFILE = REPO_ROOT / "Taskfile.yaml"
TAAK = "bootstrap-argo-system"


@pytest.fixture(scope="module")
def taakshell() -> str:
    """De shell-stappen van de taak, zonder de aanroepen naar een andere taak.

    ``bootstrap-argo-system`` roept sinds RC-349 ook ``argocd:trust-git-signing-key``
    aan; die ``task:``-vermelding is een dict, geen shell, en hoort hier niet in mee
    te joinen.
    """
    taskfile = yaml.safe_load(TASKFILE.read_text())
    return "\n".join(cmd for cmd in taskfile["tasks"][TAAK]["cmds"] if isinstance(cmd, str))


def _render(shell: str, cluster: str, dry_run: str = "false") -> str:
    """De taak met zijn template-variabelen ingevuld, zoals task ze zou invullen."""
    return (
        shell.replace("{{.CLUSTER}}", cluster)
        .replace("{{.DRY_RUN}}", dry_run)
        .replace("{{.KEY_FILE}}", "security/key.txt")
        .replace("{{.RIG_NAMESPACE}}", "rig-prd-operations")
    )


def _omgeving(tmp_path: Path, cluster: str) -> Path:
    """Een werkmap met de overlay-bestanden, de sleutel, en stubs voor kubectl en kustomize."""
    overlay = tmp_path / "bootstrap/rig-system/kustomize/overlays" / cluster
    overlay.mkdir(parents=True)
    (overlay / "kustomization.yaml").write_text("resources: []\n")
    if cluster == "odcn-production":
        (overlay / "namespace-ron.yaml").write_text("apiVersion: v1\nkind: Namespace\nmetadata:\n  name: rig-prd-ron\n")

    (tmp_path / "security").mkdir()
    (tmp_path / "security/key.txt").write_text("# created\n# public key: age1test\nAGE-SECRET-KEY-1TEST\n")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "kubectl").write_text(
        '#!/bin/bash\nprintf "%s\\n" "$*" >> "$KUBECTL_LOG"\ncat >/dev/null 2>&1 || true\nexit 0\n'
    )
    (bin_dir / "kustomize").write_text('#!/bin/bash\necho "# gerenderd"\nexit 0\n')
    for stub in bin_dir.iterdir():
        stub.chmod(0o755)
    # yq is geen stub: de taak leest de naam van de RON-namespace er echt mee uit
    # namespace-ron.yaml, en een leeg antwoord zou het label naar niets laten wijzen.
    (bin_dir / "yq").symlink_to(echt_programma("yq"))
    return bin_dir


def _draai(shell: str, tmp_path: Path, bin_dir: Path) -> list[str]:
    log = tmp_path / "kubectl.log"
    log.touch()
    bash = echt_programma("bash")
    proc = subprocess.run(
        [bash, "-c", shell],
        cwd=tmp_path,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path), "KUBECTL_LOG": str(log)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return log.read_text().splitlines()


@pytest.fixture(autouse=True)
def _gereedschap() -> None:
    for programma in ("bash", "yq"):
        if shutil.which(programma) is None:
            pytest.skip(f"{programma} ontbreekt")


class TestDeRonNamespaceUitDeBootstrap:
    def test_odcn_zet_de_namespace_met_zijn_managed_by_label(self, taakshell: str, tmp_path: Path) -> None:
        bin_dir = _omgeving(tmp_path, "odcn-production")
        regels = _draai(_render(taakshell, "odcn-production"), tmp_path, bin_dir)

        assert [r for r in regels if r.startswith("apply -f bootstrap") and "namespace-ron.yaml" in r], regels
        assert [
            r
            for r in regels
            if r == "label namespace rig-prd-ron argocd.argoproj.io/managed-by=rig-prd-operations --overwrite"
        ], regels

    def test_er_gaat_geen_sleutel_in_de_ron_namespace(self, taakshell: str, tmp_path: Path) -> None:
        """De platform-plugin leest de sleutel uit zijn mount, dus een tweede houder van de
        platformsleutel hoort hier niet meer aangemaakt te worden.

        De sleutel in de operations-namespace blijft wel: dat is de houder die de
        cmp-platform-sidecar mount.
        """
        bin_dir = _omgeving(tmp_path, "odcn-production")
        regels = _draai(_render(taakshell, "odcn-production"), tmp_path, bin_dir)

        assert not [r for r in regels if "sops-age-key" in r and "rig-prd-ron" in r], regels
        assert [r for r in regels if r == "get secret sops-age-key -n rig-prd-operations"], regels

    def test_een_dry_run_schrijft_niets(self, taakshell: str, tmp_path: Path) -> None:
        bin_dir = _omgeving(tmp_path, "odcn-production")
        regels = _draai(_render(taakshell, "odcn-production", dry_run="true"), tmp_path, bin_dir)

        assert not [r for r in regels if r.startswith("label namespace")], regels

    def test_sandboxed_local_raakt_rig_prd_ron_niet_aan(self, taakshell: str, tmp_path: Path) -> None:
        """Daar heet de namespace rig-ron en komt hij uit sandbox:create-sops-age-secret;
        de guard hier is het bestaan van namespace-ron.yaml, dat alleen odcn heeft."""
        bin_dir = _omgeving(tmp_path, "sandboxed-local")
        regels = _draai(_render(taakshell, "sandboxed-local"), tmp_path, bin_dir)

        assert not [r for r in regels if "rig-prd-ron" in r], regels
