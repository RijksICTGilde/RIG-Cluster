"""RC-510: the routing stand of the sandbox is named in three places, and they must agree.

The sandbox serves its hostnames either through ingress-nginx or through the shared Gateway
of Envoy Gateway. Three lines pick that stand: a kustomize component in the infrastructure
layer, one in the bootstrap layer, and the ``routing`` key in ``CLUSTER_CONFIG``. A mismatch
means the manifests OPI renders do not match the ingress the cluster has, and nothing else
catches that. See ``docs/sandbox/routing-wisselen.md``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from opi.core.cluster_config import CLUSTER_CONFIG

REPO_ROOT = Path(__file__).resolve().parents[3]

# The default from the contract with the OPI side of the switch: as long as that key does
# not exist, the sandbox is on ingress.
DEFAULT_STAND = "ingress"

KUSTOMIZATIONS = {
    "infrastructure": (
        REPO_ROOT / "infrastructure/bootstrap/clusters/sandboxed-local/kustomization.yaml",
        re.compile(r"^\s*-\s*\.\./\.\./infrastructure/routing/(ingress|gateway)\s*$", re.MULTILINE),
    ),
    "mailsink": (
        REPO_ROOT
        / "infrastructure/bootstrap/infrastructure/mail/controller/overlays/sandboxed-local/kustomization.yaml",
        re.compile(r"^\s*-\s*(?:\.\./){4}routing/mailsink-(ingress|gateway)\s*$", re.MULTILINE),
    ),
    "bootstrap": (
        REPO_ROOT / "bootstrap/rig-system/kustomize/overlays/sandboxed-local/kustomization.yaml",
        re.compile(r"^\s*-\s*\.\./\.\./components/sandbox-routing/(ingress|gateway)\s*$", re.MULTILINE),
    ),
}


def stand_in_cluster_config() -> str:
    return CLUSTER_CONFIG["sandboxed-local"]["ingress"].get("routing", DEFAULT_STAND)


@pytest.mark.parametrize("layer", sorted(KUSTOMIZATIONS))
def test_every_layer_names_exactly_one_stand(layer: str) -> None:
    path, pattern = KUSTOMIZATIONS[layer]
    found = pattern.findall(path.read_text())

    assert len(found) == 1, f"{path.relative_to(REPO_ROOT)} must reference exactly one routing component, found {found}"


@pytest.mark.parametrize("layer", sorted(KUSTOMIZATIONS))
def test_the_layer_agrees_with_the_cluster_config(layer: str) -> None:
    path, pattern = KUSTOMIZATIONS[layer]
    (found,) = pattern.findall(path.read_text())

    assert found == stand_in_cluster_config(), (
        f"{path.relative_to(REPO_ROOT)} is on '{found}' while CLUSTER_CONFIG"
        f"['sandboxed-local']['ingress']['routing'] says '{stand_in_cluster_config()}'"
    )


def test_the_stand_is_one_of_the_two_known_values() -> None:
    assert stand_in_cluster_config() in ("ingress", "gateway")
