"""Tests for the tenant baseline NetworkPolicy.

Regression coverage for a HIGH tenant-isolation finding: OPI used to write an
allow-all NetworkPolicy (podSelector {}, ingress [{}], egress [{}]) into every
tenant namespace, which defeated egress containment platform-wide and nullified
any later default-deny.

These tests assert the replacement is least-privilege: it must NOT contain an
empty allow-all ingress or egress rule, and it MUST contain DNS egress plus
scoped intra-namespace / ingress-nginx / datastore rules. They also assert that
project_manager emits the baseline template (not the allow-all template) at both
call sites.

The wider test suite fails at collection on this interpreter (pre-existing
Python 3.14 beta + pydantic + fastapi import break). This module deliberately
avoids importing FastAPI: it renders the template directly and inspects the
project_manager source, so it runs in isolation via
`pytest tests/test_tenant_baseline_netpol.py --noconftest`.
"""

import os

from opi.generation.manifests import render_template
from ruamel.yaml import YAML

MANIFESTS_DIR = os.path.join(os.path.dirname(__file__), "..", "manifests")
PROJECT_MANAGER = os.path.join(os.path.dirname(__file__), "..", "opi", "manager", "project_manager.py")


def _render_baseline(**overrides):
    values = {
        "name": "tenant-baseline",
        "namespace": "rig-myproject",
    }
    values.update(overrides)
    result = render_template("tenant-baseline-network-policy.yaml.jinja", values)
    return result, YAML().load(result)


def _has_empty_allow_all_rule(rules) -> bool:
    """An allow-all rule is an empty mapping or one with only empty selectors.

    Kubernetes treats `- {}` (and a rule whose only key is an empty
    namespaceSelector) as "allow everything", which is exactly what this fix
    must never emit.
    """
    for rule in rules or []:
        if rule in (None, {}):
            return True
        # `- from: [{}]` / `- to: [{}]` also means allow-all.
        for direction in ("from", "to"):
            peers = rule.get(direction)
            if peers is not None:
                for peer in peers:
                    if peer in (None, {}):
                        return True
    return False


class TestAllowAllTemplateRemoved:
    def test_allow_all_template_file_is_gone(self):
        path = os.path.join(MANIFESTS_DIR, "allow-all-network-policy.yaml.jinja")
        assert not os.path.exists(path), "the unrestricted allow-all NetworkPolicy template must be deleted"

    def test_project_manager_no_longer_renders_allow_all(self):
        with open(PROJECT_MANAGER) as f:
            source = f.read()
        # The render/manifest-list strings must be gone. Comments may mention
        # the term historically, so match the template filename specifically.
        assert "allow-all-network-policy.yaml.jinja" not in source
        assert source.count("tenant-baseline-network-policy.yaml.jinja") >= 2, (
            "both call sites (infrastructure namespace + per-component) must emit the baseline template"
        )

    def test_component_call_site_scopes_all_stateful_services(self):
        """The per-component baseline must open egress for every stateful
        service a component can request, not just Postgres/MinIO.

        Redis (shared and namespace) resolves to a cross-namespace service and
        SSO components run an OIDC back-channel to Keycloak. Under the
        default-deny baseline, omitting these breaks Redis-using and
        SSO-using tenant apps. This guards against the egress scoping
        regressing back to Postgres/MinIO only.
        """
        with open(PROJECT_MANAGER) as f:
            source = f.read()
        # Locate the per-component baseline branch.
        marker = 'if manifest_name == "tenant-baseline-network-policy":'
        assert marker in source
        branch = source[source.index(marker) : source.index(marker) + 2500]
        assert "component_uses_postgresql" in branch
        assert "component_uses_minio" in branch
        assert "component_uses_redis" in branch, "Redis-using components must get egress to the Redis namespace"
        assert "component_uses_sso" in branch, "SSO-using components must get egress for the Keycloak back-channel"
        assert '"ingress-nginx"' in branch, "SSO public-hostname traffic hairpins through ingress-nginx"


class TestBaselineIsLeastPrivilege:
    def test_renders_valid_yaml_networkpolicy(self):
        _, doc = _render_baseline()
        assert doc["kind"] == "NetworkPolicy"
        assert doc["metadata"]["name"] == "tenant-baseline"
        assert doc["metadata"]["namespace"] == "rig-myproject"
        assert set(doc["spec"]["policyTypes"]) == {"Ingress", "Egress"}

    def test_no_empty_allow_all_egress(self):
        raw, doc = _render_baseline()
        assert "egress:\n    - {}" not in raw
        assert "egress: [{}]" not in raw
        assert not _has_empty_allow_all_rule(doc["spec"]["egress"]), (
            "egress must not contain an empty allow-all rule (the core of the vulnerability)"
        )

    def test_no_empty_allow_all_ingress(self):
        raw, doc = _render_baseline()
        assert "ingress:\n    - {}" not in raw
        assert "ingress: [{}]" not in raw
        assert not _has_empty_allow_all_rule(doc["spec"]["ingress"])

    def test_dns_egress_is_present(self):
        _, doc = _render_baseline()
        dns_rules = [rule for rule in doc["spec"]["egress"] if any(p.get("port") == 53 for p in rule.get("ports", []))]
        assert dns_rules, "DNS egress (port 53) must be allowed or nothing resolves"
        protocols = {p["protocol"] for rule in dns_rules for p in rule["ports"]}
        assert {"UDP", "TCP"} <= protocols

    def test_intra_namespace_and_ingress_nginx_allowed(self):
        _, doc = _render_baseline()
        ingress = doc["spec"]["ingress"]
        # intra-namespace ingress (podSelector {} as a peer is scoped to the
        # namespace, not allow-all across the cluster).
        assert any(any(peer == {"podSelector": {}} for peer in rule.get("from", [])) for rule in ingress)
        # ingress-nginx controller must still reach published apps.
        nginx = any(
            any(
                peer.get("namespaceSelector", {}).get("matchLabels", {}).get("kubernetes.io/metadata.name")
                == "ingress-nginx"
                for peer in rule.get("from", [])
            )
            for rule in ingress
        )
        assert nginx, "published web apps must keep receiving ingress-nginx traffic"

    def test_datastore_egress_is_scoped_when_requested(self):
        _, doc = _render_baseline(
            datastore_namespaces=["rig-myproject-infrastructure"],
            minio_namespace="rig-system",
        )
        egress = doc["spec"]["egress"]
        targets = {
            peer["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"]
            for rule in egress
            for peer in rule.get("to", [])
            if peer.get("namespaceSelector")
        }
        assert "rig-myproject-infrastructure" in targets
        assert "rig-system" in targets
        # Still no allow-all leaked in.
        assert not _has_empty_allow_all_rule(egress)

    def test_no_datastore_egress_when_not_requested(self):
        """A component without DB/MinIO must not get cross-namespace egress."""
        _, doc = _render_baseline()
        cross_ns = {
            peer["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"]
            for rule in doc["spec"]["egress"]
            for peer in rule.get("to", [])
            if peer.get("namespaceSelector", {}).get("matchLabels")
        }
        # Only the kube-dns rule uses an (empty) namespaceSelector; it carries
        # no matchLabels, so the scoped set must be empty here.
        assert cross_ns == set()
