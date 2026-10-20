"""The `allow-argo` NetworkPolicy on odcn-production must scope its server ports (RC-315).

Before this, the ingress rule on 8080/8443 carried no `from`, so any pod anywhere in the
cluster could reach the ArgoCD server. This pins that only OPI, the router namespace, and
Prometheus (for metrics) are allowed in, and that an arbitrary pod is not.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_POLICY_PATH = (
    Path(__file__).resolve().parents[3]
    / "bootstrap"
    / "rig-system"
    / "kustomize"
    / "overlays"
    / "odcn-production"
    / "network-policies"
    / "argocd-network-policy.yaml"
)


def _load_policy() -> dict:
    return yaml.safe_load(_POLICY_PATH.read_text())


def _rule_for_port(ingress: list[dict], port: int) -> dict:
    for rule in ingress:
        if any(p.get("port") == port for p in rule.get("ports", [])):
            return rule
    raise AssertionError(f"no ingress rule carries port {port}")


def test_server_port_rule_restricts_to_opi_and_router() -> None:
    ingress = _load_policy()["spec"]["ingress"]
    rule = _rule_for_port(ingress, 8080)

    peers = rule["from"]
    assert {"podSelector": {"matchLabels": {"app": "operations-manager"}}} in peers
    assert {"namespaceSelector": {"matchLabels": {"network.openshift.io/policy-group": "ingress"}}} in peers
    assert len(peers) == 2, "an extra peer here is a widened allow-list that nobody asked for"


def test_server_port_rule_does_not_allow_an_arbitrary_pod() -> None:
    """A pod with neither label matches neither podSelector, so it stays outside this rule."""
    ingress = _load_policy()["spec"]["ingress"]
    rule = _rule_for_port(ingress, 8080)

    arbitrary_pod_labels = {"app": "some-other-workload"}
    for peer in rule["from"]:
        pod_selector = peer.get("podSelector")
        if pod_selector is not None:
            assert not pod_selector.get("matchLabels", {}).items() <= arbitrary_pod_labels.items()


def test_metrics_port_is_restricted_to_prometheus() -> None:
    ingress = _load_policy()["spec"]["ingress"]
    rule = _rule_for_port(ingress, 8083)

    assert rule["from"] == [{"podSelector": {"matchLabels": {"app": "prometheus"}}}]


def test_8443_no_longer_carries_a_blanket_allow() -> None:
    """8443 used to sit next to 8080 under the same unscoped rule. The operator Service points
    both 80 and 443 at targetPort 8080, so nothing ever listened on 8443."""
    ingress = _load_policy()["spec"]["ingress"]
    assert not any(p.get("port") == 8443 for rule in ingress for p in rule.get("ports", []))
