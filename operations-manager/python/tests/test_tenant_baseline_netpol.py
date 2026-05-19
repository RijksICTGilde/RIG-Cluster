"""Tests for the tenant baseline NetworkPolicy.

Regression coverage for a HIGH tenant-isolation finding: OPI used to write an
allow-all NetworkPolicy (podSelector {}, ingress [{}], egress [{}]) into every
tenant namespace, which defeated egress containment platform-wide and nullified
any later default-deny.

The replacement is a permissive-by-default baseline that nonetheless blocks
cross-tenant traffic: it allows the deployment's own pods to talk to each
other, the platform/operations namespace, the backup destination, and the
ingress controller, plus internet egress (without restriction beyond blocking
the cloud-metadata IP). It is emitted ONCE per deployment, selecting pods via
the `deployment: <name>` label so multiple components share one policy and
helm/helmfile workloads (which do not carry that label) fall through to
Kubernetes' default-allow until a separate baseline is added.

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
        "name": "myapp-tenant-baseline-network-policy",
        "namespace": "rig-myproject",
        "deployment_selector": "myapp",
        "ops_namespace": "rig-system",
        "backup_namespace": "rig-backup-destination",
        "ingress_controller_namespace": "ingress-nginx",
        "project_infra_namespace": "rig-myproject-infrastructure",
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
            "both call sites (infrastructure namespace + per-deployment) must emit the baseline template"
        )

    def test_per_component_emission_is_removed(self):
        """The per-component emission caused file collisions (last write wins).
        The baseline must now be emitted ONCE per deployment, not per component.
        """
        with open(PROJECT_MANAGER) as f:
            source = f.read()
        # The old branch-on-manifest-name is gone.
        assert 'if manifest_name == "tenant-baseline-network-policy":' not in source

    def test_deployment_baseline_uses_per_deployment_name(self):
        """The emitted resource/file name must be prefixed with the deployment
        so two deployments in the same namespace don't collide on disk or in
        the cluster.
        """
        with open(PROJECT_MANAGER) as f:
            source = f.read()
        assert 'f"{deployment_name}-tenant-baseline"' in source, (
            "per-deployment baseline emission must namespace its resource name with the deployment"
        )


class TestPerDeploymentBaseline:
    def test_renders_valid_yaml_networkpolicy(self):
        _, doc = _render_baseline()
        assert doc["kind"] == "NetworkPolicy"
        assert doc["metadata"]["name"] == "myapp-tenant-baseline-network-policy"
        assert doc["metadata"]["namespace"] == "rig-myproject"
        assert set(doc["spec"]["policyTypes"]) == {"Ingress", "Egress"}

    def test_selector_targets_only_this_deployment(self):
        """Per-deployment scope: select pods carrying `deployment: <name>`.
        Other deployments in the same namespace and helm/helmfile pods (no
        such label) are intentionally not selected.
        """
        _, doc = _render_baseline(deployment_selector="myapp")
        sel = doc["spec"]["podSelector"]
        assert sel == {"matchLabels": {"deployment": "myapp"}}

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

    def test_intra_deployment_ingress_uses_label_selector(self):
        """Pods of the same deployment talk to each other via the deployment
        label, not via a namespace-wide podSelector {} that would also expose
        unrelated deployments sharing the namespace.
        """
        _, doc = _render_baseline(deployment_selector="myapp")
        ingress = doc["spec"]["ingress"]
        assert any(
            any(peer == {"podSelector": {"matchLabels": {"deployment": "myapp"}}} for peer in rule.get("from", []))
            for rule in ingress
        )

    def test_platform_namespaces_are_allow_listed(self):
        """Operations (OPI / shared datastores / Keycloak), backup, and the
        ingress controller must always be reachable from a deployment pod.
        Cluster-specific names flow in via cluster_config.
        """
        _, doc = _render_baseline(
            ops_namespace="rig-prd-operations",
            backup_namespace="rig-prd-backup",
            ingress_controller_namespace="openshift-ingress",
        )
        ingress_targets = {
            peer["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"]
            for rule in doc["spec"]["ingress"]
            for peer in rule.get("from", [])
            if peer.get("namespaceSelector", {}).get("matchLabels")
        }
        egress_targets = {
            peer["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"]
            for rule in doc["spec"]["egress"]
            for peer in rule.get("to", [])
            if peer.get("namespaceSelector", {}).get("matchLabels")
        }
        assert {"rig-prd-operations", "openshift-ingress", "rig-prd-backup"} <= ingress_targets
        assert {"rig-prd-operations", "rig-prd-backup"} <= egress_targets

    def test_internet_egress_is_permissive_except_metadata(self):
        """The baseline does not constrain internet egress (HTTP/HTTPS) on
        purpose — tightening that is a separate, later step. The cloud-metadata
        IP (169.254.169.254) is excluded to block SSRF-style probes.
        """
        _, doc = _render_baseline()
        ip_rules = [
            rule for rule in doc["spec"]["egress"] if any(peer.get("ipBlock") for peer in rule.get("to", []) or [])
        ]
        assert ip_rules, "must allow generic internet egress for HTTP/HTTPS"
        flat_ports = {p["port"] for rule in ip_rules for p in rule.get("ports", [])}
        assert 443 in flat_ports
        assert 80 in flat_ports
        excepts = {x for rule in ip_rules for peer in rule["to"] for x in peer.get("ipBlock", {}).get("except", [])}
        assert "169.254.169.254/32" in excepts, "cloud-metadata IP must be excluded from the internet egress rule"

    def test_project_infra_namespace_included_when_distinct(self):
        """When the project has its own infrastructure namespace (dedicated
        CNPG), it gets an explicit egress allow-list entry."""
        _, doc = _render_baseline(project_infra_namespace="rig-myproject-infrastructure")
        egress_targets = {
            peer["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"]
            for rule in doc["spec"]["egress"]
            for peer in rule.get("to", [])
            if peer.get("namespaceSelector", {}).get("matchLabels")
        }
        assert "rig-myproject-infrastructure" in egress_targets

    def test_project_infra_namespace_skipped_when_equals_ops(self):
        """Avoid emitting a redundant duplicate rule when the project's infra
        namespace would equal the ops namespace (e.g. shared-only projects).
        """
        _, doc = _render_baseline(
            ops_namespace="rig-prd-operations",
            project_infra_namespace="rig-prd-operations",
        )
        egress_targets = [
            peer["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"]
            for rule in doc["spec"]["egress"]
            for peer in rule.get("to", [])
            if peer.get("namespaceSelector", {}).get("matchLabels")
        ]
        # Each platform namespace appears exactly once.
        assert egress_targets.count("rig-prd-operations") == 1


class TestInfrastructureNamespaceVariant:
    """The infrastructure-namespace call site renders the same template but
    without a deployment selector (it covers the CNPG operator + cluster pods).
    """

    def test_renders_with_namespace_wide_selector(self):
        _, doc = _render_baseline(
            deployment_selector=None,
            allowed_ingress_namespaces=["rig-myproject"],
        )
        # No matchLabels: applies to the whole namespace.
        assert doc["spec"]["podSelector"] == {}

    def test_allowed_ingress_namespaces_are_emitted(self):
        _, doc = _render_baseline(
            deployment_selector=None,
            allowed_ingress_namespaces=["rig-myproject", "rig-myproject-staging"],
        )
        ingress_targets = {
            peer["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"]
            for rule in doc["spec"]["ingress"]
            for peer in rule.get("from", [])
            if peer.get("namespaceSelector", {}).get("matchLabels")
        }
        assert {"rig-myproject", "rig-myproject-staging"} <= ingress_targets


class TestDeploymentLabelOnPods:
    """Pods need the `deployment: <name>` label or the per-deployment selector
    selects nothing.
    """

    def test_deployment_template_carries_deployment_label(self):
        with open(os.path.join(MANIFESTS_DIR, "deployment.yaml.jinja")) as f:
            source = f.read()
        assert 'deployment: "{{ deployment_name }}"' in source, (
            "pod template metadata must include a `deployment` label so the NetworkPolicy selector matches"
        )
