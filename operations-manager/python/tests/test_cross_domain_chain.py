"""From what the wizard submits to the NetworkPolicy that comes out (RC-42).

The unit tests around this one each cover a link: the merge, the peer resolution, the
template. None of them cover the CHAIN, and the chain is where this service actually failed --
the form could not be filled in at all, so nothing downstream ever ran on real form output.

What is exercised here, in one line each:

1. the project-level form's submission (the shape json-enc posts) through the real form
   processor, into project data;
2. that project data through ``contribute_deployment_manifests``;
3. the resulting values through the real Jinja template, read back as YAML.

Then the same again with a per-deployment PATCH that overrides ONLY the peer deployment, to
show the second layer changes exactly one thing in the rendered policy.

The peer project is a stub in the ProjectStore rather than a second project driven through the
wizard: that keeps this test about the chain. Two real projects finding each other is a
separate test.
"""

from __future__ import annotations

from typing import Any

import pytest
from opi.forms.editables.processor import EditableFormProcessor
from opi.services.catalog.base import ConfigLayer, DeploymentManifestContext
from opi.services.registry import get_service
from opi.services.schema_migration import normalize_service_entries
from opi.services.services_enums import ServiceType
from ruamel.yaml import YAML

_CLUSTER = "odcn-production"

#: The peer, as it sits in the store: two deployments, one component with two ports.
_PEER = {
    "name": "regelrecht",
    "components": [{"name": "api", "ports": {"inbound": [8080]}}],
    "deployments": [
        {"name": "prod", "cluster": _CLUSTER, "namespace": "regelrecht", "components": [{"reference": "api"}]},
        {"name": "acc", "cluster": _CLUSTER, "namespace": "regelrecht-acc", "components": [{"reference": "api"}]},
    ],
}


def _own_project() -> dict[str, Any]:
    return {
        "name": "me",
        "_cross_domain_projects": ["regelrecht"],
        "services": [{"name": "cross-domain-access", "config": {}}],
        "components": [{"name": "web", "ports": {"inbound": [3000]}}],
        "deployments": [
            {
                "name": "dev",
                "cluster": _CLUSTER,
                "namespace": "me",
                "services": [],
                "components": [{"reference": "web"}],
            }
        ],
    }


@pytest.fixture(autouse=True)
def _store_and_cluster(monkeypatch):
    import opi.services.project_store as store_mod
    from opi.core.config import settings

    class _Summary:
        data = _PEER

    class _Store:
        def get(self, name: str):
            return _Summary() if name == "regelrecht" else None

    monkeypatch.setattr(store_mod, "get_project_store", lambda: _Store())
    monkeypatch.setattr(settings, "CLUSTER_MANAGER", _CLUSTER)


def _service():
    return get_service(ServiceType.CROSS_DOMAIN_ACCESS)


async def _submit(section, submitted: dict[str, Any], project: dict[str, Any]) -> dict[str, Any]:
    """One form submission through the real processor, exactly as a wizard step does."""
    processor = EditableFormProcessor()
    result, errors = await processor.process_json_submission(
        submitted, section.editables, project, edit_mode=True, strip_transients=False
    )
    assert errors == {}, errors
    normalize_service_entries(result)
    return result


#: What json-enc posts for the project-level step: the sequence rows nested under the wizard's
#: virtual services root, keyed exactly as the rendered field names are.
def _project_submission(peer_deployment: str | None) -> dict[str, Any]:
    peer: dict[str, Any] = {"project": "regelrecht", "component": "api"}
    if peer_deployment is not None:
        peer["deployment"] = peer_deployment
    return {
        "_services-config": {
            "cross-domain-access": {
                "config": {
                    "inbound": [{"name": "van-regelrecht", "from": peer, "to": {"component": "web", "port": "3000"}}],
                    "outbound": [
                        {
                            "name": "naar-regelrecht",
                            "from": {"component": "web"},
                            "to": {**peer, "port": "8080"},
                        }
                    ],
                }
            }
        }
    }


#: What json-enc posts for the per-deployment patch step. ``{K}`` becomes a real list entry.
def _deployment_submission(peer_deployment: str) -> dict[str, Any]:
    return {
        "deployments": [
            {
                "services": [
                    {
                        "cross-domain-access": {
                            "config": {
                                "inbound": [{"name": "van-regelrecht", "from": {"deployment": peer_deployment}}],
                                "outbound": [{"name": "naar-regelrecht", "to": {"deployment": peer_deployment}}],
                            }
                        }
                    }
                ]
            }
        ]
    }


def _manifests(project: dict[str, Any]) -> list[Any]:
    ctx = DeploymentManifestContext(
        project_name="me",
        project_data=project,
        deployment=project["deployments"][0],
        cluster=_CLUSTER,
        namespace="rig-prd-me",
    )
    return _service().contribute_deployment_manifests(ctx)


def _policy(spec) -> dict[str, Any]:
    from opi.generation.manifests import render_template

    return YAML().load(render_template(spec.template_path, spec.values))


def _peers(rule_block: list[Any], key: str) -> list[tuple[str, str, list[int]]]:
    """(namespace, pod app label, ports) per peer entry of a rendered policy block."""
    return [
        (
            entry["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"],
            entry["podSelector"]["matchLabels"]["app"],
            [p["port"] for p in rule["ports"]],
        )
        for rule in rule_block
        for entry in rule[key]
    ]


class TestWizardToNetworkPolicy:
    @pytest.mark.asyncio
    async def test_a_rule_filled_in_the_form_becomes_a_networkpolicy(self) -> None:
        project = await _submit(
            _service().config_form_section(ConfigLayer.PROJECT), _project_submission("prod"), _own_project()
        )

        # 1. it landed in the project file, in the stored shape the schema describes
        config = next(e for e in project["services"] if e.get("name") == "cross-domain-access")["config"]
        assert config["inbound"][0] == {
            "name": "van-regelrecht",
            "from": {"project": "regelrecht", "deployment": "prod", "component": "api"},
            "to": {"component": "web", "port": 3000},
        }

        # 2. + 3. it becomes one policy for the own component, with both directions
        [spec] = _manifests(project)
        assert spec.filename == "dev-cross-domain-access-web-network-policy"
        doc = _policy(spec)
        assert doc["kind"] == "NetworkPolicy"
        assert doc["metadata"]["namespace"] == "rig-prd-me"
        assert doc["spec"]["podSelector"]["matchLabels"] == {"app": "dev-web"}
        assert _peers(doc["spec"]["ingress"], "from") == [("rig-prd-regelrecht", "prod-api", [3000])]
        assert _peers(doc["spec"]["egress"], "to") == [("rig-prd-regelrecht", "prod-api", [8080])]

    @pytest.mark.asyncio
    async def test_the_port_is_stored_as_a_number_not_the_forms_string(self) -> None:
        project = await _submit(
            _service().config_form_section(ConfigLayer.PROJECT), _project_submission("prod"), _own_project()
        )
        config = next(e for e in project["services"] if e.get("name") == "cross-domain-access")["config"]
        assert isinstance(config["inbound"][0]["to"]["port"], int)

    @pytest.mark.asyncio
    async def test_the_saved_rule_passes_the_projects_own_validation(self) -> None:
        from opi.manager.project_validation import validate_service_configs

        project = await _submit(
            _service().config_form_section(ConfigLayer.PROJECT), _project_submission("prod"), _own_project()
        )
        validate_service_configs(project)  # raises on a bad config block


class TestDeploymentPatchChangesOnlyThePeerDeployment:
    async def _patched(self) -> dict[str, Any]:
        """A project rule pointing at 'prod', patched to 'acc' for this deployment."""
        project = await _submit(
            _service().config_form_section(ConfigLayer.PROJECT), _project_submission("prod"), _own_project()
        )
        return await _submit(_service().deployment_form_section(0), _deployment_submission("acc"), project)

    @pytest.mark.asyncio
    async def test_the_patch_stores_only_the_peer_deployment(self) -> None:
        project = await self._patched()
        entry = next(e for e in project["deployments"][0]["services"] if e.get("reference") == "cross-domain-access")
        assert entry["config"]["inbound"] == [{"name": "van-regelrecht", "from": {"deployment": "acc"}}]

    @pytest.mark.asyncio
    async def test_the_patch_lands_in_the_schemas_service_envelope(self) -> None:
        # The editables write the legacy name-as-key shape; the deployment-service envelope
        # in project_v2.json rejects it, so the save path must normalize it.
        from opi.core.project_schema import validate_project_schema

        project = await self._patched()
        entry = project["deployments"][0]["services"][0]
        assert set(entry) <= {"name", "reference", "schema-version", "config"}
        # ``_cross_domain_projects`` is form context, not project data; the wizard's submit
        # path strips it (see the underscore rule in router_wizard), so it is not part of
        # what gets validated here either.
        validate_project_schema({k: v for k, v in project.items() if not k.startswith("_")} | {"schema-version": 2})

    @pytest.mark.asyncio
    async def test_the_policy_now_points_at_the_other_deployment(self) -> None:
        [spec] = _manifests(await self._patched())
        doc = _policy(spec)
        # Namespace and pod label follow 'acc'; the port and the component are unchanged,
        # which is the whole promise of a patch.
        assert _peers(doc["spec"]["ingress"], "from") == [("rig-prd-regelrecht-acc", "acc-api", [3000])]
        assert _peers(doc["spec"]["egress"], "to") == [("rig-prd-regelrecht-acc", "acc-api", [8080])]

    @pytest.mark.asyncio
    async def test_a_project_rule_with_no_peer_deployment_is_completed_by_the_patch(self) -> None:
        # The case the two layers exist for: the project says "praat met regelrecht", each
        # deployment says with WHICH deployment of regelrecht.
        project = await _submit(
            _service().config_form_section(ConfigLayer.PROJECT), _project_submission(None), _own_project()
        )
        assert _manifests(project) == []  # open peer deployment: nothing to point at yet

        patched = await _submit(_service().deployment_form_section(0), _deployment_submission("prod"), project)
        [spec] = _manifests(patched)
        assert _peers(_policy(spec)["spec"]["ingress"], "from") == [("rig-prd-regelrecht", "prod-api", [3000])]


class TestAnUnknownPeerStillProducesAPolicy:
    """A rule may name a project this cluster does not know -- it may be managed elsewhere, or
    simply be created later. That must produce a policy anyway (the receiver decides what it
    lets in), and it must never stop the OTHER rules from producing theirs."""

    @pytest.mark.asyncio
    async def test_the_other_rules_still_generate(self) -> None:
        project = await _submit(
            _service().config_form_section(ConfigLayer.PROJECT), _project_submission("prod"), _own_project()
        )
        config = next(e for e in project["services"] if e.get("name") == "cross-domain-access")["config"]
        config["inbound"].append(
            {
                "name": "van-onbekend",
                "from": {"project": "bestaat-niet", "deployment": "prod", "component": "api"},
                "to": {"component": "web", "port": 3000},
            }
        )

        [spec] = _manifests(project)
        doc = _policy(spec)
        # Both entries are there: the known peer from its own project file, the unknown one by
        # the namespace convention. Sorted, so 'bestaat-niet' comes first.
        assert _peers(doc["spec"]["ingress"], "from") == [
            ("rig-prd-bestaat-niet", "prod-api", [3000]),
            ("rig-prd-regelrecht", "prod-api", [3000]),
        ]

    @pytest.mark.asyncio
    async def test_a_rule_naming_only_an_unknown_peer_still_yields_the_policy(self) -> None:
        project = _own_project()
        project["services"][0]["config"] = {
            "inbound": [
                {
                    "name": "van-onbekend",
                    "from": {"project": "bestaat-niet", "deployment": "prod", "component": "api"},
                    "to": {"component": "web", "port": 3000},
                }
            ]
        }
        [spec] = _manifests(project)
        assert _peers(_policy(spec)["spec"]["ingress"], "from") == [("rig-prd-bestaat-niet", "prod-api", [3000])]


class TestFormContextNeverReachesTheProjectFile:
    """``_cross_domain_projects`` feeds the select and nothing else.

    It now lives in the CREATE wizard's template layer too, and that path builds its final
    submission from the whole merged view -- so the rule that keeps it out of the file has to
    be somewhere, and it is the leading underscore.
    """

    def test_the_schema_would_reject_it(self) -> None:
        from opi.core.project_schema import ProjectSchemaError, validate_project_schema

        project = {k: v for k, v in _own_project().items() if k != "_cross_domain_projects"}
        validate_project_schema({**project, "schema-version": 2})
        with pytest.raises(ProjectSchemaError):
            validate_project_schema({**project, "schema-version": 2, "_cross_domain_projects": ["x"]})

    def test_the_submit_path_strips_top_level_underscore_keys(self) -> None:
        import inspect

        from opi.web import router_wizard

        source = inspect.getsource(router_wizard)
        assert 'for key in [key for key in final_data if key.startswith("_")]' in source
