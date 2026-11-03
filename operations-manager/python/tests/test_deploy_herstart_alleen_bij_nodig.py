"""deploy-operations-manager herstart de pod als alleen een secret of de configmap verandert.

Een gewijzigde pod-template rolt Kubernetes zelf uit, en een rollout restart erbovenop is
een tweede rollout tegelijk. Maar een secret komt alleen bij het starten binnen: op
3 november 2026 draaide OPI op staging na een rotatie van SECRET_KEY nog met de oude
waarde, omdat de taak aannam dat elke deploy een nieuwe image-tag meebrengt.

De shell uit de Taskfile wordt echt gedraaid, met een `kubectl` die opschrijft wat er
gebeurt.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml
from tests.programma import echt_programma

REPO_ROOT = Path(__file__).resolve().parents[3]
TASKFILE = REPO_ROOT / "Taskfile.yaml"


@pytest.fixture(scope="module")
def taakshell() -> str:
    taskfile = yaml.safe_load(TASKFILE.read_text())
    return next(cmd for cmd in taskfile["tasks"]["deploy-operations-manager"]["cmds"] if isinstance(cmd, str))


def _render(shell: str, cluster: str) -> str:
    return (
        shell.replace("{{.CLUSTER}}", cluster)
        .replace("{{.RIG_NAMESPACE}}", "rig-staging-operations")
        .replace("{{.KEY_FILE}}", "security/key.txt")
        .replace("{{.SECRETS_DIR}}", "")
    )


def _draai(shell: str, tmp_path: Path, *, template_wijzigt: bool, apply_uitvoer: str) -> list[str]:
    """Draai de taak; geeft de kubectl-acties terug die geen get of apply zijn."""
    (tmp_path / "security").mkdir(exist_ok=True)
    (tmp_path / "security/key.txt").write_text("# c\n# p\nAGE-SECRET-KEY-1TEST\n")
    (tmp_path / "template").write_text("v1")
    (tmp_path / "apply-uitvoer").write_text(apply_uitvoer)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    wijzig = 'echo v2 > "$WORK/template"' if template_wijzigt else ":"
    (bin_dir / "kubectl").write_text(
        "#!/bin/bash\n"
        'case "$*" in\n'
        '  get\\ deployment*) cat "$WORK/template" ;;\n'
        f'  apply*) cat >/dev/null; {wijzig}; cat "$WORK/apply-uitvoer" ;;\n'
        '  *) echo "$*" >> "$WORK/acties" ;;\n'
        "esac\n"
    )
    (bin_dir / "kustomize").write_text("#!/bin/bash\necho kind: Deployment\n")
    for stub in bin_dir.iterdir():
        stub.chmod(0o755)
    proc = subprocess.run(
        [echt_programma("bash"), "-c", shell],
        cwd=tmp_path,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path), "WORK": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    acties = tmp_path / "acties"
    return acties.read_text().splitlines() if acties.exists() else []


def test_alleen_een_secret_gewijzigd_herstart_de_pod(taakshell: str, tmp_path: Path) -> None:
    acties = _draai(
        _render(taakshell, "odcn-staging"),
        tmp_path,
        template_wijzigt=False,
        apply_uitvoer="secret/operations-manager-env-secrets configured\ndeployment.apps/operations-manager unchanged\n",
    )

    assert acties == ["rollout restart deployment operations-manager -n rig-staging-operations"]


def test_een_gewijzigde_template_rolt_zelf_uit_zonder_tweede_rollout(taakshell: str, tmp_path: Path) -> None:
    acties = _draai(
        _render(taakshell, "odcn-staging"),
        tmp_path,
        template_wijzigt=True,
        apply_uitvoer="secret/operations-manager-env-secrets configured\ndeployment.apps/operations-manager configured\n",
    )

    assert acties == []


def test_niets_gewijzigd_geen_herstart(taakshell: str, tmp_path: Path) -> None:
    acties = _draai(
        _render(taakshell, "odcn-staging"),
        tmp_path,
        template_wijzigt=False,
        apply_uitvoer="secret/operations-manager-env-secrets unchanged\ndeployment.apps/operations-manager unchanged\n",
    )

    assert acties == []
