"""RC-261: de bootstrap van ODCN zet het sleutelsecret zelf in rig-prd-ron.

Zonder `sops-age-key` in die namespace weigert de CMP de ron-infrastructure-app te
renderen, en dan komt de mailrelay er niet. Dat was handwerk naast de bootstrap
(`instructions/sops-sleutel-in-het-cluster.md`), dus het ontbrak op elk cluster waar
niemand die stap deed.

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
    taskfile = yaml.safe_load(TASKFILE.read_text())
    return "\n".join(str(cmd) for cmd in taskfile["tasks"][TAAK]["cmds"])


def _render(shell: str, cluster: str, dry_run: str = "false") -> str:
    """De taak met zijn template-variabelen ingevuld, zoals task ze zou invullen."""
    return (
        shell.replace("{{.CLUSTER}}", cluster)
        .replace("{{.DRY_RUN}}", dry_run)
        .replace("{{.KEY_FILE}}", "security/key.txt")
        .replace("{{.RIG_NAMESPACE}}", "rig-prd-operations")
    )


def _omgeving(tmp_path: Path, cluster: str, secret_bestaat: bool) -> Path:
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
    # Exit 1 op de lees-check van het secret betekent "bestaat niet"; de taak hoort hem dan
    # aan te maken en anders juist niet.
    (bin_dir / "kubectl").write_text(
        "#!/bin/bash\n"
        'printf "%s\\n" "$*" >> "$KUBECTL_LOG"\n'
        'if [[ "$*" == "get secret sops-age-key -n rig-prd-ron" ]]; then\n'
        f"  exit {0 if secret_bestaat else 1}\n"
        "fi\n"
        "cat >/dev/null 2>&1 || true\n"
        "exit 0\n"
    )
    (bin_dir / "kustomize").write_text('#!/bin/bash\necho "# gerenderd"\nexit 0\n')
    for stub in bin_dir.iterdir():
        stub.chmod(0o755)
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
    if shutil.which("bash") is None:
        pytest.skip("bash ontbreekt")


class TestHetSecretKomtUitDeBootstrap:
    def test_odcn_maakt_het_secret_in_rig_prd_ron(self, taakshell: str, tmp_path: Path) -> None:
        bin_dir = _omgeving(tmp_path, "odcn-production", secret_bestaat=False)
        regels = _draai(_render(taakshell, "odcn-production"), tmp_path, bin_dir)

        aanmaak = [r for r in regels if r.startswith("create secret generic sops-age-key")]
        assert len(aanmaak) == 1, regels
        assert "--from-file=key=security/key.txt" in aanmaak[0]
        assert "-n rig-prd-ron" in aanmaak[0]

    def test_een_tweede_run_laat_een_bestaand_secret_staan(self, taakshell: str, tmp_path: Path) -> None:
        """Idempotent EN geen rotatie: een apply met de lokale sleutel zou op een cluster dat
        een andere platformsleutel draagt elk projectgeheim onleesbaar maken."""
        bin_dir = _omgeving(tmp_path, "odcn-production", secret_bestaat=True)
        regels = _draai(_render(taakshell, "odcn-production"), tmp_path, bin_dir)

        assert not [r for r in regels if r.startswith("create secret generic sops-age-key")], regels

    def test_een_dry_run_schrijft_niets(self, taakshell: str, tmp_path: Path) -> None:
        bin_dir = _omgeving(tmp_path, "odcn-production", secret_bestaat=False)
        regels = _draai(_render(taakshell, "odcn-production", dry_run="true"), tmp_path, bin_dir)

        assert not [r for r in regels if r.startswith("create secret generic")], regels

    def test_sandboxed_local_raakt_rig_prd_ron_niet_aan(self, taakshell: str, tmp_path: Path) -> None:
        """Daar heet de namespace rig-ron en komt het secret uit sandbox:create-sops-age-secret;
        de guard hier is het bestaan van namespace-ron.yaml, dat alleen odcn heeft."""
        bin_dir = _omgeving(tmp_path, "sandboxed-local", secret_bestaat=False)
        regels = _draai(_render(taakshell, "sandboxed-local"), tmp_path, bin_dir)

        assert not [r for r in regels if "rig-prd-ron" in r], regels
