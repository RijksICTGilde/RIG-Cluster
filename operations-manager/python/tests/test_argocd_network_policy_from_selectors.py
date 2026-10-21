"""The `allow-argo` NetworkPolicy on the ODCN overlays must scope its server ports (RC-315).

Before this, the ingress rule on 8080/8443 carried no `from`, so any pod anywhere in the
cluster could reach the ArgoCD server. This pins that only OPI, the router namespace, and
Prometheus (for metrics) are allowed in, and that an arbitrary pod is not.

Both ODCN overlays are checked: staging got the same policy in RC-357.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_OVERLAYS = Path(__file__).resolve().parents[3] / "bootstrap" / "rig-system" / "kustomize" / "overlays"
_OVERLAY_NAMEN = ("odcn-production", "odcn-staging")


def _load_policy(overlay: str) -> dict:
    pad = _OVERLAYS / overlay / "network-policies" / "argocd-network-policy.yaml"
    return yaml.safe_load(pad.read_text())


def _rule_for_port(ingress: list[dict], port: int) -> dict:
    for rule in ingress:
        if any(p.get("port") == port for p in rule.get("ports", [])):
            return rule
    raise AssertionError(f"no ingress rule carries port {port}")


@pytest.mark.parametrize("overlay", _OVERLAY_NAMEN)
def test_server_port_rule_restricts_to_opi_and_router(overlay: str) -> None:
    ingress = _load_policy(overlay)["spec"]["ingress"]
    rule = _rule_for_port(ingress, 8080)

    peers = rule["from"]
    assert {"podSelector": {"matchLabels": {"app": "operations-manager"}}} in peers
    assert {"namespaceSelector": {"matchLabels": {"network.openshift.io/policy-group": "ingress"}}} in peers
    assert len(peers) == 2, "an extra peer here is a widened allow-list that nobody asked for"


@pytest.mark.parametrize("overlay", _OVERLAY_NAMEN)
def test_server_port_rule_does_not_allow_an_arbitrary_pod(overlay: str) -> None:
    """A pod with neither label matches neither podSelector, so it stays outside this rule."""
    ingress = _load_policy(overlay)["spec"]["ingress"]
    rule = _rule_for_port(ingress, 8080)

    arbitrary_pod_labels = {"app": "some-other-workload"}
    for peer in rule["from"]:
        pod_selector = peer.get("podSelector")
        if pod_selector is not None:
            assert not pod_selector.get("matchLabels", {}).items() <= arbitrary_pod_labels.items()


@pytest.mark.parametrize("overlay", _OVERLAY_NAMEN)
def test_metrics_port_is_restricted_to_prometheus(overlay: str) -> None:
    ingress = _load_policy(overlay)["spec"]["ingress"]
    rule = _rule_for_port(ingress, 8083)

    assert rule["from"] == [{"podSelector": {"matchLabels": {"app": "prometheus"}}}]


@pytest.mark.parametrize("overlay", _OVERLAY_NAMEN)
def test_8443_no_longer_carries_a_blanket_allow(overlay: str) -> None:
    """8443 used to sit next to 8080 under the same unscoped rule. The operator Service points
    both 80 and 443 at targetPort 8080, so nothing ever listened on 8443."""
    ingress = _load_policy(overlay)["spec"]["ingress"]
    assert not any(p.get("port") == 8443 for rule in ingress for p in rule.get("ports", []))
