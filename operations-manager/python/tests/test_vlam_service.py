"""Tests for the ``vlam`` service (RC-142).

Five things carry the design, and each one is a way this could fail silently:

1. **The address and the network rule come from ONE configuration entry.** An address
   naming one pod while the rule opens another presents itself as a timeout at the
   consumer, days after the change.
2. **Availability is refused at the SAVE path, not only on the wizard card.** The API and
   a hand-written project file never see a card.
3. **The project's selection is what switches the contribution on.** The service is
   deployment-bound, so no component ever ticks it; a component-scoped activation would
   answer "no" for every component forever and nothing would ever be contributed.
4. **The variable is ADDED to the component's own variables**, not put in their place.
5. **Switching the service off removes the policy**, which is the prune prefix on the
   filename plus the service returning nothing.
"""

from __future__ import annotations

import pytest
from opi.core.cluster_config import get_vlam_config
from opi.core.project_schema import ProjectIntegrityError
from opi.generation.manifests import render_template
from opi.manager.project_manager import apply_manifest_contributions, collect_manifest_contributions
from opi.manager.project_validation import validate_service_availability
from opi.services.catalog.base import DeploymentManifestContext, ManifestContext, ManifestContribution
from opi.services.catalog.vlam.endpoint import vlam_endpoint
from opi.services.registry import get_service
from opi.services.services_enums import ServiceBinding, ServiceType
from ruamel.yaml import YAML

SERVICE = get_service(ServiceType.VLAM)

#: The cluster that has VLAM, and one that does not. Read from the configuration rather
#: than assumed, so this file says the same thing the code does.
WITH_VLAM = "odcn-production"
WITHOUT_VLAM = "local"


def _project(*, selected: bool = True, cluster: str = WITH_VLAM) -> dict:
    """A project with one deployment of one component, optionally taking vlam."""
    return {
        "name": "myproject",
        "services": ([{"name": ServiceType.VLAM.value}] if selected else []),
        "components": [{"name": "web", "services": []}],
        "deployments": [
            {
                "name": "prod",
                "cluster": cluster,
                "namespace": "myproject",
                "components": [{"reference": "web"}],
            }
        ],
    }


class TestTheClusterConfiguration:
    """The premise of everything below: which clusters know a VLAM endpoint."""

    def test_the_cluster_with_the_ron_link_has_one(self) -> None:
        assert get_vlam_config(WITH_VLAM) is not None

    def test_a_cluster_without_the_ron_link_has_none(self) -> None:
        assert get_vlam_config(WITHOUT_VLAM) is None


class TestTheEndpoint:
    """One entry, two derived answers -- they cannot drift apart."""

    def test_the_address_and_the_peer_name_the_same_pod(self) -> None:
        endpoint = vlam_endpoint(WITH_VLAM)
        assert endpoint is not None
        assert endpoint.pod_labels["app"] in endpoint.api_url
        assert endpoint.namespace in endpoint.api_url
        assert str(endpoint.port) in endpoint.api_url

    def test_the_address_is_plain_http_on_the_configured_port(self) -> None:
        """The proxy terminates TLS towards VLAM; inside the cluster it is HTTP."""
        endpoint = vlam_endpoint(WITH_VLAM)
        assert endpoint is not None
        config = get_vlam_config(WITH_VLAM)
        assert config is not None
        assert endpoint.api_url == (
            f"http://{config['deployment']}-{config['component']}"
            f".{endpoint.namespace}.svc.cluster.local:{config['port']}"
        )

    def test_the_namespace_carries_the_cluster_prefix(self) -> None:
        """The project file says ``vlam-wt8``; production runs it as ``rig-prd-vlam-wt8``."""
        endpoint = vlam_endpoint(WITH_VLAM)
        assert endpoint is not None
        assert endpoint.namespace == "rig-prd-vlam-wt8"

    def test_the_peer_is_pinned_by_project_as_well_as_by_app(self) -> None:
        """The project label closes the gap that another project takes that namespace name."""
        endpoint = vlam_endpoint(WITH_VLAM)
        assert endpoint is not None
        assert endpoint.pod_labels == {"app": "productie-vlam-proxy-intern", "project": "vlam-wt8"}

    def test_a_cluster_without_vlam_has_no_endpoint(self) -> None:
        assert vlam_endpoint(WITHOUT_VLAM) is None


class TestTheServiceDeclaration:
    def test_it_is_selectable_by_a_user(self) -> None:
        assert SERVICE.definition.hidden is False

    def test_it_binds_per_deployment(self) -> None:
        """Every pod of the deployment gets the same address; there is nothing to pick."""
        assert SERVICE.definition.binding is ServiceBinding.DEPLOYMENT

    def test_it_carries_no_config_at_all(self) -> None:
        assert SERVICE.config_model is None
        assert SERVICE.config_layers() == []

    def test_it_hands_out_one_variable(self) -> None:
        assert [var.name for var in SERVICE.definition.variables] == ["VLAM_API_URL"]

    def test_binding_it_somewhere_enrols_it_at_project_level(self) -> None:
        """RC-103: no project layer means nothing to decide there, so a bare selection is
        added rather than refused. The cluster question is a different one, and
        ``available_on_cluster`` answers it -- see TestAvailability."""
        assert SERVICE.implicit_project_entry() == ServiceType.VLAM.value


class TestAvailability:
    def test_it_is_available_where_the_configuration_knows_an_endpoint(self) -> None:
        assert SERVICE.available_on_cluster(WITH_VLAM) is True

    def test_it_is_not_available_elsewhere(self) -> None:
        assert SERVICE.available_on_cluster(WITHOUT_VLAM) is False

    def test_a_project_on_a_cluster_without_vlam_is_refused(self) -> None:
        errors = validate_service_availability(_project(cluster=WITHOUT_VLAM))
        assert len(errors) == 1
        assert ServiceType.VLAM.value in errors[0]
        assert WITHOUT_VLAM in errors[0]
        assert "prod" in errors[0]

    def test_a_project_on_the_cluster_that_has_vlam_is_accepted(self) -> None:
        assert validate_service_availability(_project(cluster=WITH_VLAM)) == []

    def test_a_project_that_did_not_select_it_is_never_refused(self) -> None:
        assert validate_service_availability(_project(selected=False, cluster=WITHOUT_VLAM)) == []

    @pytest.mark.asyncio
    async def test_the_save_path_raises_and_names_the_cluster(self) -> None:
        """The refusal that counts: the wizard, the API and a hand-edited file all pass here."""
        from opi.manager.project_validation import validate_project_structure

        with pytest.raises(ProjectIntegrityError) as error:
            await validate_project_structure(_project(cluster=WITHOUT_VLAM))
        assert WITHOUT_VLAM in str(error.value)
        assert ServiceType.VLAM.value in str(error.value)

    @pytest.mark.asyncio
    async def test_the_same_project_saves_on_the_cluster_that_has_vlam(self) -> None:
        from opi.manager.project_validation import validate_project_structure

        await validate_project_structure(_project(cluster=WITH_VLAM))


class TestTheWizardCard:
    """Presentation only, but a card for something the cluster cannot deliver is a lie."""

    def _service_values(self, cluster: str, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        from opi.core.config import settings
        from opi.forms.visualizers.providers import ServiceOptionsProvider

        monkeypatch.setattr(settings, "CLUSTER_MANAGER", cluster)
        return [option["value"] for option in ServiceOptionsProvider().get_options()]

    def test_the_card_is_offered_on_the_cluster_that_has_vlam(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert ServiceType.VLAM.value in self._service_values(WITH_VLAM, monkeypatch)

    def test_the_card_is_absent_elsewhere(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert ServiceType.VLAM.value not in self._service_values(WITHOUT_VLAM, monkeypatch)

    def test_the_other_services_are_unaffected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The filter must remove exactly one card, not quietly thin the catalog."""
        with_vlam = set(self._service_values(WITH_VLAM, monkeypatch))
        without_vlam = set(self._service_values(WITHOUT_VLAM, monkeypatch))
        assert with_vlam - without_vlam == {ServiceType.VLAM.value}


class TestTheEnvironmentVariable:
    def _ctx(self, cluster: str = WITH_VLAM) -> ManifestContext:
        return ManifestContext(
            deployment_name="prod",
            project_data=_project(cluster=cluster),
            unique_name="prod-web",
            cluster=cluster,
            get_secret=lambda *args, **kwargs: None,
            component_def={"name": "web", "services": []},
        )

    def test_the_component_is_given_the_proxy_address(self) -> None:
        """Precies één variabele, zonder APP_-tweeling: de declaratie in variables.py
        is wat de e2e-probe-spec belooft en diens coverage-check meet, dus wat de
        dienst declareert en wat hij injecteert moeten hetzelfde zijn."""
        endpoint = vlam_endpoint(WITH_VLAM)
        assert endpoint is not None
        contribution = SERVICE.contribute_manifest_context(self._ctx())
        assert contribution.env_vars == {"VLAM_API_URL": endpoint.api_url}

    def test_it_is_not_an_envfrom_secret(self) -> None:
        """An in-cluster address is not a secret; encrypting it only hides it from its owner."""
        contribution = SERVICE.contribute_manifest_context(self._ctx())
        assert contribution.env_from_secrets == []
        assert contribution.secret_files == []

    def test_a_cluster_without_an_endpoint_contributes_nothing(self) -> None:
        """Generation never fails on a project that slipped past the validation."""
        assert SERVICE.contribute_manifest_context(self._ctx(WITHOUT_VLAM)) == ManifestContribution()


class TestTheContributionReachesTheComponent:
    """The seam that decides whether any of the above ends up in a pod."""

    def _ctx(self) -> ManifestContext:
        return ManifestContext(
            deployment_name="prod",
            project_data=_project(),
            unique_name="prod-web",
            cluster=WITH_VLAM,
            get_secret=lambda *args, **kwargs: None,
            component_def={"name": "web", "services": []},
        )

    def _env_vars_after_merge(self, *, component_services: list[str], project_services: list[str]) -> dict:
        variables: dict = {"env_vars": {"APP_ENV": "production"}}
        contributions = collect_manifest_contributions(
            self._ctx(), component_services=component_services, project_services=project_services
        )
        apply_manifest_contributions(variables, contributions)
        return variables["env_vars"]

    def test_the_project_selection_switches_it_on_without_any_component_ticking_it(self) -> None:
        """The whole point of manifest_activated_by_project: the component list is empty."""
        env_vars = self._env_vars_after_merge(component_services=[], project_services=[ServiceType.VLAM.value])
        assert env_vars["VLAM_API_URL"].startswith("http://")

    def test_a_project_without_the_service_gets_nothing(self) -> None:
        assert self._env_vars_after_merge(component_services=[], project_services=[]) == {"APP_ENV": "production"}

    def test_the_components_own_variables_survive(self) -> None:
        """Additive, not an override: a service adding one variable must not wipe the rest."""
        env_vars = self._env_vars_after_merge(component_services=[], project_services=[ServiceType.VLAM.value])
        assert env_vars["APP_ENV"] == "production"

    def test_a_component_ticking_it_is_not_what_switches_it_on(self) -> None:
        """Deployment-bound: the component list is not consulted for this service."""
        assert self._env_vars_after_merge(component_services=[ServiceType.VLAM.value], project_services=[]) == {
            "APP_ENV": "production"
        }

    def test_the_variable_is_rendered_into_the_container(self) -> None:
        """A merged dict is not a pod: measure the rendered Deployment."""
        variables = _golden_deployment_vars()
        apply_manifest_contributions(
            variables,
            collect_manifest_contributions(
                self._ctx(), component_services=[], project_services=[ServiceType.VLAM.value]
            ),
        )
        rendered = YAML().load(render_template("deployment.yaml.jinja", variables))
        env = {entry["name"]: entry["value"] for entry in rendered["spec"]["template"]["spec"]["containers"][0]["env"]}
        endpoint = vlam_endpoint(WITH_VLAM)
        assert endpoint is not None
        assert env["VLAM_API_URL"] == endpoint.api_url
        assert "APP_VLAM_API_URL" not in env
        assert env["APP_ENV"] == "production"


def _golden_deployment_vars() -> dict:
    """The template context of one component, borrowed from the golden harness."""
    from tests.test_golden_manifests import _deployment_vars

    return _deployment_vars(env_vars={"APP_ENV": "production"})


class TestTheNetworkPolicy:
    def _ctx(self, *, selected: bool = True, cluster: str = WITH_VLAM) -> DeploymentManifestContext:
        project = _project(selected=selected, cluster=cluster)
        return DeploymentManifestContext(
            project_name="myproject",
            project_data=project,
            deployment=project["deployments"][0],
            cluster=cluster,
            namespace="rig-prd-myproject",
        )

    def test_a_project_using_the_service_gets_one_egress_rule(self) -> None:
        specs = SERVICE.contribute_deployment_manifests(self._ctx())
        assert len(specs) == 1
        endpoint = vlam_endpoint(WITH_VLAM)
        assert endpoint is not None
        egress = specs[0].values["egress"]
        assert egress == [
            {"peer": {"namespace": endpoint.namespace, "pod_labels": endpoint.pod_labels}, "ports": [8081]}
        ]

    def test_it_opens_nothing_inbound(self) -> None:
        """One direction: the VLAM proxy never has to reach into a consumer's namespace."""
        assert SERVICE.contribute_deployment_manifests(self._ctx())[0].values["ingress"] == []

    def test_a_project_without_the_service_gets_nothing(self) -> None:
        """No file means the prune removes a stale one -- that is how switching off works."""
        assert SERVICE.contribute_deployment_manifests(self._ctx(selected=False)) == []

    def test_a_cluster_without_vlam_gets_nothing(self) -> None:
        assert SERVICE.contribute_deployment_manifests(self._ctx(cluster=WITHOUT_VLAM)) == []

    def test_the_filename_carries_the_prune_prefix(self) -> None:
        """``_prune_obsolete_service_manifests`` keys on '{deployment}-{service}-'; without
        this prefix the policy stays behind after the service is switched off."""
        specs = SERVICE.contribute_deployment_manifests(self._ctx())
        assert specs[0].filename.startswith(f"prod-{ServiceType.VLAM.value}-")

    def test_the_rendered_policy_opens_only_the_proxy_pod(self) -> None:
        """A rule on the whole namespace would open every workload the vlam project runs,
        the VPN passthrough on 8080 included."""
        specs = SERVICE.contribute_deployment_manifests(self._ctx())
        rendered = YAML().load(render_template(specs[0].template_path, specs[0].values))
        assert rendered["spec"]["policyTypes"] == ["Egress"]
        assert rendered["spec"]["podSelector"]["matchLabels"] == {"deployment": "prod", "project": "myproject"}
        rule = rendered["spec"]["egress"][0]
        peer = rule["to"][0]
        assert peer["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"] == "rig-prd-vlam-wt8"
        assert peer["podSelector"]["matchLabels"] == {
            "app": "productie-vlam-proxy-intern",
            "project": "vlam-wt8",
        }
        assert [port["port"] for port in rule["ports"]] == [8081]

    def test_the_rule_selects_every_pod_of_the_deployment(self) -> None:
        """Deployment-bound: one policy for the deployment, not one per component."""
        specs = SERVICE.contribute_deployment_manifests(self._ctx())
        assert len(specs) == 1
        assert "component" not in specs[0].values["pod_selector"]
