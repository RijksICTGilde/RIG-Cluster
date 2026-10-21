"""sandbox:create-sops-age-secret zet de sandboxsleutel in rig-system en de RON-namespace
neer, en geen sleutel meer in die namespace (RC-351).

Sinds de CMP-plugin zich splitst in een tenant- en een platform-sidecar, leest
ron-infrastructure de platformsleutel uit een mount in plaats van uit een secret in zijn
eigen namespace. Een kopie van die sleutel naar rig-ron is dan een plek die bij een
rotatie wordt vergeten (`instructions/sops-sleutel-in-het-cluster.md`).

De shell wordt echt gedraaid, met een `kubectl`-stub die zijn aanroepen opschrijft: een
test op de TEKST van de taak zou niet zien of de aanroep ook echt verdwenen is.
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
TAAK = "sandbox:create-sops-age-secret"


@pytest.fixture(scope="module")
def taakshell() -> str:
    taskfile = yaml.safe_load(TASKFILE.read_text())
    return "\n".join(str(cmd) for cmd in taskfile["tasks"][TAAK]["cmds"])


def _omgeving(tmp_path: Path, *, met_sleutel: bool = True) -> Path:
    if met_sleutel:
        (tmp_path / "security").mkdir()
        (tmp_path / "security/sandbox-key.txt").write_text("# created\n# public key: age1test\nAGE-SECRET-KEY-1TEST\n")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "kubectl").write_text(
        '#!/bin/bash\nprintf "%s\\n" "$*" >> "$KUBECTL_LOG"\ncat >/dev/null 2>&1 || true\nexit 0\n'
    )
    (bin_dir / "kubectl").chmod(0o755)
    return bin_dir


def _draai(shell: str, tmp_path: Path, bin_dir: Path) -> subprocess.CompletedProcess[str]:
    log = tmp_path / "kubectl.log"
    log.touch()
    bash = echt_programma("bash")
    return subprocess.run(
        [bash, "-c", shell],
        cwd=tmp_path,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path), "KUBECTL_LOG": str(log)},
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.fixture(autouse=True)
def _gereedschap() -> None:
    if shutil.which("bash") is None:
        pytest.skip("bash ontbreekt")


class TestSandboxCreateSopsAgeSecret:
    def test_rig_system_krijgt_het_secret(self, taakshell: str, tmp_path: Path) -> None:
        bin_dir = _omgeving(tmp_path)
        proc = _draai(taakshell, tmp_path, bin_dir)

        assert proc.returncode == 0, proc.stderr
        regels = (tmp_path / "kubectl.log").read_text().splitlines()
        assert [r for r in regels if r.startswith("create secret generic sops-age-key") and "-n rig-system" in r], (
            regels
        )

    def test_rig_ron_krijgt_de_namespace_maar_geen_secret(self, taakshell: str, tmp_path: Path) -> None:
        bin_dir = _omgeving(tmp_path)
        proc = _draai(taakshell, tmp_path, bin_dir)

        assert proc.returncode == 0, proc.stderr
        regels = (tmp_path / "kubectl.log").read_text().splitlines()
        assert [r for r in regels if r.startswith("apply -f -")], regels  # de namespace-apply
        assert not [r for r in regels if "create secret" in r and "rig-ron" in r], regels
        assert not [r for r in regels if "rig-ron" in r and "secret" in r], regels

    def test_zonder_sandbox_sleutel_stopt_de_taak(self, taakshell: str, tmp_path: Path) -> None:
        bin_dir = _omgeving(tmp_path, met_sleutel=False)
        proc = _draai(taakshell, tmp_path, bin_dir)

        assert proc.returncode != 0
        assert not (tmp_path / "kubectl.log").read_text().strip()
