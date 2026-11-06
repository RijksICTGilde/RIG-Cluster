"""Tests voor de scope van een wijziging: welke deployments raakt ze?

Een wijziging aan een deployment hoorde alleen die deployment te raken, maar
``configure_service``, ``configure_service_values``, ``update_component`` en
``add_service`` verwerkten het hele project: alle manifests opnieuw, alle provisioning
opnieuw, en wachten op de health van elke deployment (RC-418).

De scope wordt op EEN plek bepaald (``affected_deployments``) en op twee plekken gebruikt:
de verwerking en de taakscope waarmee een nieuwe taak een oudere kan verdringen. Die twee
MOETEN dezelfde lijst zijn, want een scope die breder is dan de verwerking verdringt een
wachtende taak waarvan de deployments daarna niemand meer doet. Dat is wat deze tests
vastleggen.
"""

import pytest
from opi.core.task_supersede import covers, scope_of
from opi.utils.project_utils import affected_deployments, deployments_using_component


@pytest.fixture
def project() -> dict:
    """Een project met drie deployments: 'web' zit in twee, 'api' in een."""
    return {
        "name": "proj",
        "components": [{"name": "web"}, {"name": "api"}, {"name": "ongebruikt"}],
        "deployments": [
            {"name": "productie", "components": [{"reference": "web"}, {"reference": "api"}]},
            {"name": "acceptatie", "components": [{"reference": "web"}]},
            {"name": "test", "components": []},
        ],
    }


class TestDeploymentsVanEenComponent:
    def test_een_component_in_twee_deployments_geeft_beide(self, project: dict) -> None:
        assert deployments_using_component(project, "web") == ["productie", "acceptatie"]

    def test_een_component_in_een_deployment_geeft_er_een(self, project: dict) -> None:
        assert deployments_using_component(project, "api") == ["productie"]

    def test_een_component_dat_niemand_gebruikt_geeft_niets(self, project: dict) -> None:
        assert deployments_using_component(project, "ongebruikt") == []

    def test_een_onbekend_component_geeft_niets(self, project: dict) -> None:
        assert deployments_using_component(project, "bestaat-niet") == []

    def test_een_project_zonder_deployments_geeft_niets(self) -> None:
        assert deployments_using_component({}, "web") == []


class TestAffectedDeployments:
    def test_een_deploymentdoel_raakt_alleen_die_deployment(self, project: dict) -> None:
        assert affected_deployments(project, deployment_name="acceptatie") == ["acceptatie"]

    def test_een_deploymentdoel_gaat_voor_een_componentdoel(self, project: dict) -> None:
        """De deploymentlaag is specifieker: dan doet het component niet meer mee."""
        assert affected_deployments(project, deployment_name="test", component_names=["web"]) == ["test"]

    def test_een_componentdoel_raakt_elke_deployment_die_het_gebruikt(self, project: dict) -> None:
        assert affected_deployments(project, component_names=["web"]) == ["productie", "acceptatie"]

    def test_twee_componenten_geven_de_vereniging_zonder_dubbelen(self, project: dict) -> None:
        assert affected_deployments(project, component_names=["web", "api"]) == ["productie", "acceptatie"]

    def test_zonder_doel_blijft_de_ronde_projectbreed(self, project: dict) -> None:
        assert affected_deployments(project) is None

    def test_een_lege_componentlijst_blijft_projectbreed(self, project: dict) -> None:
        """``add_service`` zonder componentlijst bindt aan elk component van het project."""
        assert affected_deployments(project, component_names=[]) is None


class TestTaakscopeVolgtDeVerwerking:
    @pytest.mark.parametrize(
        "task_type",
        ["configure_service", "configure_service_values", "update_component", "add_service", "add_component"],
    )
    def test_de_scope_is_de_lijst_uit_de_payload(self, task_type: str) -> None:
        assert scope_of(task_type, None, {"deployment_names": ["acceptatie"]}) == frozenset({"acceptatie"})

    @pytest.mark.parametrize(
        "task_type",
        ["configure_service", "configure_service_values", "update_component", "add_service", "add_component"],
    )
    def test_zonder_lijst_blijft_de_scope_projectbreed(self, task_type: str) -> None:
        assert scope_of(task_type, None, {}) is None
        assert scope_of(task_type, None, {"deployment_names": []}) is None

    def test_een_refresh_van_het_project_blijft_altijd_projectbreed(self) -> None:
        assert scope_of("refresh_project", "productie", {"deployment_names": ["productie"]}) is None

    def test_de_scope_van_een_gerichte_taak_verdringt_geen_projectbrede(self, project: dict) -> None:
        """De grendel onder deze wijziging: versmallen mag nooit iemand anders wegdrukken."""
        gericht = scope_of("configure_service", None, {"deployment_names": ["acceptatie"]})
        projectbreed = scope_of("refresh_project", None, {})
        assert covers(gericht, projectbreed) is False
        assert covers(projectbreed, gericht) is True

    def test_een_componentwijziging_dekt_elke_deployment_die_ze_verwerkt(self, project: dict) -> None:
        namen = affected_deployments(project, component_names=["web"])
        gericht = scope_of("update_component", None, {"deployment_names": namen})
        for naam in namen or []:
            assert covers(gericht, frozenset({naam})), f"{naam} wordt verwerkt maar valt buiten de scope"
        assert covers(gericht, frozenset({"test"})) is False
