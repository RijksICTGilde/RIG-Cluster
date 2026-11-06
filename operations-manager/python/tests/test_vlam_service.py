"""Tests for the ``vlam`` service (RC-142).

Five things carry the design, and each one is a way this could fail silently:

1. **The address and the network rule come from ONE configuration entry.** An address
   naming one pod while the rule opens another presents itself as a timeout at the
   consumer, days after the change.
2. **Availability is refused at the SAVE path, not only on the wizard card.** The API and
   a hand-written project file never see a card.
3. **The component's own tick is what switches the contribution on** (RC-213). Until
   then the PROJECT's selection did, and every component of every deployment carried the
   address and the egress rule, including the ones whose owner never asked for them.
4. **The variable is ADDED to the component's own variables**, not put in their place.
5. **Switching the service off removes the policy**, which is the prune prefix on the
   filename plus the service returning nothing.

RC-167 adds the DOORLUS to the same service, and with it a sixth: **the two things that
path needs are one unit.** The address of the passthrough port and the hosts entry that
points the VLAM name at our proxy are each useless without the other -- an address without
the name fails on every connection. So a cluster offers both or neither, and the tests at
the bottom of this file measure exactly that.

RC-414 removes the third thing that path used to need. VLAM moved from
``vlam-api.rijksweb.nl`` to ``vlam-api.overheid-i.nl``, whose certificate comes from
DigiCert, so the consumer verifies it against the standard trust store of its own runtime
and the service delivers no CA material at all.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opi.core.cluster_config import get_vlam_config
from opi.core.project_schema import ProjectIntegrityError
from opi.generation.manifests import ManifestGenerator, render_template
from opi.manager.project_manager import (
    ProjectManager,
    apply_manifest_contributions,
    collect_manifest_contributions,
)
from opi.manager.project_validation import validate_service_availability
from opi.services.catalog.base import DeploymentManifestContext, ManifestContext, ManifestContribution
from opi.services.catalog.vlam.endpoint import vlam_endpoint
from opi.services.registry import collect_service_routers, get_service
from opi.services.services_enums import ServiceType, UIEvent
from opi.utils.naming import generate_unique_name
from opi.utils.sops import sops_filenames
from ruamel.yaml import YAML

SERVICE = get_service(ServiceType.VLAM)

#: The cluster that has VLAM, and one that does not. Read from the configuration rather
#: than assumed, so this file says the same thing the code does.
WITH_VLAM = "odcn-production"
WITHOUT_VLAM = "local"


def _project(*, selected: bool = True, cluster: str = WITH_VLAM) -> dict:
    """A project with one deployment of two components; only ``web`` takes vlam.

    Two components rather than one, because the whole question this service moved on is
    what the OTHER component gets. ``selected=False`` is a project that took the service
    nowhere.
    """
    return {
        "name": "myproject",
        "services": ([{"name": ServiceType.VLAM.value}] if selected else []),
        "components": [
            {"name": "web", "services": ([ServiceType.VLAM.value] if selected else [])},
            {"name": "worker", "services": []},
        ],
        "deployments": [
            {
                "name": "prod",
                "cluster": cluster,
                "namespace": "myproject",
                "components": [{"reference": "web"}, {"reference": "worker"}],
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

    def test_a_component_switches_it_on_for_itself(self) -> None:
        """Toegang hoort per component: de standaard, en nu ook echt zo uitgevoerd."""
        assert SERVICE.definition.selectable_per_component is True

    def test_it_carries_no_config_at_all(self) -> None:
        assert SERVICE.config_model is None
        assert SERVICE.config_layers() == []

    def test_it_hands_out_two_variables(self) -> None:
        """Een voor het getermineerde pad, een voor de doorlus (RC-167)."""
        assert [var.name for var in SERVICE.definition.variables] == [
            "VLAM_API_URL",
            "VLAM_API_URL_DIRECT",
        ]

    def test_only_the_terminated_address_is_unconditional(self) -> None:
        """De doorlus-variabele hangt aan het CLUSTER, niet aan de selectie.

        Dat verschil staat in de declaratie omdat de e2e-probe erop oordeelt: hij eist dat
        elke variabele van een gebonden dienst in de pod staat, en zonder deze markering
        zou een cluster dat de doorlus niet aanbiedt dat als een provisioning-fout melden.
        """
        conditional = {var.name for var in SERVICE.definition.variables if var.conditional}
        assert conditional == {"VLAM_API_URL_DIRECT"}

    def test_it_sets_no_language_wide_ca_variables(self) -> None:
        """De valkuil van het oude ontwerp, vastgelegd: die variabelen VERVANGEN de keten.

        Een pod met SSL_CERT_FILE op een losse bundel vertrouwt verder niets meer, en die
        storing lijkt in niets op zijn oorzaak. Sinds RC-414 levert de dienst geen bundel
        meer, en dan is dit de toets die voorkomt dat er ooit weer een komt.
        """
        names = {var.name for var in SERVICE.definition.variables}
        assert names.isdisjoint({"REQUESTS_CA_BUNDLE", "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS", "CURL_CA_BUNDLE"})

    def test_ticking_it_somewhere_enrols_it_at_project_level(self) -> None:
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
        """The filter must remove exactly one card, not quietly thin the catalog.

        On ONE cluster with only the VLAM configuration taken away. Comparing two
        clusters put every other cluster-bound service (seaweedfs) in the difference too.
        """
        with_vlam = set(self._service_values(WITH_VLAM, monkeypatch))
        monkeypatch.setattr("opi.services.catalog.vlam.endpoint.get_vlam_config", lambda cluster: None)
        without_vlam = set(self._service_values(WITH_VLAM, monkeypatch))

        assert with_vlam - without_vlam == {ServiceType.VLAM.value}
        assert not without_vlam - with_vlam


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
        """Zonder APP_-tweeling: de declaratie in variables.py is wat de e2e-probe-spec
        belooft en diens coverage-check meet, dus wat de dienst declareert en wat hij
        injecteert moeten hetzelfde zijn.

        Op een cluster mét doorlus zijn dat er twee: het getermineerde adres en het
        doorlus-adres. Dat tweede staat in variables.py als ``conditional`` en hoort er op
        zo'n cluster dus bij."""
        endpoint = vlam_endpoint(WITH_VLAM)
        assert endpoint is not None
        assert endpoint.passthrough is not None
        contribution = SERVICE.contribute_manifest_context(self._ctx())
        assert contribution.env_vars == {
            "VLAM_API_URL": endpoint.api_url,
            "VLAM_API_URL_DIRECT": endpoint.passthrough.api_url,
        }

    def test_it_is_not_an_envfrom_secret(self) -> None:
        """An in-cluster address is not a secret; encrypting it only hides it from its owner.

        Sinds RC-414 levert de dienst ook geen bestand meer, dus er is helemaal geen
        Secret: beide waarden zijn gewone env-vars."""
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

    def test_the_component_that_ticked_it_gets_the_address(self) -> None:
        env_vars = self._env_vars_after_merge(
            component_services=[ServiceType.VLAM.value], project_services=[ServiceType.VLAM.value]
        )
        assert env_vars["VLAM_API_URL"].startswith("http://")

    def test_a_project_without_the_service_gets_nothing(self) -> None:
        assert self._env_vars_after_merge(component_services=[], project_services=[]) == {"APP_ENV": "production"}

    def test_the_components_own_variables_survive(self) -> None:
        """Additive, not an override: a service adding one variable must not wipe the rest."""
        env_vars = self._env_vars_after_merge(
            component_services=[ServiceType.VLAM.value], project_services=[ServiceType.VLAM.value]
        )
        assert env_vars["APP_ENV"] == "production"

    def test_the_project_selection_alone_gives_a_component_nothing(self) -> None:
        assert self._env_vars_after_merge(component_services=[], project_services=[ServiceType.VLAM.value]) == {
            "APP_ENV": "production"
        }

    def test_the_variable_is_rendered_into_the_container(self) -> None:
        """A merged dict is not a pod: measure the rendered Deployment."""
        variables = _golden_deployment_vars()
        apply_manifest_contributions(
            variables,
            collect_manifest_contributions(
                self._ctx(),
                component_services=[ServiceType.VLAM.value],
                project_services=[ServiceType.VLAM.value],
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

    def test_a_component_using_the_service_gets_one_egress_rule(self) -> None:
        specs = SERVICE.contribute_deployment_manifests(self._ctx())
        assert len(specs) == 1
        endpoint = vlam_endpoint(WITH_VLAM)
        assert endpoint is not None
        egress = specs[0].values["egress"]
        # De poorten uit het endpoint en niet als losse lijst: welke er in horen hangt
        # ervan af of het cluster de doorlus aanbiedt, en dat legt
        # TestDeNetwerkregelOpentBeidePoorten apart vast. Hier gaat het om de ENE regel.
        assert egress == [
            {"peer": {"namespace": endpoint.namespace, "pod_labels": endpoint.pod_labels}, "ports": endpoint.ports}
        ]

    def test_it_opens_nothing_inbound(self) -> None:
        """One direction: the VLAM proxy never has to reach into a consumer's namespace."""
        assert SERVICE.contribute_deployment_manifests(self._ctx())[0].values["ingress"] == []

    def test_a_project_without_the_service_gets_nothing(self) -> None:
        """No file means the prune removes a stale one -- that is how switching off works."""
        assert SERVICE.contribute_deployment_manifests(self._ctx(selected=False)) == []

    def test_only_the_component_that_ticked_it_gets_a_policy(self) -> None:
        """RC-213: de vorige regel selecteerde de hele deployment, dus ook ``worker``.

        Gemeten in productie op ``bouwm-6gn``: ``main-component-1`` vinkte vlam niet aan
        en viel toch onder ``vlam-main-network-policy``.
        """
        specs = SERVICE.contribute_deployment_manifests(self._ctx())
        assert [spec.values["pod_selector"] for spec in specs] == [{"app": generate_unique_name("prod", "web")}]

    def test_every_ticking_component_gets_its_own_policy(self) -> None:
        ctx = self._ctx()
        ctx.project_data["components"][1]["services"] = [ServiceType.VLAM.value]
        specs = SERVICE.contribute_deployment_manifests(ctx)
        assert [spec.filename for spec in specs] == [
            "prod-vlam-web-network-policy",
            "prod-vlam-worker-network-policy",
        ]

    def test_a_component_outside_this_deployment_is_not_counted(self) -> None:
        """De componentlijst van het PROJECT, begrensd op wat deze deployment uitrolt."""
        ctx = self._ctx()
        ctx.project_data["components"].append({"name": "elders", "services": [ServiceType.VLAM.value]})
        specs = SERVICE.contribute_deployment_manifests(ctx)
        assert [spec.filename for spec in specs] == ["prod-vlam-web-network-policy"]

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
        endpoint = vlam_endpoint(WITH_VLAM)
        assert endpoint is not None
        assert rendered["spec"]["policyTypes"] == ["Egress"]
        assert rendered["spec"]["podSelector"]["matchLabels"] == {"app": generate_unique_name("prod", "web")}
        rule = rendered["spec"]["egress"][0]
        peer = rule["to"][0]
        assert peer["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"] == "rig-prd-vlam-wt8"
        assert peer["podSelector"]["matchLabels"] == {
            "app": "productie-vlam-proxy-intern",
            "project": "vlam-wt8",
        }
        assert [port["port"] for port in rule["ports"]] == endpoint.ports

    def test_the_filename_names_the_component(self) -> None:
        """Dezelfde vorm als send-email, zodat de prune per component werkt en de oude
        ``prod-vlam-network-policy`` vanzelf verdwijnt."""
        specs = SERVICE.contribute_deployment_manifests(self._ctx())
        assert specs[0].filename == "prod-vlam-web-network-policy"


# ---------------------------------------------------------------------------
# Het doorlus-pad (RC-167, zonder eigen CA sinds RC-414)
#
# Twee dingen, en ze zijn beide waardeloos zonder het andere: het ADRES van de doorlus-
# poort en de NAAM die naar onze proxy wijst (TLS vergelijkt de hostnaam uit de URL met
# het certificaat). Daarom toetsen deze tests ze als EEN geheel: een cluster biedt het pad
# aan of niet. Een derde ding, een eigen CA-bundel, hoorde er tot RC-414 bij; het
# certificaat van `vlam-api.overheid-i.nl` komt van DigiCert en is publiek vertrouwd, dus
# de afnemer verifieert met de certificatenlijst van zijn eigen image.
# ---------------------------------------------------------------------------

#: De sleutels die samen de doorlus vormen. Het ontbreken van elk van de drie laat het pad
#: wegvallen, en die drie gevallen zijn even normaal als aanwezigheid.
DOORLUS_KEYS = ("api_host", "cluster_ip", "passthrough_port")


def _config_zonder(*keys: str) -> dict:
    """De productie-``vlam``-ingang met deze sleutels eruit."""
    config = get_vlam_config(WITH_VLAM)
    assert config is not None
    return {key: value for key, value in config.items() if key not in keys}


@pytest.fixture
def zonder_doorlus(monkeypatch: pytest.MonkeyPatch):
    """Een cluster met VLAM maar zonder doorlus: de proxy heeft geen passthrough-poort.

    Gepatcht op de configuratie en niet op een bestand: sinds RC-414 is de hele vraag of
    de ``vlam``-ingang de doorlus-sleutels noemt.
    """
    from opi.services.catalog.vlam import endpoint as endpoint_module

    monkeypatch.setattr(endpoint_module, "get_vlam_config", lambda cluster: _config_zonder(*DOORLUS_KEYS))


class TestDeDoorlusInDeConfiguratie:
    """Het ClusterIP en de poort als platformgegeven vastgelegd."""

    def test_de_clusterconfiguratie_noemt_de_drie_waarden(self) -> None:
        config = get_vlam_config(WITH_VLAM)
        assert config is not None
        assert config["passthrough_port"] == 8443
        assert config["api_host"] == "vlam-api.overheid-i.nl"
        assert config["cluster_ip"] == "172.30.254.144"

    def test_er_staat_geen_ca_bundel_meer_in(self) -> None:
        """RC-414: het certificaat van VLAM is publiek vertrouwd, dus er valt niets te
        leveren. Een achtergebleven sleutel zou een bestand noemen dat niet bestaat."""
        config = get_vlam_config(WITH_VLAM)
        assert config is not None
        assert "ca_bundle" not in config

    def test_een_cluster_zonder_vlam_noemt_ook_geen_doorlus(self) -> None:
        """De doorlus hangt aan dezelfde sleutel, dus hij kan niet los blijven staan."""
        assert get_vlam_config(WITHOUT_VLAM) is None

    def test_het_geconfigureerde_adres_is_geen_servicenaam(self) -> None:
        """hostAliases neemt een IP-ADRES. Een naam erin zou stil niets doen."""
        import ipaddress

        config = get_vlam_config(WITH_VLAM)
        assert config is not None
        ipaddress.ip_address(config["cluster_ip"])


class TestDeDoorlusUitEenIngang:
    """Twee dingen uit een configuratie-ingang, zodat ze niet uiteen lopen."""

    def test_het_endpoint_levert_de_doorlus_als_een_geheel(self) -> None:
        endpoint = vlam_endpoint(WITH_VLAM)
        assert endpoint is not None
        passthrough = endpoint.passthrough
        assert passthrough is not None
        assert passthrough.api_url == "https://vlam-api.overheid-i.nl:8443"
        assert passthrough.host == "vlam-api.overheid-i.nl"
        assert passthrough.cluster_ip == "172.30.254.144"
        assert passthrough.port == 8443

    def test_de_naam_in_het_adres_is_de_naam_die_de_alias_zet(self) -> None:
        """Anders valideert TLS een andere naam dan er in /etc/hosts staat."""
        endpoint = vlam_endpoint(WITH_VLAM)
        assert endpoint is not None
        assert endpoint.passthrough is not None
        assert endpoint.passthrough.host in endpoint.passthrough.api_url

    def test_het_getermineerde_adres_blijft_ongewijzigd(self) -> None:
        """De doorlus komt ERNAAST; het bestaande pad verandert niet."""
        endpoint = vlam_endpoint(WITH_VLAM)
        assert endpoint is not None
        assert endpoint.api_url.startswith("http://")
        assert ":8082" in endpoint.api_url

    def test_de_drie_sleutels_zijn_genoeg(self) -> None:
        """Geen vierde voorwaarde meer: wat de configuratie noemt, levert het pad."""
        from opi.services.catalog.vlam.endpoint import _passthrough

        config = {key: value for key, value in _config_zonder().items() if key in DOORLUS_KEYS}
        passthrough = _passthrough(config)
        assert passthrough is not None
        assert passthrough.api_url == "https://vlam-api.overheid-i.nl:8443"

    @pytest.mark.parametrize("ontbrekend", DOORLUS_KEYS)
    def test_zonder_een_van_de_drie_geen_doorlus(self, ontbrekend: str) -> None:
        """Niet de helft van een pad: een adres zonder de naam faalt op elke verbinding,
        en de naam zonder adres wijst nergens heen."""
        from opi.services.catalog.vlam.endpoint import _passthrough

        assert _passthrough(_config_zonder(ontbrekend)) is None

    def test_zonder_doorlus_blijft_het_getermineerde_pad_staan(self, zonder_doorlus) -> None:
        """De doorlus valt weg, de dienst niet."""
        endpoint = vlam_endpoint(WITH_VLAM)
        assert endpoint is not None
        assert endpoint.passthrough is None
        assert endpoint.api_url.endswith(":8082")

    def test_een_cluster_zonder_vlam_levert_nog_steeds_none(self) -> None:
        assert vlam_endpoint(WITHOUT_VLAM) is None


class TestDeDoorlusVariabelen:
    """De doorlus-variabele komt ERBIJ, en alleen samen met de rest."""

    def _ctx(self, cluster: str = WITH_VLAM) -> ManifestContext:
        return ManifestContext(
            deployment_name="prod",
            project_data=_project(cluster=cluster),
            unique_name="prod-web",
            cluster=cluster,
            get_secret=lambda *args, **kwargs: None,
            component_def={"name": "web", "services": []},
        )

    def test_het_component_krijgt_beide_adressen(self) -> None:
        endpoint = vlam_endpoint(WITH_VLAM)
        assert endpoint is not None
        assert endpoint.passthrough is not None
        contribution = SERVICE.contribute_manifest_context(self._ctx())
        assert contribution.env_vars == {
            "VLAM_API_URL": endpoint.api_url,
            "VLAM_API_URL_DIRECT": endpoint.passthrough.api_url,
        }

    def test_er_reist_geen_bestand_en_geen_secret_mee(self) -> None:
        """RC-414: geen Secret, geen mount. Een volume dat naar een Secret wijst die
        niemand schrijft, laat de pod in ContainerCreating hangen."""
        contribution = SERVICE.contribute_manifest_context(self._ctx())
        assert contribution.secret_files == []

    def test_zonder_doorlus_krijgt_het_component_alleen_het_bestaande_adres(self, zonder_doorlus) -> None:
        contribution = SERVICE.contribute_manifest_context(self._ctx())
        assert list(contribution.env_vars) == ["VLAM_API_URL"]
        assert contribution.template_vars == {}
        assert contribution.secret_files == []


class TestDeAliasOpDeDeployment:
    """hostAliases wijst de VLAM-naam naar het geconfigureerde adres."""

    def _ctx(self) -> ManifestContext:
        return ManifestContext(
            deployment_name="prod",
            project_data=_project(),
            unique_name="prod-web",
            cluster=WITH_VLAM,
            get_secret=lambda *args, **kwargs: None,
            component_def={"name": "web", "services": []},
        )

    def _rendered(self, *, component_services: list[str], own_mounts: list[dict] | None = None) -> dict:
        variables = _golden_deployment_vars()
        variables["attachment_secret_mounts"] = list(own_mounts or [])
        apply_manifest_contributions(
            variables,
            collect_manifest_contributions(
                self._ctx(),
                component_services=component_services,
                project_services=[ServiceType.VLAM.value],
            ),
        )
        return YAML().load(render_template("deployment.yaml.jinja", variables))

    def test_de_pod_krijgt_de_regel_in_etc_hosts(self) -> None:
        pod_spec = self._rendered(component_services=[ServiceType.VLAM.value])["spec"]["template"]["spec"]
        assert pod_spec["hostAliases"] == [{"ip": "172.30.254.144", "hostnames": ["vlam-api.overheid-i.nl"]}]

    def test_de_naam_in_de_regel_is_de_naam_uit_het_doorlus_adres(self) -> None:
        """Anders wijst /etc/hosts een andere naam aan dan de URL gebruikt en doet de
        alias niets, terwijl alles er goed uitziet."""
        rendered = self._rendered(component_services=[ServiceType.VLAM.value])
        container = rendered["spec"]["template"]["spec"]["containers"][0]
        env = {entry["name"]: entry["value"] for entry in container["env"]}
        alias = rendered["spec"]["template"]["spec"]["hostAliases"][0]
        assert alias["hostnames"][0] in env["VLAM_API_URL_DIRECT"]

    def test_een_component_zonder_de_dienst_krijgt_geen_blok(self) -> None:
        pod_spec = self._rendered(component_services=[])["spec"]["template"]["spec"]
        assert "hostAliases" not in pod_spec

    def test_zonder_doorlus_geen_blok(self, zonder_doorlus) -> None:
        pod_spec = self._rendered(component_services=[ServiceType.VLAM.value])["spec"]["template"]["spec"]
        assert "hostAliases" not in pod_spec

    def test_de_gerenderde_pod_mount_geen_ca_bundel(self) -> None:
        """RC-414 op het gerenderde manifest: het adres en de alias blijven, het volume en
        de volumeMount ``vlam-ca`` zijn weg."""
        rendered = self._rendered(component_services=[ServiceType.VLAM.value])
        pod_spec = rendered["spec"]["template"]["spec"]
        container = pod_spec["containers"][0]
        env = {entry["name"]: entry["value"] for entry in container["env"]}
        assert env["VLAM_API_URL_DIRECT"] == "https://vlam-api.overheid-i.nl:8443"
        assert pod_spec["hostAliases"]
        assert "vlam-ca" not in [mount["name"] for mount in container.get("volumeMounts", [])]
        assert "vlam-ca" not in [volume["name"] for volume in pod_spec.get("volumes", [])]

    def test_de_eigen_bijlagen_van_het_component_blijven_staan(self) -> None:
        """De dienst levert zelf geen bestand meer, maar mag de bestanden die het
        component zelf uploadde ook niet wegduwen."""
        own = {
            "name": "attch-eigen",
            "secret_name": "prod-attch-eigen",
            "mount_path": "/data/eigen.pem",
            "sub_path": "eigen",
        }
        container = self._rendered(component_services=[ServiceType.VLAM.value], own_mounts=[own])["spec"]["template"][
            "spec"
        ]["containers"][0]
        assert [mount["name"] for mount in container["volumeMounts"]] == ["attch-eigen"]


class TestDeOudeCaSecretWordtOpgeruimd:
    """De tien productie-deployments die de bundel nog hebben, raken hem bij de eerstvolgende
    reprocess kwijt. Het Secret-bestand heet ``<deployment>-vlam-ca-secret``, en dat draagt
    het prune-voorvoegsel ``<deployment>-vlam-``; zonder deze toets is dat een aanname, en
    een kustomization die naar een verdwenen bestand wijst breekt de sync.
    """

    def _ctx(self) -> ManifestContext:
        return ManifestContext(
            deployment_name="prod",
            project_data=_project(),
            unique_name="prod-web",
            cluster=WITH_VLAM,
            get_secret=lambda *args, **kwargs: None,
            component_def={"name": "web", "services": []},
        )

    def _gegenereerd(self) -> list[str]:
        """Wat een run met de huidige code voor deze deployment neerzet."""
        deployment_ctx = DeploymentManifestContext(
            project_name="myproject",
            project_data=_project(),
            deployment=_project()["deployments"][0],
            cluster=WITH_VLAM,
            namespace="rig-prd-myproject",
        )
        namen = [f"{spec.filename}.yaml" for spec in SERVICE.contribute_deployment_manifests(deployment_ctx)]
        contribution = SERVICE.contribute_manifest_context(self._ctx())
        namen += [sops_filenames(f"{spec.secret_name}-secret").plaintext for spec in contribution.secret_files]
        return namen

    def test_het_achtergebleven_secret_wordt_verwijderd(self, tmp_path) -> None:
        achtergebleven = tmp_path / "prod-vlam-ca-secret.sops.yaml"
        achtergebleven.write_text("kind: Secret\n")
        beleid = tmp_path / "prod-vlam-web-network-policy.yaml"
        beleid.write_text("kind: NetworkPolicy\n")

        # De methode gebruikt alleen ``deployment["name"]`` van zichzelf; een echte
        # ProjectManager bouwen zou hier connectors optuigen die niets met de prune doen.
        ProjectManager._prune_obsolete_service_manifests(
            MagicMock(), {"name": "prod"}, str(tmp_path), self._gegenereerd()
        )

        assert not achtergebleven.exists()
        assert beleid.exists()

    def test_de_kustomization_noemt_het_niet_meer(self, tmp_path) -> None:
        """De kustomization wordt uit een schijfscan opgebouwd, dus dit is de uitkomst van
        de prune en niet een tweede plek om bij te werken."""
        (tmp_path / "prod-vlam-ca-secret.sops.yaml").write_text("kind: Secret\n")
        ProjectManager._prune_obsolete_service_manifests(
            MagicMock(), {"name": "prod"}, str(tmp_path), self._gegenereerd()
        )

        sops_files, regular_files = ManifestGenerator().collect_manifest_files(str(tmp_path))
        assert "prod-vlam-ca-secret.sops.yaml" not in sops_files + regular_files


class TestDeNetwerkregelOpentBeidePoorten:
    """De doorlus zit op een TWEEDE poort van dezelfde pod. Alleen 8082 openen levert een
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

    def test_beide_poorten_staan_in_de_regel(self) -> None:
        specs = SERVICE.contribute_deployment_manifests(self._ctx())
        assert specs[0].values["egress"][0]["ports"] == [8082, 8443]

    def test_de_gerenderde_regel_opent_beide(self) -> None:
        specs = SERVICE.contribute_deployment_manifests(self._ctx())
        rendered = YAML().load(render_template(specs[0].template_path, specs[0].values))
        rule = rendered["spec"]["egress"][0]
        assert [port["port"] for port in rule["ports"]] == [8082, 8443]
        assert rule["to"][0]["podSelector"]["matchLabels"]["app"] == "productie-vlam-proxy-intern"

    def test_zonder_doorlus_blijft_het_bij_een_poort(self, zonder_doorlus) -> None:
        specs = SERVICE.contribute_deployment_manifests(self._ctx())
        assert specs[0].values["egress"][0]["ports"] == [8082]


class TestDeDownloadrouteIsWeg:
    """RC-414: de bundel bestaat niet meer, dus de route die hem uitdeelde ook niet.

    Op de gemonteerde routers en niet op de bron: een vergeten registratie elders zou de
    route terugbrengen zonder dat er een bestand bij hoort, en dan deelt de app een
    404-pagina uit als PEM-bestand.
    """

    def test_geen_dienst_registreert_de_route_nog(self) -> None:
        paden = [route.path for router in collect_service_routers() for route in router.routes]
        assert not [pad for pad in paden if "ca-bundle" in pad]

    def test_de_app_antwoordt_niet_meer_op_het_pad(self) -> None:
        app = FastAPI()
        for router in SERVICE.web_routers():
            app.include_router(router)
        assert TestClient(app).get("/services/vlam/ca-bundle").status_code == 404


class TestHetBlokOpDeProjectpagina:
    """Het blok bestaat alleen waar er iets te melden is."""

    @pytest.fixture
    def op_het_vlam_cluster(self, monkeypatch: pytest.MonkeyPatch):
        from opi.core.config import settings

        monkeypatch.setattr(settings, "CLUSTER_MANAGER", WITH_VLAM)

    def _sections(self) -> list:
        from opi.services.catalog.base import ProjectPageContext

        return SERVICE.handle_ui(
            UIEvent.PROJECT_SECTIONS, ProjectPageContext(project_data=_project(), can=lambda _handeling: True)
        )

    def test_het_blok_noemt_beide_adressen(self, op_het_vlam_cluster) -> None:
        sections = self._sections()
        assert len(sections) == 1
        context = sections[0].context
        assert context["api_url"].startswith("http://")
        assert context["direct_url"] == "https://vlam-api.overheid-i.nl:8443"

    def test_het_blok_geeft_niets_over_een_bundel_mee(self, op_het_vlam_cluster) -> None:
        """Een sjabloon dat een pad of een knop verwacht, rendert stil een lege waarde."""
        context = self._sections()[0].context
        assert set(context) == {"api_url", "direct_url"}

    def test_zonder_doorlus_geen_blok(self, zonder_doorlus, op_het_vlam_cluster) -> None:
        assert self._sections() == []
