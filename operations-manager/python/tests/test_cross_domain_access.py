"""Unit tests for the cross-domain-access service (RC-15).

Covers the two pure cores -- the layer merge (``merge.py``) and the peer resolution
(``resolve.py``) -- plus the config model's shape guarantees. Manifest-template, prune and
API tests live alongside the templates / router they exercise.
"""

from __future__ import annotations

import pytest
from opi.core.project_schema import ProjectIntegrityError
from opi.manager.project_validation import validate_service_configs
from opi.services.catalog.cross_domain_access.config_model import CrossDomainAccessConfig
from opi.services.catalog.cross_domain_access.merge import (
    IncompleteRuleError,
    MergedRule,
    merge_rules,
    to_merged_rule,
)
from opi.services.catalog.cross_domain_access.resolve import resolve_rules
from opi.services.services_enums import ServiceType
from pydantic import ValidationError

# --- config model -----------------------------------------------------------------------


class TestConfigModel:
    def test_stored_root_rule_may_leave_peer_deployment_open(self) -> None:
        # The stored (patch) model is lenient: a root outbound rule without a peer deployment
        # is valid at rest; completeness is judged at merge time.
        config = CrossDomainAccessConfig.model_validate(
            {
                "outbound": [
                    {
                        "name": "naar-x",
                        "from": {"component": "web"},
                        "to": {"project": "x", "component": "api", "port": 8080},
                    }
                ]
            }
        )
        assert config.outbound[0].to.deployment is None

    def test_unknown_field_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CrossDomainAccessConfig.model_validate({"inbound": [{"name": "r", "bogus": 1}]})

    def test_project_on_own_side_is_rejected(self) -> None:
        # 'to' of an inbound rule is my side (LocalTarget): it has no 'project'.
        with pytest.raises(ValidationError):
            CrossDomainAccessConfig.model_validate(
                {
                    "inbound": [
                        {
                            "name": "r",
                            "from": {"project": "x", "deployment": "prod", "component": "api"},
                            "to": {"project": "me", "component": "web", "port": 8080},
                        }
                    ]
                }
            )

    def test_port_out_of_range_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CrossDomainAccessConfig.model_validate(
                {
                    "inbound": [
                        {
                            "name": "r",
                            "from": {"project": "x", "deployment": "prod", "component": "api"},
                            "to": {"component": "web", "port": 99999},
                        }
                    ]
                }
            )

    def test_duplicate_name_in_direction_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CrossDomainAccessConfig.model_validate(
                {"outbound": [{"name": "dup", "to": {"deployment": "a"}}, {"name": "dup", "to": {"deployment": "b"}}]}
            )


# --- merge ------------------------------------------------------------------------------


def _inbound(name: str, project: str, deployment: str | None, component: str, local: str, port: int) -> dict:
    peer: dict = {"project": project, "component": component}
    if deployment is not None:
        peer["deployment"] = deployment
    return {"name": name, "from": peer, "to": {"component": local, "port": port}}


def _outbound(name: str, local: str, project: str, deployment: str | None, component: str, port: int) -> dict:
    peer: dict = {"project": project, "component": component, "port": port}
    if deployment is not None:
        peer["deployment"] = deployment
    return {"name": name, "from": {"component": local}, "to": peer}


class TestMerge:
    def test_patch_only_peer_deployment_inherits_the_rest(self) -> None:
        root = [_outbound("naar-api", "web", "regelrecht", "prod", "api", 8080)]
        deployment = [{"name": "naar-api", "to": {"deployment": "dev"}}]
        merged = merge_rules(root, deployment)
        assert merged == [
            {
                "name": "naar-api",
                "from": {"component": "web"},
                "to": {"project": "regelrecht", "component": "api", "port": 8080, "deployment": "dev"},
            }
        ]

    def test_patch_that_overrides_nothing_keeps_root(self) -> None:
        root = [_outbound("naar-api", "web", "regelrecht", "prod", "api", 8080)]
        merged = merge_rules(root, [{"name": "naar-api"}])
        assert merged[0]["to"]["deployment"] == "prod"

    def test_new_name_is_added(self) -> None:
        root = [_outbound("naar-api", "web", "regelrecht", "prod", "api", 8080)]
        extra = _outbound("naar-sandbox", "web", "sandbox", "dev", "api", 9000)
        merged = merge_rules(root, [{"name": "naar-api"}, extra])
        assert [r["name"] for r in merged] == ["naar-api", "naar-sandbox"]

    def test_disabled_drops_the_rule(self) -> None:
        root = [_inbound("van-a", "a", "prod", "worker", "api", 8080)]
        merged = merge_rules(root, [{"name": "van-a", "disabled": True}])
        assert merged == []

    def test_root_rule_without_peer_deployment_filled_by_deployment(self) -> None:
        root = [_outbound("naar-events", "worker", "regelrecht", None, "events", 9090)]
        merged = merge_rules(root, [{"name": "naar-events", "to": {"deployment": "dev"}}])
        assert to_merged_rule(merged[0], direction="outbound") == MergedRule(
            name="naar-events",
            peer_project="regelrecht",
            peer_deployment="dev",
            peer_component="events",
            local_component="worker",
            port=9090,
        )

    def test_root_rule_without_peer_deployment_left_open_is_skipped(self) -> None:
        root = [_outbound("naar-events", "worker", "regelrecht", None, "events", 9090)]
        merged = merge_rules(root, [])
        assert to_merged_rule(merged[0], direction="outbound") is None

    def test_rule_missing_a_non_deployment_field_raises(self) -> None:
        # Root rule with no peer component: had it never should have existed; surfaced by name.
        broken = {
            "name": "kapot",
            "from": {"component": "web"},
            "to": {"project": "x", "deployment": "prod", "port": 8080},
        }
        with pytest.raises(IncompleteRuleError, match="kapot"):
            to_merged_rule(broken, direction="outbound")

    def test_a_patch_does_not_wipe_inherited_fields(self) -> None:
        # A deployment rule carrying explicit None (as a deserialized model might) must not
        # erase the inherited value.
        root = [_outbound("naar-api", "web", "regelrecht", "prod", "api", 8080)]
        merged = merge_rules(root, [{"name": "naar-api", "to": {"deployment": "dev", "component": None, "port": None}}])
        assert merged[0]["to"]["component"] == "api"
        assert merged[0]["to"]["port"] == 8080
        assert merged[0]["to"]["deployment"] == "dev"


# --- validate_service_configs (project + deployment layer) ------------------------------


def _project_with_cross_domain(project_config: dict, deployment_config: dict) -> dict:
    return {
        "name": "me",
        "services": [{"name": "cross-domain-access", "schema-version": "1.0", "config": project_config}],
        "deployments": [
            {
                "name": "dev",
                "cluster": "odcn-production",
                "namespace": "me",
                "services": [{"reference": "cross-domain-access", "config": deployment_config}],
            }
        ],
    }


class TestValidateServiceConfigs:
    def test_valid_project_and_deployment_config_passes(self) -> None:
        project = _project_with_cross_domain(
            {"outbound": [_outbound("naar-api", "web", "regelrecht", "prod", "api", 8080)]},
            {"outbound": [{"name": "naar-api", "to": {"deployment": "dev"}}]},
        )
        validate_service_configs(project)  # does not raise

    def test_unknown_field_at_project_layer_is_rejected(self) -> None:
        project = _project_with_cross_domain({"inbound": [{"name": "r", "bogus": 1}]}, {})
        with pytest.raises(ProjectIntegrityError):
            validate_service_configs(project)

    def test_unknown_field_at_deployment_layer_is_rejected(self) -> None:
        project = _project_with_cross_domain({}, {"outbound": [{"name": "r", "to": {"port": 0}}]})
        with pytest.raises(ProjectIntegrityError):
            validate_service_configs(project)


# --- resolve ----------------------------------------------------------------------------

_CLUSTER = "odcn-production"

_PEER_PROJECT = {
    "name": "regelrecht",
    "deployments": [
        {
            "name": "prod",
            "cluster": "odcn-production",
            "namespace": "regelrecht",
            "components": [{"reference": "api"}, {"reference": "events"}],
        },
        {
            "name": "dev",
            "cluster": "odcn-production",
            "namespace": "regelrecht-dev",
            "components": [{"reference": "api"}],
        },
        {
            "name": "elders",
            "cluster": "other-cluster",
            "namespace": "regelrecht",
            "components": [{"reference": "api"}],
        },
    ],
}


def _lookup(name: str) -> dict | None:
    return _PEER_PROJECT if name == "regelrecht" else None


def _mr(**kw) -> MergedRule:
    base = {
        "name": "r",
        "peer_project": "regelrecht",
        "peer_deployment": "prod",
        "peer_component": "api",
        "local_component": "web",
        "port": 8080,
    }
    base.update(kw)
    return MergedRule(**base)


class TestResolve:
    def test_selector_carries_both_pod_labels(self) -> None:
        [rule] = resolve_rules([_mr()], cluster=_CLUSTER, self_project="me", lookup_project=_lookup)
        assert rule.peer.pod_labels == {"app": "prod-api", "project": "regelrecht"}
        assert rule.peer.namespace == "rig-prd-regelrecht"
        assert rule.local_component == "web"
        assert rule.port == 8080

    def test_self_reference_is_dropped(self) -> None:
        assert (
            resolve_rules([_mr(peer_project="me")], cluster=_CLUSTER, self_project="me", lookup_project=_lookup) == []
        )

    def test_missing_project_is_dropped(self) -> None:
        assert (
            resolve_rules([_mr(peer_project="nope")], cluster=_CLUSTER, self_project="me", lookup_project=_lookup) == []
        )

    def test_missing_deployment_is_dropped(self) -> None:
        assert (
            resolve_rules([_mr(peer_deployment="ghost")], cluster=_CLUSTER, self_project="me", lookup_project=_lookup)
            == []
        )

    def test_other_cluster_is_dropped(self) -> None:
        assert (
            resolve_rules([_mr(peer_deployment="elders")], cluster=_CLUSTER, self_project="me", lookup_project=_lookup)
            == []
        )

    def test_component_not_in_deployment_is_dropped(self) -> None:
        assert (
            resolve_rules([_mr(peer_component="ghost")], cluster=_CLUSTER, self_project="me", lookup_project=_lookup)
            == []
        )

    def test_dedup_and_sort(self) -> None:
        rules = [
            _mr(name="b", peer_component="events"),
            _mr(name="a", peer_component="api"),
            _mr(name="dup", peer_component="api"),  # same selector+port as 'a' -> deduped
        ]
        resolved = resolve_rules(rules, cluster=_CLUSTER, self_project="me", lookup_project=_lookup)
        # api (port 8080) sorts before events by pod_labels; the duplicate api collapses.
        assert [(r.peer.pod_labels["app"], r.port) for r in resolved] == [("prod-api", 8080), ("prod-events", 8080)]


# --- template ---------------------------------------------------------------------------


def _peer(app: str, project: str = "regelrecht", namespace: str = "rig-prd-regelrecht") -> dict:
    return {"namespace": namespace, "pod_labels": {"app": app, "project": project}}


def _render(**overrides) -> dict:
    from opi.generation.manifests import render_template
    from ruamel.yaml import YAML

    values = {"name": "np", "namespace": "rig-prd-me", "pod_selector": {"app": "dev-web"}, "ingress": [], "egress": []}
    values.update(overrides)
    return YAML().load(render_template("service-network-policy.yaml.jinja", values))


class TestTemplate:
    def test_only_ingress_sets_only_ingress_policy_type(self) -> None:
        doc = _render(ingress=[{"peer": _peer("prod-api"), "ports": [8080]}])
        assert doc["spec"]["policyTypes"] == ["Ingress"]
        assert "egress" not in doc["spec"]

    def test_only_egress_sets_only_egress_policy_type(self) -> None:
        doc = _render(egress=[{"peer": _peer("prod-api"), "ports": [8080]}])
        assert doc["spec"]["policyTypes"] == ["Egress"]
        assert "ingress" not in doc["spec"]

    def test_both_directions(self) -> None:
        doc = _render(
            ingress=[{"peer": _peer("prod-api"), "ports": [8080]}],
            egress=[{"peer": _peer("prod-worker"), "ports": [9090]}],
        )
        assert doc["spec"]["policyTypes"] == ["Ingress", "Egress"]

    def test_peer_carries_both_labels_and_ns_and_pod_selector_in_one_entry(self) -> None:
        doc = _render(ingress=[{"peer": _peer("prod-api"), "ports": [8080]}])
        peer_entry = doc["spec"]["ingress"][0]["from"][0]
        assert peer_entry["namespaceSelector"]["matchLabels"] == {"kubernetes.io/metadata.name": "rig-prd-regelrecht"}
        assert peer_entry["podSelector"]["matchLabels"] == {"app": "prod-api", "project": "regelrecht"}

    def test_two_ports_land_on_the_same_peer_entry(self) -> None:
        doc = _render(ingress=[{"peer": _peer("prod-api"), "ports": [8080, 9090]}])
        rule = doc["spec"]["ingress"][0]
        assert [p["port"] for p in rule["ports"]] == [8080, 9090]
        assert len(rule["from"]) == 1

    def test_never_emits_an_empty_allow_all_peer(self) -> None:
        from tests.test_tenant_baseline_netpol import _has_empty_allow_all_rule

        doc = _render(
            ingress=[{"peer": _peer("prod-api"), "ports": [8080]}],
            egress=[{"peer": _peer("prod-worker"), "ports": [9090]}],
        )
        assert not _has_empty_allow_all_rule(doc["spec"]["ingress"])
        assert not _has_empty_allow_all_rule(doc["spec"]["egress"])


# --- service grouping + emission --------------------------------------------------------


class _FakeSummary:
    def __init__(self, data: dict) -> None:
        self.data = data


class _FakeStore:
    def get(self, name: str):
        return _FakeSummary(_PEER_PROJECT) if name == "regelrecht" else None


def _me_project(project_config: dict) -> dict:
    return {
        "name": "me",
        "services": [{"name": "cross-domain-access", "config": project_config}],
        "deployments": [
            {
                "name": "dev",
                "cluster": _CLUSTER,
                "namespace": "me",
                "components": [{"reference": "web"}, {"reference": "worker"}],
            }
        ],
    }


class TestContributeDeploymentManifests:
    def _run(self, monkeypatch, project_config: dict):
        import opi.services.project_store as store_mod
        from opi.services.catalog.base import DeploymentManifestContext
        from opi.services.registry import get_service

        monkeypatch.setattr(store_mod, "get_project_store", lambda: _FakeStore())
        project = _me_project(project_config)
        ctx = DeploymentManifestContext(
            project_name="me",
            project_data=project,
            deployment=project["deployments"][0],
            cluster=_CLUSTER,
            namespace="rig-prd-me",
        )
        service = get_service(ServiceType.CROSS_DOMAIN_ACCESS)
        return service.contribute_deployment_manifests(ctx)

    def test_two_own_components_yield_two_files_with_distinct_pod_selectors(self, monkeypatch) -> None:
        specs = self._run(
            monkeypatch,
            {
                "inbound": [_inbound("van-web", "regelrecht", "prod", "api", "web", 8080)],
                "outbound": [_outbound("naar-worker", "worker", "regelrecht", "prod", "events", 9090)],
            },
        )
        by_file = {s.filename: s for s in specs}
        assert set(by_file) == {
            "dev-cross-domain-access-web-network-policy",
            "dev-cross-domain-access-worker-network-policy",
        }
        assert by_file["dev-cross-domain-access-web-network-policy"].values["pod_selector"] == {"app": "dev-web"}
        assert by_file["dev-cross-domain-access-worker-network-policy"].values["pod_selector"] == {"app": "dev-worker"}

    def test_two_rules_same_peer_merge_into_one_entry_two_ports(self, monkeypatch) -> None:
        specs = self._run(
            monkeypatch,
            {
                "outbound": [
                    _outbound("a", "web", "regelrecht", "prod", "api", 8080),
                    _outbound("b", "web", "regelrecht", "prod", "api", 9090),
                ]
            },
        )
        [spec] = specs
        egress = spec.values["egress"]
        assert len(egress) == 1
        assert egress[0]["ports"] == [8080, 9090]

    def test_rule_for_a_component_not_in_this_deployment_is_skipped(self, monkeypatch) -> None:
        specs = self._run(
            monkeypatch,
            {"inbound": [_inbound("van-x", "regelrecht", "prod", "api", "ghost", 8080)]},
        )
        assert specs == []


# --- service-manifest prune -------------------------------------------------------------


class TestOptionsProviders:
    def test_project_provider_reads_precomputed_and_keeps_unknown_current(self) -> None:
        from opi.forms.visualizers.providers import CrossDomainProjectOptionsProvider

        provider = CrossDomainProjectOptionsProvider(
            yaml_data={"_cross_domain_projects": ["regelrecht", "dp-bn7"]}, current_value="gone"
        )
        values = [o["value"] for o in provider.get_options()]
        assert "regelrecht" in values
        assert "dp-bn7" in values
        assert "gone" in values  # stored-but-unknown kept selectable

    def test_project_provider_empty_shows_explanation(self) -> None:
        from opi.forms.visualizers.providers import CrossDomainProjectOptionsProvider

        options = CrossDomainProjectOptionsProvider(yaml_data={}).get_options()
        assert options == [{"value": "", "label": "Geen andere projecten beschikbaar waar u toegang op heeft"}]

    def test_local_component_provider_reads_own_components(self) -> None:
        from opi.forms.visualizers.providers import CrossDomainLocalComponentOptionsProvider

        provider = CrossDomainLocalComponentOptionsProvider(
            yaml_data={"components": [{"name": "web"}, {"name": "worker"}]}
        )
        assert [o["value"] for o in provider.get_options()] == ["", "web", "worker"]

    def test_port_provider_reads_precomputed_ports(self) -> None:
        from opi.forms.visualizers.providers import CrossDomainPortOptionsProvider

        provider = CrossDomainPortOptionsProvider(yaml_data={"_cross_domain_ports": [8080, 4180]})
        assert [o["value"] for o in provider.get_options()] == ["", "8080", "4180"]


class TestServiceManifestPrune:
    def test_removes_only_stale_service_files(self, tmp_path) -> None:
        from opi.manager.project_manager import _select_obsolete_service_manifests

        stale = "dev-cross-domain-access-web-network-policy.yaml"
        kept_component = "web-deployment.yaml"
        kept_baseline = "dev-tenant-baseline-network-policy.yaml"
        current = "dev-cross-domain-access-worker-network-policy.yaml"
        for name in (stale, kept_component, kept_baseline, current):
            (tmp_path / name).write_text("x")

        prefixes = {f"dev-{s.value}-" for s in ServiceType}
        obsolete = _select_obsolete_service_manifests(str(tmp_path), prefixes, {current})
        assert obsolete == [stale]
