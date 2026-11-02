"""RC-356: de bootstrap kan de dichte `default` overslaan, voor stap 1 van de uitrol.

De uitrol van de AppProjects gaat in twee stappen (`features/argocd-appprojects.md`): eerst
de nieuwe AppProjects en de omgezette Applications, met `default` nog open zoals hij in het
cluster staat, en pas daarna `default` dicht. Zonder die eerste stap zou een Application die
nog op `default` staat tussen de twee applies in niets meer mogen.

De shell uit de Taskfile wordt echt gedraaid, met een `kubectl` die opschrijft wat hij
binnenkrijgt. Een test op de TEKST van de taak zou niet zien dat het filter de verkeerde
documenten weglaat.
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

GERENDERD = """\
apiVersion: argoproj.io/v1alpha1
kind: AppProject
metadata:
  name: default
  namespace: rig-staging-operations
---
apiVersion: argoproj.io/v1alpha1
kind: AppProject
metadata:
  name: zad-platform
  namespace: rig-staging-operations
---
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: staging-infrastructure
  namespace: rig-staging-operations
spec:
  project: zad-platform
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
---
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: handmatig
  namespace: rig-staging-operations
spec:
  project: zad-platform
"""


@pytest.fixture(scope="module")
def taakshell() -> str:
    taskfile = yaml.safe_load(TASKFILE.read_text())
    # Alleen de shell-stappen: een `- task:`-stap is een dict en geen script.
    return "\n".join(cmd for cmd in taskfile["tasks"][TAAK]["cmds"] if isinstance(cmd, str))


def _render(shell: str, keep_default: str, pause_prune: str = "false") -> str:
    return (
        shell.replace("{{.CLUSTER}}", "odcn-staging")
        .replace("{{.DRY_RUN}}", "false")
        .replace("{{.KEY_FILE}}", "security/key.txt")
        .replace("{{.RIG_NAMESPACE}}", "rig-staging-operations")
        .replace("{{.KEEP_DEFAULT_APPPROJECT}}", keep_default)
        .replace("{{.PAUSE_PRUNE}}", pause_prune)
        .replace("{{.SECRETS_DIR}}", "")
    )


def _omgeving(tmp_path: Path) -> Path:
    overlay = tmp_path / "bootstrap/rig-system/kustomize/overlays/odcn-staging"
    overlay.mkdir(parents=True)
    (overlay / "kustomization.yaml").write_text("resources: []\n")

    (tmp_path / "security").mkdir()
    (tmp_path / "security/key.txt").write_text("# created\n# public key: age1test\nAGE-SECRET-KEY-1TEST\n")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "kubectl").write_text(
        "#!/bin/bash\n"
        'if [[ "$*" == apply*"-f -" ]]; then\n'
        '  cat >> "$APPLY_LOG"\n'
        "else\n"
        "  cat >/dev/null 2>&1 || true\n"
        "fi\n"
        "exit 0\n"
    )
    (bin_dir / "kustomize").write_text(f"#!/bin/bash\ncat <<'EOF'\n{GERENDERD}EOF\nexit 0\n")
    for stub in bin_dir.iterdir():
        stub.chmod(0o755)
    (bin_dir / "yq").symlink_to(echt_programma("yq"))
    return bin_dir


def _toegepast(shell: str, tmp_path: Path, bin_dir: Path) -> list[dict]:
    """De documenten die bij `kubectl apply -f -` zijn binnengekomen."""
    log = tmp_path / "apply.log"
    log.touch()
    bash = echt_programma("bash")
    proc = subprocess.run(
        [bash, "-c", shell],
        cwd=tmp_path,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path), "APPLY_LOG": str(log)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return [doc for doc in yaml.safe_load_all(log.read_text()) if doc]


@pytest.fixture(autouse=True)
def _gereedschap() -> None:
    if shutil.which("bash") is None:
        pytest.skip("bash ontbreekt")


def _namen(documenten: list[dict], kind: str) -> list[str]:
    return [doc["metadata"]["name"] for doc in documenten if doc.get("kind") == kind]


class TestKeepDefaultAppproject:
    def test_zonder_de_optie_gaat_de_dichte_default_mee(self, taakshell: str, tmp_path: Path) -> None:
        documenten = _toegepast(_render(taakshell, "false"), tmp_path, _omgeving(tmp_path))

        assert _namen(documenten, "AppProject") == ["default", "zad-platform"]

    def test_met_de_optie_blijft_default_ongemoeid(self, taakshell: str, tmp_path: Path) -> None:
        documenten = _toegepast(_render(taakshell, "true"), tmp_path, _omgeving(tmp_path))

        assert _namen(documenten, "AppProject") == ["zad-platform"]

    def test_met_de_optie_gaat_de_rest_van_de_overlay_wel_mee(self, taakshell: str, tmp_path: Path) -> None:
        """Het filter mag alleen de default weglaten, niet de Applications die erop stonden."""
        documenten = _toegepast(_render(taakshell, "true"), tmp_path, _omgeving(tmp_path))

        assert _namen(documenten, "Application") == ["staging-infrastructure", "handmatig"]


def _syncpolicy(documenten: list[dict], naam: str) -> dict | None:
    app = next(doc for doc in documenten if doc.get("kind") == "Application" and doc["metadata"]["name"] == naam)
    return app["spec"].get("syncPolicy")


class TestPausePrune:
    """Het vangnet uit #238: prune uit tijdens de uitrol, in de apply zelf.

    Met de hand uitzetten helpt niet: de apply zet `prune: true` uit git terug.
    """

    def test_zonder_de_optie_blijft_prune_zoals_in_git(self, taakshell: str, tmp_path: Path) -> None:
        documenten = _toegepast(_render(taakshell, "false"), tmp_path, _omgeving(tmp_path))

        assert _syncpolicy(documenten, "staging-infrastructure") == {"automated": {"prune": True, "selfHeal": True}}

    def test_met_de_optie_staat_prune_uit_en_selfheal_aan(self, taakshell: str, tmp_path: Path) -> None:
        documenten = _toegepast(_render(taakshell, "false", "true"), tmp_path, _omgeving(tmp_path))

        assert _syncpolicy(documenten, "staging-infrastructure") == {"automated": {"prune": False, "selfHeal": True}}

    def test_een_app_zonder_automatische_sync_krijgt_er_geen_bij(self, taakshell: str, tmp_path: Path) -> None:
        documenten = _toegepast(_render(taakshell, "false", "true"), tmp_path, _omgeving(tmp_path))

        assert _syncpolicy(documenten, "handmatig") is None

    def test_samen_met_keep_default(self, taakshell: str, tmp_path: Path) -> None:
        documenten = _toegepast(_render(taakshell, "true", "true"), tmp_path, _omgeving(tmp_path))

        assert _namen(documenten, "AppProject") == ["zad-platform"]
        assert _syncpolicy(documenten, "staging-infrastructure") == {"automated": {"prune": False, "selfHeal": True}}
