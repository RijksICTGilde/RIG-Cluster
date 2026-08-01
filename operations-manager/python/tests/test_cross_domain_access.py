"""Unit tests for the cross-domain-access service (RC-15).

Covers the two pure cores -- the layer merge (``merge.py``) and the peer resolution
(``resolve.py``) -- plus the config model's shape guarantees. Manifest-template, prune and
API tests live alongside the templates / router they exercise.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from opi.services.catalog.cross_domain_access.config_model import CrossDomainAccessConfig
from opi.services.catalog.cross_domain_access.merge import (
    IncompleteRuleError,
    MergedRule,
    merge_rules,
    to_merged_rule,
)
from opi.services.catalog.cross_domain_access.resolve import resolve_rules
from opi.manager.project_validation import validate_service_configs
from opi.core.project_schema import ProjectIntegrityError


# --- config model -----------------------------------------------------------------------


class TestConfigModel:
    def test_stored_root_rule_may_leave_peer_deployment_open(self) -> None:
        # The stored (patch) model is lenient: a root outbound rule without a peer deployment
        # is valid at rest; completeness is judged at merge time.
        config = CrossDomainAccessConfig.model_validate(
            {"outbound": [{"name": "naar-x", "from": {"component": "web"}, "to": {"project": "x", "component": "api", "port": 8080}}]}
        )
        assert config.outbound[0].to.deployment is None

    def test_unknown_field_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CrossDomainAccessConfig.model_validate({"inbound": [{"name": "r", "bogus": 1}]})

    def test_project_on_own_side_is_rejected(self) -> None:
        # 'to' of an inbound rule is my side (LocalTarget): it has no 'project'.
        with pytest.raises(ValidationError):
            CrossDomainAccessConfig.model_validate(
                {"inbound": [{"name": "r", "from": {"project": "x", "deployment": "prod", "component": "api"}, "to": {"project": "me", "component": "web", "port": 8080}}]}
            )

    def test_port_out_of_range_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CrossDomainAccessConfig.model_validate(
                {"inbound": [{"name": "r", "from": {"project": "x", "deployment": "prod", "component": "api"}, "to": {"component": "web", "port": 99999}}]}
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
            {"name": "naar-api", "from": {"component": "web"}, "to": {"project": "regelrecht", "component": "api", "port": 8080, "deployment": "dev"}}
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
        broken = {"name": "kapot", "from": {"component": "web"}, "to": {"project": "x", "deployment": "prod", "port": 8080}}
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
    base = dict(
        name="r",
        peer_project="regelrecht",
        peer_deployment="prod",
        peer_component="api",
        local_component="web",
        port=8080,
    )
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
        assert resolve_rules([_mr(peer_project="me")], cluster=_CLUSTER, self_project="me", lookup_project=_lookup) == []

    def test_missing_project_is_dropped(self) -> None:
        assert resolve_rules([_mr(peer_project="nope")], cluster=_CLUSTER, self_project="me", lookup_project=_lookup) == []

    def test_missing_deployment_is_dropped(self) -> None:
        assert resolve_rules([_mr(peer_deployment="ghost")], cluster=_CLUSTER, self_project="me", lookup_project=_lookup) == []

    def test_other_cluster_is_dropped(self) -> None:
        assert resolve_rules([_mr(peer_deployment="elders")], cluster=_CLUSTER, self_project="me", lookup_project=_lookup) == []

    def test_component_not_in_deployment_is_dropped(self) -> None:
        assert resolve_rules([_mr(peer_component="ghost")], cluster=_CLUSTER, self_project="me", lookup_project=_lookup) == []

    def test_dedup_and_sort(self) -> None:
        rules = [
            _mr(name="b", peer_component="events"),
            _mr(name="a", peer_component="api"),
            _mr(name="dup", peer_component="api"),  # same selector+port as 'a' -> deduped
        ]
        resolved = resolve_rules(rules, cluster=_CLUSTER, self_project="me", lookup_project=_lookup)
        # api (port 8080) sorts before events by pod_labels; the duplicate api collapses.
        assert [(r.peer.pod_labels["app"], r.port) for r in resolved] == [("prod-api", 8080), ("prod-events", 8080)]
