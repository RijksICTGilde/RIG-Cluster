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

RC-167 adds the DOORLUS to the same service, and with it a sixth: **the three things that
path needs are one unit.** The address of the passthrough port, the hosts entry that
points the VLAM name at our proxy, and the CA bundle to verify against are each useless
without the other two -- an address without the name fails on every connection, the name
without the issuer fails on an unknown CA. So a cluster offers all three or none, and the
tests at the bottom of this file measure exactly that.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opi.core.cluster_config import get_vlam_config
from opi.core.project_schema import ProjectIntegrityError
from opi.generation.manifests import render_template
from opi.manager.project_manager import apply_manifest_contributions, collect_manifest_contributions
from opi.manager.project_validation import validate_service_availability
from opi.services.catalog.base import DeploymentManifestContext, ManifestContext, ManifestContribution
from opi.services.catalog.vlam.endpoint import vlam_endpoint
from opi.services.registry import get_service
from opi.services.services_enums import ServiceBinding, ServiceType, UIEvent
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

    def test_it_hands_out_three_variables(self) -> None:
        """Een voor het getermineerde pad, twee voor de doorlus (RC-167)."""
        assert [var.name for var in SERVICE.definition.variables] == [
            "VLAM_API_URL",
            "VLAM_API_URL_DIRECT",
            "VLAM_CA_BUNDLE_PATH",
        ]

    def test_only_the_terminated_address_is_unconditional(self) -> None:
        """De twee doorlus-variabelen hangen aan het CLUSTER, niet aan de binding.

        Dat verschil staat in de declaratie omdat de e2e-probe erop oordeelt: hij eist dat
        elke variabele van een gebonden dienst in de pod staat, en zonder deze markering
        zou een cluster dat de doorlus niet aanbiedt dat als een provisioning-fout melden.
        """
        conditional = {var.name for var in SERVICE.definition.variables if var.conditional}
        assert conditional == {"VLAM_API_URL_DIRECT", "VLAM_CA_BUNDLE_PATH"}

    def test_it_sets_no_language_wide_ca_variables(self) -> None:
        """De valkuil van dit ontwerp, vastgelegd: die variabelen VERVANGEN de keten.

        Een pod met SSL_CERT_FILE op alleen deze bundel vertrouwt verder niets meer, en
        die storing lijkt in niets op zijn oorzaak. De dienst biedt een PAD aan; wat de
        applicatie ermee doet is aan de applicatie.
        """
        names = {var.name for var in SERVICE.definition.variables}
        assert names.isdisjoint({"REQUESTS_CA_BUNDLE", "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS", "CURL_CA_BUNDLE"})

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


# ---------------------------------------------------------------------------
# Het doorlus-pad (RC-167)
#
# Drie dingen, en ze zijn alle drie waardeloos zonder de andere twee: het ADRES van de
# doorlus-poort, de NAAM die naar onze proxy wijst (TLS vergelijkt de hostnaam uit de URL
# met het certificaat) en het CA-CERTIFICAAT in de pod (de afnemer verifieert nu zelf).
# Daarom toetsen deze tests ze als EEN geheel: een cluster biedt het pad aan of niet.
# ---------------------------------------------------------------------------

#: Geen echt certificaat en met opzet ook niet iets dat erop lijkt. Wat hier gemeten wordt
#: is dat de BYTES uit het bronbestand ongewijzigd in de pod aankomen, en daarvoor is de
#: inhoud onverschillig zolang hij meerdere regels heeft (de secret-sjabloon kiest op een
#: newline tussen een blokscalar en een gewone scalar).
_TEST_BUNDLE = "-----BEGIN CERTIFICATE-----\nVOORBEELD-GEEN-ECHT-CERTIFICAAT\nabc/def+ghi=\n-----END CERTIFICATE-----\n"


@pytest.fixture
def doorlus(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Een cluster dat het doorlus-pad WEL aanbiedt: de CA-bundel staat er.

    De bundel is een platformgegeven dat eenmalig geleverd wordt; welk bestand dat is zegt
    de clusterconfiguratie (``ca_bundle``), en waar de dienst het zoekt is zijn eigen map.
    Die map wijst deze fixture naar een tijdelijke, zodat de toets niet afhangt van wat er
    op dat moment in de repo ligt.
    """
    from opi.services.catalog.vlam import endpoint as endpoint_module

    config = get_vlam_config(WITH_VLAM)
    assert config is not None
    bundle = tmp_path / str(config["ca_bundle"])
    bundle.write_text(_TEST_BUNDLE)
    monkeypatch.setattr(endpoint_module, "CA_BUNDLE_DIR", tmp_path)
    return bundle


class TestDeDoorlusInDeConfiguratie:
    """Stap 1 en 2: het certificaat en het ClusterIP als platformgegeven vastgelegd."""

    def test_de_clusterconfiguratie_noemt_de_vier_waarden(self) -> None:
        config = get_vlam_config(WITH_VLAM)
        assert config is not None
        assert config["passthrough_port"] == 8443
        assert config["api_host"] == "vlam-api.rijksweb.nl"
        assert config["cluster_ip"] == "172.30.254.144"
        assert config["ca_bundle"] == "rijksdienst-ca.pem"

    def test_een_cluster_zonder_vlam_noemt_ook_geen_ca(self) -> None:
        """De doorlus hangt aan dezelfde sleutel, dus hij kan niet los blijven staan."""
        assert get_vlam_config(WITHOUT_VLAM) is None

    def test_het_geconfigureerde_adres_is_geen_servicenaam(self) -> None:
        """hostAliases neemt een IP-ADRES. Een naam erin zou stil niets doen."""
        import ipaddress

        config = get_vlam_config(WITH_VLAM)
        assert config is not None
        ipaddress.ip_address(config["cluster_ip"])


class TestDeDoorlusUitEenIngang:
    """Stap 3: drie dingen uit een configuratie-ingang, zodat ze niet uiteen lopen."""

    def test_het_endpoint_levert_de_doorlus_als_een_geheel(self, doorlus) -> None:
        endpoint = vlam_endpoint(WITH_VLAM)
        assert endpoint is not None
        passthrough = endpoint.passthrough
        assert passthrough is not None
        assert passthrough.api_url == "https://vlam-api.rijksweb.nl:8443"
        assert passthrough.host == "vlam-api.rijksweb.nl"
        assert passthrough.cluster_ip == "172.30.254.144"
        assert passthrough.port == 8443

    def test_de_naam_in_het_adres_is_de_naam_die_de_alias_zet(self, doorlus) -> None:
        """Anders valideert TLS een andere naam dan er in /etc/hosts staat."""
        endpoint = vlam_endpoint(WITH_VLAM)
        assert endpoint is not None
        assert endpoint.passthrough is not None
        assert endpoint.passthrough.host in endpoint.passthrough.api_url

    def test_het_getermineerde_adres_blijft_ongewijzigd(self, doorlus) -> None:
        """De doorlus komt ERNAAST; het bestaande pad verandert niet."""
        endpoint = vlam_endpoint(WITH_VLAM)
        assert endpoint is not None
        assert endpoint.api_url.startswith("http://")
        assert ":8081" in endpoint.api_url

    def test_het_mountpad_staat_niet_in_de_systeemmap(self, doorlus) -> None:
        """/etc/ssl/certs wordt door sommige runtimes vanzelf gelezen, en dan hangt het
        gedrag af van het basis-image in plaats van van wat de applicatie vraagt."""
        endpoint = vlam_endpoint(WITH_VLAM)
        assert endpoint is not None
        assert endpoint.passthrough is not None
        assert endpoint.passthrough.container_path == "/etc/ssl/vlam/rijksdienst-ca.pem"
        assert not endpoint.passthrough.container_path.startswith("/etc/ssl/certs")

    def test_zonder_bundel_geen_doorlus(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Een adres plus een alias zonder de uitgever levert 'unknown issuer' bij de
        afnemer op, met aan deze kant niets dat zegt waarom. Dan liever niets aanbieden."""
        from opi.services.catalog.vlam import endpoint as endpoint_module

        monkeypatch.setattr(endpoint_module, "CA_BUNDLE_DIR", tmp_path)
        endpoint = vlam_endpoint(WITH_VLAM)
        assert endpoint is not None
        assert endpoint.passthrough is None

    def test_zonder_bundel_blijft_het_getermineerde_pad_staan(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        """De doorlus valt weg, de dienst niet."""
        from opi.services.catalog.vlam import endpoint as endpoint_module

        monkeypatch.setattr(endpoint_module, "CA_BUNDLE_DIR", tmp_path)
        endpoint = vlam_endpoint(WITH_VLAM)
        assert endpoint is not None
        assert endpoint.api_url.endswith(":8081")

    def test_een_cluster_zonder_vlam_levert_nog_steeds_none(self, doorlus) -> None:
        assert vlam_endpoint(WITHOUT_VLAM) is None


class TestDeDoorlusVariabelen:
    """Stap 6: de twee variabelen komen ERBIJ, en alleen samen met de rest."""

    def _ctx(self, cluster: str = WITH_VLAM) -> ManifestContext:
        return ManifestContext(
            deployment_name="prod",
            project_data=_project(cluster=cluster),
            unique_name="prod-web",
            cluster=cluster,
            get_secret=lambda *args, **kwargs: None,
            component_def={"name": "web", "services": []},
        )

    def test_het_component_krijgt_alle_drie_de_adressen(self, doorlus) -> None:
        endpoint = vlam_endpoint(WITH_VLAM)
        assert endpoint is not None
        assert endpoint.passthrough is not None
        contribution = SERVICE.contribute_manifest_context(self._ctx())
        assert contribution.env_vars == {
            "VLAM_API_URL": endpoint.api_url,
            "VLAM_API_URL_DIRECT": endpoint.passthrough.api_url,
            "VLAM_CA_BUNDLE_PATH": endpoint.passthrough.container_path,
        }

    def test_zonder_doorlus_krijgt_het_component_alleen_het_bestaande_adres(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Niet twee derde van een pad: een pad naar een bestand dat niet bestaat is
        erger dan geen pad."""
        from opi.services.catalog.vlam import endpoint as endpoint_module

        monkeypatch.setattr(endpoint_module, "CA_BUNDLE_DIR", tmp_path)
        contribution = SERVICE.contribute_manifest_context(self._ctx())
        assert list(contribution.env_vars) == ["VLAM_API_URL"]
        assert contribution.template_vars == {}
        assert contribution.secret_mounts == []
        assert contribution.secret_files == []


class TestDeAliasOpDeDeployment:
    """Stap 4: hostAliases wijst de VLAM-naam naar het geconfigureerde adres."""

    def _ctx(self) -> ManifestContext:
        return ManifestContext(
            deployment_name="prod",
            project_data=_project(),
            unique_name="prod-web",
            cluster=WITH_VLAM,
            get_secret=lambda *args, **kwargs: None,
            component_def={"name": "web", "services": []},
        )

    def _rendered(self, *, project_services: list[str]) -> dict:
        variables = _golden_deployment_vars()
        apply_manifest_contributions(
            variables,
            collect_manifest_contributions(self._ctx(), component_services=[], project_services=project_services),
        )
        return YAML().load(render_template("deployment.yaml.jinja", variables))

    def test_de_pod_krijgt_de_regel_in_etc_hosts(self, doorlus) -> None:
        pod_spec = self._rendered(project_services=[ServiceType.VLAM.value])["spec"]["template"]["spec"]
        assert pod_spec["hostAliases"] == [{"ip": "172.30.254.144", "hostnames": ["vlam-api.rijksweb.nl"]}]

    def test_de_naam_in_de_regel_is_de_naam_uit_het_doorlus_adres(self, doorlus) -> None:
        """Anders wijst /etc/hosts een andere naam aan dan de URL gebruikt en doet de
        alias niets, terwijl alles er goed uitziet."""
        rendered = self._rendered(project_services=[ServiceType.VLAM.value])
        container = rendered["spec"]["template"]["spec"]["containers"][0]
        env = {entry["name"]: entry["value"] for entry in container["env"]}
        alias = rendered["spec"]["template"]["spec"]["hostAliases"][0]
        assert alias["hostnames"][0] in env["VLAM_API_URL_DIRECT"]

    def test_een_component_zonder_de_dienst_krijgt_geen_blok(self, doorlus) -> None:
        pod_spec = self._rendered(project_services=[])["spec"]["template"]["spec"]
        assert "hostAliases" not in pod_spec

    def test_zonder_doorlus_geen_blok(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        from opi.services.catalog.vlam import endpoint as endpoint_module

        monkeypatch.setattr(endpoint_module, "CA_BUNDLE_DIR", tmp_path)
        pod_spec = self._rendered(project_services=[ServiceType.VLAM.value])["spec"]["template"]["spec"]
        assert "hostAliases" not in pod_spec


class TestDeGemounteCaBundel:
    """Stap 5: Secret, volume en volumeMount langs de weg van attachment_secret_mounts."""

    def _ctx(self) -> ManifestContext:
        return ManifestContext(
            deployment_name="prod",
            project_data=_project(),
            unique_name="prod-web",
            cluster=WITH_VLAM,
            get_secret=lambda *args, **kwargs: None,
            component_def={"name": "web", "services": []},
        )

    def _rendered(self, *, own_mounts: list[dict] | None = None) -> dict:
        variables = _golden_deployment_vars()
        variables["attachment_secret_mounts"] = list(own_mounts or [])
        apply_manifest_contributions(
            variables,
            collect_manifest_contributions(
                self._ctx(), component_services=[], project_services=[ServiceType.VLAM.value]
            ),
        )
        return YAML().load(render_template("deployment.yaml.jinja", variables))

    def test_de_bundel_wordt_als_bestand_gemount(self, doorlus) -> None:
        container = self._rendered()["spec"]["template"]["spec"]["containers"][0]
        mounts = {mount["name"]: mount for mount in container["volumeMounts"]}
        assert mounts["vlam-ca"]["mountPath"] == "/etc/ssl/vlam/rijksdienst-ca.pem"
        assert mounts["vlam-ca"]["subPath"] == "rijksdienst-ca.pem"
        assert mounts["vlam-ca"]["readOnly"] is True

    def test_er_staat_een_volume_bij_dat_de_secret_noemt(self, doorlus) -> None:
        volumes = {volume["name"]: volume for volume in self._rendered()["spec"]["template"]["spec"]["volumes"]}
        assert volumes["vlam-ca"]["secret"]["secretName"] == "prod-vlam-ca"

    def test_het_gemounte_pad_is_het_pad_in_de_variabele(self, doorlus) -> None:
        """De applicatie leest VLAM_CA_BUNDLE_PATH; wijst die ergens anders heen dan de
        mount, dan is er een bestand en een pad en komen ze niet bij elkaar."""
        rendered = self._rendered()
        container = rendered["spec"]["template"]["spec"]["containers"][0]
        env = {entry["name"]: entry["value"] for entry in container["env"]}
        mount = next(m for m in container["volumeMounts"] if m["name"] == "vlam-ca")
        assert env["VLAM_CA_BUNDLE_PATH"] == mount["mountPath"]

    def test_de_eigen_bijlagen_van_het_component_blijven_staan(self, doorlus) -> None:
        """Additief, geen override: een dienst die een bestand meelevert mag de bestanden
        die het component zelf uploadde niet wegduwen."""
        own = {
            "name": "attch-eigen",
            "secret_name": "prod-attch-eigen",
            "mount_path": "/data/eigen.pem",
            "sub_path": "eigen",
        }
        container = self._rendered(own_mounts=[own])["spec"]["template"]["spec"]["containers"][0]
        names = [mount["name"] for mount in container["volumeMounts"]]
        assert "attch-eigen" in names
        assert "vlam-ca" in names

    def test_de_secret_draagt_de_bytes_van_het_bronbestand(self, doorlus) -> None:
        specs = SERVICE.build_secret_files(self._ctx())
        assert len(specs) == 1
        assert specs[0].secret_name == "prod-vlam-ca"
        rendered = YAML().load(
            render_template(
                "generic-secret.yaml.to-sops.jinja",
                {
                    "name": specs[0].secret_name,
                    "namespace": "rig-prd-myproject",
                    "secret_pairs": specs[0].secret_pairs,
                    "secret_labels": None,
                },
            )
        )
        assert rendered["stringData"]["rijksdienst-ca.pem"] == doorlus.read_text()

    def test_de_secret_reist_mee_met_de_bijdrage(self, doorlus) -> None:
        """De schrijver leest ``contribution.secret_files``; staat de spec daar niet in,
        dan wordt er een volume gemount dat naar een Secret wijst die niemand schrijft."""
        contribution = SERVICE.contribute_manifest_context(self._ctx())
        assert [spec.secret_name for spec in contribution.secret_files] == ["prod-vlam-ca"]
        assert [mount["secret_name"] for mount in contribution.secret_mounts] == ["prod-vlam-ca"]

    def test_zonder_doorlus_geen_secret_en_geen_mount(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        from opi.services.catalog.vlam import endpoint as endpoint_module

        monkeypatch.setattr(endpoint_module, "CA_BUNDLE_DIR", tmp_path)
        assert SERVICE.build_secret_files(self._ctx()) == []
        container = self._rendered()["spec"]["template"]["spec"]["containers"][0]
        assert "volumeMounts" not in container


class TestDeNetwerkregelOpentBeidePoorten:
    """De doorlus zit op een TWEEDE poort van dezelfde pod. Alleen 8081 openen levert een
    adres op dat de afnemer krijgt en niet kan bereiken -- de time-out die deze dienst
    juist wil voorkomen."""

    def _ctx(self, cluster: str = WITH_VLAM) -> DeploymentManifestContext:
        project = _project(cluster=cluster)
        return DeploymentManifestContext(
            project_name="myproject",
            project_data=project,
            deployment=project["deployments"][0],
            cluster=cluster,
            namespace="rig-prd-myproject",
        )

    def test_beide_poorten_staan_in_de_regel(self, doorlus) -> None:
        specs = SERVICE.contribute_deployment_manifests(self._ctx())
        assert specs[0].values["egress"][0]["ports"] == [8081, 8443]

    def test_de_gerenderde_regel_opent_beide(self, doorlus) -> None:
        specs = SERVICE.contribute_deployment_manifests(self._ctx())
        rendered = YAML().load(render_template(specs[0].template_path, specs[0].values))
        rule = rendered["spec"]["egress"][0]
        assert [port["port"] for port in rule["ports"]] == [8081, 8443]
        assert rule["to"][0]["podSelector"]["matchLabels"]["app"] == "productie-vlam-proxy-intern"

    def test_zonder_doorlus_blijft_het_bij_een_poort(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        from opi.services.catalog.vlam import endpoint as endpoint_module

        monkeypatch.setattr(endpoint_module, "CA_BUNDLE_DIR", tmp_path)
        specs = SERVICE.contribute_deployment_manifests(self._ctx())
        assert specs[0].values["egress"][0]["ports"] == [8081]


class TestDeDownloadknop:
    """Stap 7: de bundel is te downloaden bij het dienstblok.

    Van de drie dingen die de dienst neerzet is het BESTAND het enige dat je zonder de pod
    niet kunt bekijken, en het is het ding dat het vaakst verkeerd begrepen wordt. Wie het
    pad lokaal wil proberen, of wil zien wat zijn pod vertrouwt, haalt het hier op.
    """

    @pytest.fixture
    def client(self, monkeypatch: pytest.MonkeyPatch) -> TestClient:
        """De route zonder de rest van de applicatie: genoeg om te meten wat hij teruggeeft.

        Het cluster van deze instantie is dat met VLAM -- de route kent geen project en
        leest dus, net als het blok, ``settings.CLUSTER_MANAGER``.
        """
        from opi.core.config import settings
        from opi.services.catalog.vlam.routes import vlam_router

        monkeypatch.setattr(settings, "CLUSTER_MANAGER", WITH_VLAM)
        app = FastAPI()
        app.include_router(vlam_router)
        return TestClient(app)

    def test_hij_zit_achter_de_login(self) -> None:
        """Er valt niets te lekken aan een publiek CA-certificaat, maar er is ook geen
        reden waarom een route van deze app anders zou werken dan alle andere."""
        from opi.services.catalog.vlam.routes import vlam_ca_bundle

        assert getattr(vlam_ca_bundle, "_requires_sso", False) is True

    def test_de_bundel_komt_er_byte_voor_byte_uit(self, client: TestClient, doorlus) -> None:
        """Dezelfde bron als de gemounte Secret: wat je downloadt en wat je pod
        verifieert mogen niet twee verschillende bestanden zijn."""
        response = client.get("/services/vlam/ca-bundle")
        assert response.status_code == 200
        assert response.content == doorlus.read_bytes()

    def test_hij_komt_binnen_als_bestand_en_niet_als_pagina(self, client: TestClient, doorlus) -> None:
        response = client.get("/services/vlam/ca-bundle")
        assert response.headers["content-type"].startswith("application/x-pem-file")
        assert 'filename="rijksdienst-ca.pem"' in response.headers["content-disposition"]

    def test_een_cluster_zonder_doorlus_heeft_niets_te_downloaden(
        self, client: TestClient, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from opi.services.catalog.vlam import endpoint as endpoint_module

        monkeypatch.setattr(endpoint_module, "CA_BUNDLE_DIR", tmp_path)
        assert client.get("/services/vlam/ca-bundle").status_code == 404


class TestHetBlokOpDeProjectpagina:
    """Het blok bestaat alleen waar er iets te melden is."""

    @pytest.fixture
    def op_het_vlam_cluster(self, monkeypatch: pytest.MonkeyPatch):
        from opi.core.config import settings

        monkeypatch.setattr(settings, "CLUSTER_MANAGER", WITH_VLAM)

    def _sections(self) -> list:
        from opi.services.catalog.base import ProjectPageContext

        return SERVICE.handle_ui(
            UIEvent.PROJECT_SECTIONS, ProjectPageContext(project_data=_project(), user_role="admin")
        )

    def test_het_blok_noemt_beide_adressen_en_het_pad(self, doorlus, op_het_vlam_cluster) -> None:
        sections = self._sections()
        assert len(sections) == 1
        context = sections[0].context
        assert context["api_url"].startswith("http://")
        assert context["direct_url"] == "https://vlam-api.rijksweb.nl:8443"
        assert context["ca_path"] == "/etc/ssl/vlam/rijksdienst-ca.pem"

    def test_de_knop_wijst_naar_het_endpoint_dat_bestaat(self, doorlus, op_het_vlam_cluster) -> None:
        """Een dode knop ziet er precies zo uit als een levende."""
        from opi.services.catalog.vlam.routes import vlam_router

        context = self._sections()[0].context
        assert context["ca_download_url"] in [route.path for route in vlam_router.routes]

    def test_zonder_doorlus_geen_blok(self, tmp_path, monkeypatch: pytest.MonkeyPatch, op_het_vlam_cluster) -> None:
        from opi.services.catalog.vlam import endpoint as endpoint_module

        monkeypatch.setattr(endpoint_module, "CA_BUNDLE_DIR", tmp_path)
        assert self._sections() == []
