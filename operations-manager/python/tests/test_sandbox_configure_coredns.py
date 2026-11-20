"""RC-513: sandbox:configure-coredns rewrites to the fixed entrypoint zad-ingang.

The target no longer depends on the routing stand: every stand brings an ExternalName Service
``zad-ingang`` pointing at its own controller, so this rewrite is written once and never
changes again. A cluster that still carries the old rule, straight at a controller Service,
must be moved over instead of keeping or duplicating it.

A previous version of the idempotency check (``grep "name regex.*\\.sandbox\\.rijksapp\\.dev"``)
never matched: the Corefile carries real backslashes in that regex, so every run added a
second ``rewrite stop`` block after ``ready``. This file measures the three branches the fixed
task must take: add when there is no rule yet, do nothing when the rule already points at the
target, and replace when it points at the other stand's target. The Taskfile is run for real,
with a stub kubectl that returns a canned Corefile and records the patch it would apply.

See docs/sandbox/routing-wisselen.md.
"""

from __future__ import annotations

import json
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
INGANG_TARGET = "zad-ingang.rig-system.svc.cluster.local"
#: The rule a sandbox from before RC-513 carries: straight at the nginx controller.
FORMER_TARGET = "ingress-nginx-controller.ingress-nginx.svc.cluster.local"

COREFILE_WITHOUT_RULE = """.:53 {
    errors
    health {
       lameduck 5s
    }
    ready
    kubernetes cluster.local in-addr.arpa ip6.arpa {
       pods insecure
       fallthrough in-addr.arpa ip6.arpa
    }
    forward . /etc/resolv.conf
    cache 30
    loop
    reload
    loadbalance
}
"""


def _corefile_with_rule(target: str) -> str:
    return f""".:53 {{
    errors
    health {{
       lameduck 5s
    }}
    ready
    kubernetes cluster.local in-addr.arpa ip6.arpa {{
       pods insecure
       fallthrough in-addr.arpa ip6.arpa
    }}
    rewrite stop {{
        name regex (.+)\\.sandbox\\.rijksapp\\.dev {target}
        answer auto
    }}
    forward . /etc/resolv.conf
    cache 30
    loop
    reload
    loadbalance
}}
"""


KUBECTL_STUB = """#!/usr/bin/env bash
echo "kubectl $*" >> "$STUB_LOG"
case "$1 $2" in
  "get configmap")
    echo "$STUB_COREFILE"
    ;;
  "patch configmap")
    previous=""
    for arg in "$@"; do
      if [ "$previous" = "--patch-file" ]; then
        cp "$arg" "$STUB_PATCH_LOG"
      fi
      previous="$arg"
    done
    ;;
esac
exit 0
"""


@pytest.fixture
def repo_copy(tmp_path: Path) -> Path:
    """A git repo with the Taskfile and the scripts its global vars read.

    SANDBOX_OM_TAG is a global var that needs a repo before any task even reaches its own
    body.
    """
    if shutil.which("bash") is None or shutil.which("git") is None:
        pytest.skip("bash or git is missing")
    shutil.copy(TASKFILE, tmp_path / "Taskfile.yaml")
    (tmp_path / "scripts").mkdir()
    for script in (SOURCE_TAG_SCRIPT, CALVER_TAG_SCRIPT):
        shutil.copy(script, tmp_path / "scripts" / script.name)
    for args in (("init", "-q"), ("add", "."), ("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    (stubs / "kubectl").write_text(KUBECTL_STUB)
    (stubs / "kubectl").chmod(0o755)
    return tmp_path


def _configure_coredns(repo_copy: Path, *, corefile: str) -> subprocess.CompletedProcess[str]:
    """Run sandbox:configure-coredns for real, against a stub kubectl."""
    env = dict(os.environ)
    env["PATH"] = f"{repo_copy / 'stubs'}{os.pathsep}{env['PATH']}"
    env["STUB_LOG"] = str(repo_copy / "stub.log")
    env["STUB_PATCH_LOG"] = str(repo_copy / "patch.json")
    env["STUB_COREFILE"] = corefile
    (repo_copy / "stub.log").write_text("")
    return subprocess.run(
        [echt_programma("task"), "sandbox:configure-coredns"],
        cwd=repo_copy,
        env=env,
        capture_output=True,
        text=True,
    )


def _patched_corefile(repo_copy: Path) -> str:
    patch = json.loads((repo_copy / "patch.json").read_text())
    return patch["data"]["Corefile"]


def test_without_existing_rule_the_rewrite_block_is_added(repo_copy: Path) -> None:
    result = _configure_coredns(repo_copy, corefile=COREFILE_WITHOUT_RULE)

    assert result.returncode == 0, result.stderr + result.stdout
    corefile = _patched_corefile(repo_copy)
    assert corefile.count("rewrite stop") == 1
    assert f"name regex (.+)\\.sandbox\\.rijksapp\\.dev {INGANG_TARGET}" in corefile


def test_a_rule_to_the_fixed_entrypoint_is_idempotent(repo_copy: Path) -> None:
    """No kubectl patch at all: the previous version of this check never matched and so added
    a second rewrite block on every run."""
    result = _configure_coredns(repo_copy, corefile=_corefile_with_rule(INGANG_TARGET))

    assert result.returncode == 0, result.stderr + result.stdout
    assert "already points at" in result.stdout
    assert not (repo_copy / "patch.json").exists()


def test_an_old_rule_straight_at_a_controller_is_replaced(repo_copy: Path) -> None:
    """What an existing sandbox carries: the rewrite must move to zad-ingang."""
    result = _configure_coredns(repo_copy, corefile=_corefile_with_rule(FORMER_TARGET))

    assert result.returncode == 0, result.stderr + result.stdout
    corefile = _patched_corefile(repo_copy)
    assert corefile.count("rewrite stop") == 1
    assert INGANG_TARGET in corefile
    assert FORMER_TARGET not in corefile
