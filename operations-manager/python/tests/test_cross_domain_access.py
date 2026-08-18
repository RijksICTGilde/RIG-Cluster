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
        [rule] = resolve_rules([_mr()], cluster=_CLUSTER, lookup_project=_lookup)
        assert rule.peer.pod_labels == {"app": "prod-api", "project": "regelrecht"}
        assert rule.peer.namespace == "rig-prd-regelrecht"
        assert rule.local_component == "web"
        assert rule.port == 8080

    def test_an_unknown_project_resolves_by_convention_instead_of_being_dropped(self) -> None:
        # RC-42: cross-domain means the peer may live elsewhere or not exist yet. Dropping the
        # rule would turn a declared rule into no policy at all; naming the peer grants it
        # nothing, because the receiver decides with its own policy what it lets in.
        [rule] = resolve_rules([_mr(peer_project="nope")], cluster=_CLUSTER, lookup_project=_lookup)
        assert rule.peer.namespace == "rig-prd-nope"  # the convention: namespace = project name
        assert rule.peer.pod_labels == {"app": "prod-api", "project": "nope"}
        assert rule.port == 8080

    def test_an_unknown_project_does_not_stop_the_other_rules(self) -> None:
        resolved = resolve_rules(
            [_mr(name="unknown", peer_project="nope"), _mr(name="known")],
            cluster=_CLUSTER,
            lookup_project=_lookup,
        )
        assert [r.peer.namespace for r in resolved] == ["rig-prd-nope", "rig-prd-regelrecht"]

    def test_a_known_project_still_uses_its_own_namespace(self) -> None:
        # The convention is the FALLBACK only: 'dev' deploys to regelrecht-dev, which no
        # convention would have guessed.
        [rule] = resolve_rules([_mr(peer_deployment="dev")], cluster=_CLUSTER, lookup_project=_lookup)
        assert rule.peer.namespace == "rig-prd-regelrecht-dev"

    def test_missing_deployment_is_dropped(self) -> None:
        assert resolve_rules([_mr(peer_deployment="ghost")], cluster=_CLUSTER, lookup_project=_lookup) == []

    def test_other_cluster_is_dropped(self) -> None:
        assert resolve_rules([_mr(peer_deployment="elders")], cluster=_CLUSTER, lookup_project=_lookup) == []

    def test_component_not_in_deployment_is_dropped(self) -> None:
        assert resolve_rules([_mr(peer_component="ghost")], cluster=_CLUSTER, lookup_project=_lookup) == []

    def test_dedup_and_sort(self) -> None:
        rules = [
            _mr(name="b", peer_component="events"),
            _mr(name="a", peer_component="api"),
            _mr(name="dup", peer_component="api"),  # same selector+port as 'a' -> deduped
        ]
        resolved = resolve_rules(rules, cluster=_CLUSTER, lookup_project=_lookup)
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


class TestDeploymentLayerForm:
    """A patch editor, not a second rule editor: the name (the merge key) and the peer
    deployment (the field a project rule may leave open), and nothing else."""

    def _service(self):
        from opi.services.registry import get_service

        return get_service(ServiceType.CROSS_DOMAIN_ACCESS)

    def test_the_layer_has_a_form_and_needs_no_exemption(self) -> None:
        from opi.services.catalog.base import ConfigLayer

        service = self._service()
        assert service.config_form_section(ConfigLayer.DEPLOYMENT) is not None
        assert ConfigLayer.DEPLOYMENT not in service.form_exempt_layers

    def test_only_the_name_and_the_peer_deployment_are_editable(self) -> None:
        from opi.services.catalog.cross_domain_access.editables import CROSS_DOMAIN_DEPLOYMENT_EDITABLES

        leaves = [
            child.yaml_path.rsplit("/", 1)[-1] for seq in CROSS_DOMAIN_DEPLOYMENT_EDITABLES for child in seq.children
        ]
        assert sorted(set(leaves)) == ["deployment", "name"]

    def test_paths_are_materialized_to_one_deployment(self) -> None:
        section = self._service().deployment_form_section(3)
        paths = [v.editable.yaml_path for v in section.editables]
        assert paths == [
            "deployments[3]/services{cross-domain-access}/config/inbound",
            "deployments[3]/services{cross-domain-access}/config/outbound",
        ]

    def test_the_peer_deployment_is_not_required_at_either_layer(self) -> None:
        from opi.services.catalog.cross_domain_access.editables import (
            DEPLOYMENT_INBOUND_PEER_DEPLOYMENT_EDITABLE,
            INBOUND_PEER_DEPLOYMENT_EDITABLE,
        )

        # Open on a project rule is a valid, intended state; a patch may also only disable.
        assert not INBOUND_PEER_DEPLOYMENT_EDITABLE.required
        assert not DEPLOYMENT_INBOUND_PEER_DEPLOYMENT_EDITABLE.required

    def test_the_modal_flow_exists_for_a_deployment_index(self) -> None:
        from opi.forms.visualizers.flows import get_flow

        flow = get_flow("modal-edit-cross-domain-deployment-1")
        assert flow.target is not None
        assert (flow.target.list_key, flow.target.index) == ("deployments", 1)

    def test_the_rule_name_select_offers_the_project_rules(self) -> None:
        from opi.forms.visualizers.providers import CrossDomainRuleNameOptionsProvider

        yaml_data = {
            "services": [
                {
                    "name": "cross-domain-access",
                    "config": {"inbound": [{"name": "van-regelrecht"}], "outbound": [{"name": "naar-api"}]},
                }
            ]
        }
        path = "deployments[0]/services{cross-domain-access}/config/inbound[0]/name"
        options = CrossDomainRuleNameOptionsProvider(yaml_data=yaml_data, yaml_path=path).get_options()
        assert [o["value"] for o in options] == ["", "van-regelrecht"]

    def test_a_patch_row_borrows_the_peer_project_from_the_rule_it_patches(self, monkeypatch) -> None:
        # The patch row carries only a name; the peer project lives on the project rule, and
        # without borrowing it the peer-deployment select -- the whole point of this form --
        # would be empty.
        import opi.services.project_store as store_mod
        from opi.core.config import settings
        from opi.forms.visualizers.providers import CrossDomainPeerDeploymentOptionsProvider

        class _Summary:
            data = _PEER_WITH_PORTS

        monkeypatch.setattr(
            store_mod, "get_project_store", lambda: type("S", (), {"get": lambda self, n: _Summary()})()
        )
        monkeypatch.setattr(settings, "CLUSTER_MANAGER", _CLUSTER)
        yaml_data = {
            "_cross_domain_projects": ["regelrecht"],
            "services": [
                {
                    "name": "cross-domain-access",
                    "config": {
                        "inbound": [{"name": "van-regelrecht", "from": {"project": "regelrecht", "component": "api"}}]
                    },
                }
            ],
        }
        path = "deployments[0]/services{cross-domain-access}/config/inbound[0]/from/deployment"
        options = CrossDomainPeerDeploymentOptionsProvider(
            yaml_data=yaml_data, row_data={"name": "van-regelrecht"}, yaml_path=path
        ).get_options()
        assert [o["value"] for o in options] == ["", "prod", "dev"]


class TestFieldRulesComeFromTheConfigModel:
    """The form points at the model's rule instead of restating it (RC-38's move, one layer
    down). Restating it is how the form came to reject names the schema, the API and the
    project store all accept."""

    def _errors(self, editable, value) -> list[str]:
        return editable.validator.validate(value)

    def test_a_peer_project_starting_with_a_digit_is_accepted(self) -> None:
        from opi.services.catalog.cross_domain_access.editables import INBOUND_PEER_PROJECT_EDITABLE

        # DNS-1123 allows a leading digit and the schema does too; the old
        # KubernetesNameValidator demanded a leading letter.
        assert self._errors(INBOUND_PEER_PROJECT_EDITABLE, "7-eleven") == []

    def test_capitals_are_still_rejected(self) -> None:
        from opi.services.catalog.cross_domain_access.editables import INBOUND_PEER_PROJECT_EDITABLE

        assert self._errors(INBOUND_PEER_PROJECT_EDITABLE, "MijnProject")

    def test_the_rule_name_length_limit_is_the_models(self) -> None:
        from opi.services.catalog.cross_domain_access.editables import INBOUND_NAME_EDITABLE

        assert self._errors(INBOUND_NAME_EDITABLE, "a" * 41)
        assert self._errors(INBOUND_NAME_EDITABLE, "a" * 40) == []

    def test_the_port_range_is_the_models(self) -> None:
        from opi.services.catalog.cross_domain_access.editables import INBOUND_PORT_EDITABLE

        assert self._errors(INBOUND_PORT_EDITABLE, 0)
        assert self._errors(INBOUND_PORT_EDITABLE, 65536)
        assert self._errors(INBOUND_PORT_EDITABLE, 8080) == []

    def test_empty_is_left_to_required(self) -> None:
        from opi.services.catalog.cross_domain_access.editables import INBOUND_PEER_DEPLOYMENT_EDITABLE

        assert self._errors(INBOUND_PEER_DEPLOYMENT_EDITABLE, "") == []
        assert self._errors(INBOUND_PEER_DEPLOYMENT_EDITABLE, None) == []


class TestConfigApiSurface:
    """Both config layers are addressable over the API, typed on the service's own model."""

    def _routes(self) -> dict[str, set[str]]:
        from opi.api.v2.router import v2_router

        found: dict[str, set[str]] = {}
        for route in v2_router.routes:
            path = getattr(route, "path", "")
            if "cross-domain-access/config" in path:
                found.setdefault(path, set()).update(route.methods)
        return found

    def test_project_and_deployment_layer_both_have_write_endpoints(self) -> None:
        routes = self._routes()
        project = "/api/v2/projects/{project_name}/services/cross-domain-access/config/project"
        deployment = "/api/v2/projects/{project_name}/services/cross-domain-access/config/deployment/{deployment_name}"
        assert routes.get(project) == {"PUT", "DELETE"}
        assert routes.get(deployment) == {"PUT", "DELETE"}

    def test_the_service_declares_both_layers(self) -> None:
        from opi.services.catalog.base import ConfigLayer
        from opi.services.registry import get_service

        service = get_service(ServiceType.CROSS_DOMAIN_ACCESS)
        assert ConfigLayer.PROJECT in service.config_layers()
        assert ConfigLayer.DEPLOYMENT in service.config_layers()
        assert service.config_model_for(ConfigLayer.DEPLOYMENT) is CrossDomainAccessConfig


class TestSharedFormContext:
    """One builder for both flows, so "works when editing, empty in the create wizard" -- the
    state that made this step unusable -- cannot come back."""

    def _build(self, monkeypatch) -> dict:
        import opi.services.catalog.cross_domain_access.context as context_mod

        class _Summary:
            def __init__(self, name: str) -> None:
                self.name = name

        monkeypatch.setattr(
            context_mod,
            "get_project_store",
            lambda: type(
                "S", (), {"get_all": lambda self: [_Summary("regelrecht"), _Summary("me"), _Summary("verboden")]}
            )(),
        )
        monkeypatch.setattr(context_mod, "is_user_authorized_for_project", lambda name, email: name != "verboden")
        return context_mod.build_cross_domain_context("u@example.com")

    def test_lists_authorized_peers(self, monkeypatch) -> None:
        assert self._build(monkeypatch)["_cross_domain_projects"] == ["me", "regelrecht"]

    def test_unauthorized_projects_are_not_named(self, monkeypatch) -> None:
        assert "verboden" not in self._build(monkeypatch)["_cross_domain_projects"]

    def test_the_own_project_is_selectable(self, monkeypatch) -> None:
        # The tenant baseline isolates per DEPLOYMENT, so reaching another deployment of your
        # own project needs a rule too. Hiding the own project left that with no way to say it.
        assert "me" in self._build(monkeypatch)["_cross_domain_projects"]

    def test_both_flows_call_the_same_builder(self) -> None:
        import inspect

        from opi.web import router_detail_edit, router_wizard

        for module in (router_detail_edit, router_wizard):
            assert "build_cross_domain_context(" in inspect.getsource(module)


# --- the per-row cascade (RC-42) --------------------------------------------------------

_PEER_WITH_PORTS = {
    **_PEER_PROJECT,
    "components": [
        {"name": "api", "ports": {"inbound": [8080, 9090]}},
        {"name": "events", "ports": {"inbound": [5000]}, "services": ["authorization-wall"]},
    ],
}

_OWN = {
    "_cross_domain_projects": ["regelrecht"],
    "_cross_domain_ports": [8080, 3000],
    "components": [
        {"name": "web", "ports": {"inbound": [3000]}},
        {"name": "worker", "ports": {"inbound": [8080]}, "services": [{"name": "authorization-wall"}]},
    ],
}


def _peer_row(**peer) -> dict:
    """An inbound row: the peer sits on ``from``."""
    return {"name": "r", "from": peer, "to": {"component": "web"}}


def _out_row(**peer) -> dict:
    """An outbound row: the peer sits on ``to``."""
    return {"name": "r", "from": {"component": "web"}, "to": peer}


@pytest.fixture
def _peer_store(monkeypatch):
    """Point the ProjectStore lookup at the peer fixture and pin the managed cluster."""
    import opi.services.project_store as store_mod
    from opi.core.config import settings

    class _Summary:
        data = _PEER_WITH_PORTS

    class _Store:
        def get(self, name: str):
            return _Summary() if name == "regelrecht" else None

    monkeypatch.setattr(store_mod, "get_project_store", lambda: _Store())
    monkeypatch.setattr(settings, "CLUSTER_MANAGER", _CLUSTER)


@pytest.mark.usefixtures("_peer_store")
class TestPeerCascade:
    """project -> deployment -> component, each list a function of the SAME row."""

    def _deployments(self, row: dict, path: str = "inbound[0]/from/deployment", current=None) -> list[str]:
        from opi.forms.visualizers.providers import CrossDomainPeerDeploymentOptionsProvider

        provider = CrossDomainPeerDeploymentOptionsProvider(
            yaml_data=_OWN, row_data=row, yaml_path=path, current_value=current
        )
        return [o["value"] for o in provider.get_options()]

    def _components(self, row: dict, path: str = "inbound[0]/from/component", current=None) -> list[str]:
        from opi.forms.visualizers.providers import CrossDomainPeerComponentOptionsProvider

        provider = CrossDomainPeerComponentOptionsProvider(
            yaml_data=_OWN, row_data=row, yaml_path=path, current_value=current
        )
        return [o["value"] for o in provider.get_options()]

    def test_deployments_follow_the_project_chosen_in_this_row(self) -> None:
        assert self._deployments(_peer_row(project="regelrecht")) == ["", "prod", "dev"]

    def test_deployment_on_another_cluster_is_not_offered(self) -> None:
        # 'elders' runs on another cluster; resolve.py would skip such a rule, so offering it
        # would be offering a rule that silently never applies.
        assert "elders" not in self._deployments(_peer_row(project="regelrecht"))

    def test_no_project_yet_explains_instead_of_showing_a_blank_select(self) -> None:
        from opi.forms.visualizers.providers import CrossDomainPeerDeploymentOptionsProvider

        options = CrossDomainPeerDeploymentOptionsProvider(
            yaml_data=_OWN, row_data=_peer_row(), yaml_path="inbound[0]/from/deployment"
        ).get_options()
        assert options == [{"value": "", "label": "Kies eerst een project"}]

    def test_unknown_project_reads_nothing(self) -> None:
        # Not on the authorized list: not looked up at all, so no peer data leaks into the form.
        assert self._deployments(_peer_row(project="geheim")) == [""]

    def test_stored_deployment_that_no_longer_exists_stays_selectable(self) -> None:
        values = self._deployments(_peer_row(project="regelrecht"), current="weg")
        assert "weg" in values

    def test_components_follow_the_chosen_deployment(self) -> None:
        row = _peer_row(project="regelrecht", deployment="dev")
        assert self._components(row) == ["", "api"]

    def test_components_without_a_deployment_are_the_union_over_this_cluster(self) -> None:
        # A project-level rule may leave the peer deployment open; the component must still be
        # fillable, and a component name is project-level, so the union is exactly right.
        row = _peer_row(project="regelrecht")
        assert self._components(row) == ["", "api", "events"]

    def test_outbound_reads_the_peer_from_the_to_side(self) -> None:
        row = _out_row(project="regelrecht", deployment="prod")
        assert self._components(row, path="outbound[0]/to/component") == ["", "api", "events"]

    def test_each_row_is_independent(self) -> None:
        assert self._deployments(_peer_row(project="regelrecht")) == ["", "prod", "dev"]
        assert self._deployments(_peer_row(project="geheim")) == [""]


@pytest.mark.usefixtures("_peer_store")
class TestPortIsTheReceivingSide:
    """The port belongs to the pod that is REACHED: mine inbound, the peer's outbound."""

    def _ports(self, row: dict, path: str, current=None) -> list[str]:
        from opi.forms.visualizers.providers import CrossDomainPortOptionsProvider

        provider = CrossDomainPortOptionsProvider(yaml_data=_OWN, row_data=row, yaml_path=path, current_value=current)
        return [o["value"] for o in provider.get_options()]

    def test_inbound_offers_my_own_components_ports(self) -> None:
        row = {"name": "r", "from": {"project": "regelrecht"}, "to": {"component": "web"}}
        assert self._ports(row, "inbound[0]/to/port") == ["", "3000"]

    def test_inbound_adds_4180_when_an_authorization_wall_fronts_my_component(self) -> None:
        row = {"name": "r", "from": {"project": "regelrecht"}, "to": {"component": "worker"}}
        assert self._ports(row, "inbound[0]/to/port") == ["", "8080", "4180"]

    def test_inbound_without_a_component_falls_back_to_the_project_union(self) -> None:
        # Union over my own components (3000 from web, 8080 + 4180 from the walled worker),
        # derived from the form's own data so the create wizard has it too.
        row = {"name": "r", "from": {"project": "regelrecht"}, "to": {}}
        assert self._ports(row, "inbound[0]/to/port") == ["", "8080", "3000", "4180"]

    def test_inbound_union_needs_no_precomputed_context(self) -> None:
        yaml_data = {k: v for k, v in _OWN.items() if k != "_cross_domain_ports"}
        from opi.forms.visualizers.providers import CrossDomainPortOptionsProvider

        options = CrossDomainPortOptionsProvider(
            yaml_data=yaml_data, row_data={"name": "r", "to": {}}, yaml_path="inbound[0]/to/port"
        ).get_options()
        assert [o["value"] for o in options] == ["", "3000", "8080", "4180"]

    def test_outbound_offers_the_peer_components_ports(self) -> None:
        row = _out_row(project="regelrecht", deployment="prod", component="api")
        assert self._ports(row, "outbound[0]/to/port") == ["", "8080", "9090"]

    def test_outbound_adds_4180_for_a_peer_behind_an_authorization_wall(self) -> None:
        row = _out_row(project="regelrecht", deployment="prod", component="events")
        assert self._ports(row, "outbound[0]/to/port") == ["", "5000", "4180"]

    def test_outbound_never_offers_my_own_ports(self) -> None:
        # The old behaviour: both directions got the same precomputed union of MY ports.
        row = _out_row(project="regelrecht", deployment="prod", component="api")
        assert "3000" not in self._ports(row, "outbound[0]/to/port")

    def test_outbound_without_a_peer_component_says_so(self) -> None:
        from opi.forms.visualizers.providers import CrossDomainPortOptionsProvider

        row = _out_row(project="regelrecht", deployment="prod")
        options = CrossDomainPortOptionsProvider(
            yaml_data=_OWN, row_data=row, yaml_path="outbound[0]/to/port"
        ).get_options()
        assert options == [{"value": "", "label": "Kies eerst een component"}]

    def test_a_stored_port_outside_the_list_stays_selectable(self) -> None:
        row = _out_row(project="regelrecht", deployment="prod", component="api")
        assert "1234" in self._ports(row, "outbound[0]/to/port", current="1234")


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


class TestDeploymentCardButton:
    """The service owns the button that opens its per-deployment form."""

    def _actions(self, project: dict, deployment_name: str = "dev") -> list:
        from opi.services.registry import collect_deployment_actions

        return collect_deployment_actions(project, deployment_name)

    def _project(self, services: list) -> dict:
        return {
            "name": "me",
            "services": services,
            "deployments": [{"name": "acc", "cluster": _CLUSTER}, {"name": "dev", "cluster": _CLUSTER}],
        }

    def test_the_button_opens_this_deployments_flow(self) -> None:
        actions = [a for a in self._actions(self._project(["cross-domain-access"])) if a.icon == "netwerk"]
        assert len(actions) == 1
        # Index 1: the button must address the deployment it sits on, not the first one.
        assert actions[0].modal_endpoint == "/projects/me/modal-wizard/modal-edit-cross-domain-deployment-1"

    def test_no_button_when_the_project_does_not_use_the_service(self) -> None:
        assert [a for a in self._actions(self._project(["redis"])) if a.icon == "netwerk"] == []

    def test_no_button_for_an_unknown_deployment(self) -> None:
        actions = self._actions(self._project(["cross-domain-access"]), deployment_name="bestaat-niet")
        assert [a for a in actions if a.icon == "netwerk"] == []


class TestDeWallPoortLegtZichzelfUit:
    """4180 is een randgeval dat we wel moeten aanbieden maar zelden gebruikt wordt.

    De uitleg hoort bij die ene optie en niet in een hulptekst onder het veld: hij geldt
    voor een keuze, niet voor het veld, en wie hem niet kiest hoeft er niets van te weten.
    """

    def _opties(self, component: dict) -> dict[str, str]:
        from opi.forms.visualizers.providers import CrossDomainPortOptionsProvider

        provider = CrossDomainPortOptionsProvider(
            yaml_data={"components": [component]},
            row_data={"to": {"component": "web"}},
            yaml_path="services/cross-domain-access/config/inbound[0]/to/port",
        )
        return {optie["value"]: optie["label"] for optie in provider.get_options()}

    def test_de_wall_poort_zegt_waar_hij_vandaan_komt(self) -> None:
        opties = self._opties(
            {"name": "web", "ports": {"inbound": [8080]}, "services": ["authorization-wall"]},
        )
        assert opties["4180"] == "4180 (via authorization wall)"
        assert opties["8080"] == "8080", "een gewone poort krijgt geen bijschrift"

    def test_een_eigen_4180_wordt_niet_aan_de_wall_toegeschreven(self) -> None:
        """Een component mag 4180 gewoon zelf als inbound-poort hebben; dan is dat label onwaar."""
        opties = self._opties({"name": "web", "ports": {"inbound": [8080, 4180]}, "services": []})
        assert opties["4180"] == "4180"
