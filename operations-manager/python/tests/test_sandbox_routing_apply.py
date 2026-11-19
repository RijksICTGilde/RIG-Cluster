"""RC-510: install-routing-controller dispatches to the controller of the routing stand in
git, and sandbox:routing-apply lists its steps in the documented order.

The dispatch and the precondition guard (``routing_stand_guard``) are read by three tasks
(install-routing-controller, sandbox:configure-coredns, sandbox:routing-apply, plus
sandbox:import-wildcard-cert for the gateway early exit) through the YAML anchor/alias in
Taskfile.yaml, so a change to that guard or to SANDBOX_ROUTING shows up here without a real
cluster: ``task --dry --verbose`` prints which subtask starts and in what order, without
running any command.

See docs/sandbox/routing-wisselen.md.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from tests.programma import echt_programma

REPO_ROOT = Path(__file__).resolve().parents[3]
TASKFILE = REPO_ROOT / "Taskfile.yaml"
SOURCE_TAG_SCRIPT = REPO_ROOT / "scripts/bron-tag.sh"
CALVER_TAG_SCRIPT = REPO_ROOT / "scripts/calver-tag.sh"
SANDBOX_OVERLAY_KUSTOMIZATION = Path("bootstrap/rig-system/kustomize/overlays/sandboxed-local/kustomization.yaml")


@pytest.fixture
def repo_copy(tmp_path: Path) -> Path:
    """A git repo with the Taskfile and the sandbox overlay, so SANDBOX_OM_TAG and
    SANDBOX_ROUTING read as global vars instead of failing the whole run before it starts."""
    if shutil.which("bash") is None or shutil.which("git") is None:
        pytest.skip("bash or git is missing")
    shutil.copy(TASKFILE, tmp_path / "Taskfile.yaml")
    (tmp_path / "scripts").mkdir()
    for script in (SOURCE_TAG_SCRIPT, CALVER_TAG_SCRIPT):
        shutil.copy(script, tmp_path / "scripts" / script.name)
    (tmp_path / SANDBOX_OVERLAY_KUSTOMIZATION.parent).mkdir(parents=True)
    shutil.copy(REPO_ROOT / SANDBOX_OVERLAY_KUSTOMIZATION, tmp_path / SANDBOX_OVERLAY_KUSTOMIZATION)
    shutil.copy(REPO_ROOT / ".env-taskfile-sandboxed-local", tmp_path / ".env-taskfile-sandboxed-local")
    for args in (("init", "-q"), ("add", "."), ("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    return tmp_path


def _set_stand(repo_copy: Path, stand: str) -> None:
    kustomization = repo_copy / SANDBOX_OVERLAY_KUSTOMIZATION
    text = kustomization.read_text()
    for other in ("ingress", "gateway"):
        text = text.replace(f"sandbox-routing/{other}", f"sandbox-routing/{stand}")
    kustomization.write_text(text)


def _dry_verbose(repo_copy: Path, *args: str, cluster_type: str | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PATH"] = f"/bin:/usr/bin:{env['PATH']}"
    if cluster_type:
        env["CLUSTER_TYPE"] = cluster_type
    return subprocess.run(
        [echt_programma("task"), "--dry", "--verbose", *args], cwd=repo_copy, env=env, capture_output=True, text=True
    )


@pytest.mark.parametrize(
    ("stand", "expected_subtask"), [("ingress", "install-ingress-nginx"), ("gateway", "install-envoy-gateway")]
)
def test_install_routing_controller_dispatches_to_the_stand_in_git(
    repo_copy: Path, stand: str, expected_subtask: str
) -> None:
    _set_stand(repo_copy, stand)

    result = _dry_verbose(repo_copy, "install-routing-controller")
    output = result.stdout + result.stderr

    assert result.returncode == 0, result.stderr
    assert f'"{expected_subtask}" started' in output
    other_subtask = "install-envoy-gateway" if stand == "ingress" else "install-ingress-nginx"
    assert f'"{other_subtask}" started' not in output


def test_the_stand_comes_from_the_component_line_and_not_from_a_comment(repo_copy: Path) -> None:
    """SANDBOX_ROUTING reads the line that kustomize reads, so a mention of the other stand
    in the comment above it must not move the stand."""
    _set_stand(repo_copy, "ingress")
    kustomization = repo_copy / SANDBOX_OVERLAY_KUSTOMIZATION
    kustomization.write_text(
        kustomization.read_text().replace(
            "components:", "components:\n# sandbox-routing/gateway staat hier alleen in het commentaar"
        )
    )

    result = _dry_verbose(repo_copy, "install-routing-controller")
    output = result.stdout + result.stderr

    assert result.returncode == 0, result.stderr
    assert '"install-ingress-nginx" started' in output
    assert '"install-envoy-gateway" started' not in output


def test_install_routing_controller_refuses_without_a_recognisable_stand(repo_copy: Path) -> None:
    """Neither of the two components is in the kustomization: the guard must stop this
    instead of picking one of the two controllers on an empty SANDBOX_ROUTING."""
    _set_stand(repo_copy, "ingress")
    kustomization = repo_copy / SANDBOX_OVERLAY_KUSTOMIZATION
    kustomization.write_text(kustomization.read_text().replace("sandbox-routing/ingress", "sandbox-routing/other"))

    result = _dry_verbose(repo_copy, "install-routing-controller")

    assert result.returncode != 0
    assert "routing-stand" in result.stdout + result.stderr


@pytest.mark.parametrize("stand", ["ingress", "gateway"])
def test_routing_apply_lists_its_steps_in_the_documented_order(repo_copy: Path, stand: str) -> None:
    """The documented steps from docs/sandbox/routing-wisselen.md, in that order: the new
    ingress first and only then the old one removed (step 2 before step 3), and
    configure-coredns after the sync."""
    _set_stand(repo_copy, stand)

    result = _dry_verbose(repo_copy, "sandbox:routing-apply", cluster_type="sandboxed-local")
    output = result.stdout + result.stderr

    assert result.returncode == 0, output
    order = [
        "install-routing-controller",
        "bootstrap-argo-system",
        "sandbox:sync",
        "sandbox:configure-coredns",
    ]
    positions = [output.index(f'"{task}" started') for task in order]
    assert positions == sorted(positions), f"expected order {order} does not hold: {output}"
