"""A service that may enrol itself at project level (RC-84).

Hanging a service on a component or a deployment used to mean one of two things: a
needless extra step (a database has nothing to decide at project level) or a missing
decision (which domains may this project publish on). Which of the two it is, is now the
service's own answer -- ``allows_implicit_project_selection`` plus
``implicit_project_config``, resolved by ``Service.implicit_project_entry``.

These tests hold the three properties the answer has to have: the base class says no, a
service that says yes without a valid default fails loudly, and the API path adds the
service through the ordinary save path or refuses by name.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel

from opi.core.project_schema import validate_project_schema
from opi.manager.project_manager import ProjectManager
from opi.services.catalog.base import ConfigLayer, Service
from opi.services.registry import SERVICES, get_service
from opi.services.services import ServiceAdapter, ServiceValidationError, service_entry_name
from opi.services.services_enums import ServiceType

#: The recorded decision, per service (features/impliciete-dienstselectie.md). A service
#: that may enrol itself is a deliberate entry in this set; everything else is the safe
#: default, so a new service can never join it by accident.
IMPLICIT_SERVICES = {
    ServiceType.POSTGRESQL_DATABASE,
    ServiceType.NAMESPACE_POSTGRESQL_DATABASE,
    ServiceType.MINIO_STORAGE,
    ServiceType.REDIS,
    ServiceType.NAMESPACE_REDIS,
    ServiceType.PERSISTENT_STORAGE,
    ServiceType.TEMP_STORAGE,
    ServiceType.HEALTH_CHECK,
    ServiceType.METRICS_SCRAPER,
    ServiceType.PLATFORM,
    ServiceType.RESOURCE_TUNING,
    ServiceType.DEPLOYMENT_HEALTH,
    ServiceType.USER_ENV_VARS,
    ServiceType.ALIASES,
}


class TestTheHookItself:
    def test_base_class_says_no(self) -> None:
        """A service that says nothing is not implicit -- the safe answer by default."""

        class SilentService(Service):
            pass

        assert SilentService().implicit_project_entry() is None

    def test_yes_without_a_valid_default_fails_loudly(self) -> None:
        """Saying yes while the default project config does not validate is a
        programming error in the service, not a project file written anyway."""

        class NeedsAChoice(BaseModel):
            template: str  # required: no default can be assumed

        class OverconfidentService(Service):
            service_type = ServiceType.KEYCLOAK
            definition = SERVICES[ServiceType.KEYCLOAK].definition
            config_model = NeedsAChoice
            allows_implicit_project_selection = True

            def config_api_fields(self, layer: ConfigLayer) -> list[str]:
                return self.config_model_field_names() if layer is ConfigLayer.PROJECT else []

        with pytest.raises(TypeError, match="does not validate"):
            OverconfidentService().implicit_project_entry()

    def test_a_supplied_default_config_becomes_the_project_entry(self) -> None:
        class HasADefault(BaseModel):
            template: str

        class ConfiguredService(Service):
            service_type = ServiceType.KEYCLOAK
            definition = SERVICES[ServiceType.KEYCLOAK].definition
            config_model = HasADefault
            allows_implicit_project_selection = True

            def config_api_fields(self, layer: ConfigLayer) -> list[str]:
                return self.config_model_field_names() if layer is ConfigLayer.PROJECT else []

            def implicit_project_config(self) -> dict[str, Any]:
                return {"template": "default"}

        assert ConfiguredService().implicit_project_entry() == {
            "name": "keycloak",
            "config": {"template": "default"},
        }

    def test_a_config_without_a_project_layer_fails_loudly(self) -> None:
        """Handing over a project config while carrying none at that layer is a
        programming error: the config would have nowhere to go."""

        class MisplacedService(Service):
            service_type = ServiceType.NAMESPACE_REDIS
            definition = SERVICES[ServiceType.NAMESPACE_REDIS].definition
            allows_implicit_project_selection = True

            def implicit_project_config(self) -> dict[str, Any]:
                return {"anything": 1}

        with pytest.raises(TypeError, match="nowhere to go"):
            MisplacedService().implicit_project_entry()

    def test_approvals_beat_whatever_the_service_declares(self) -> None:
        """A service that needs an administrator's approval can never enrol itself --
        that would be enrolling the very thing the approval guards."""

        class ApprovableService(Service):
            service_type = ServiceType.PUBLISH_ON_WEB
            definition = SERVICES[ServiceType.PUBLISH_ON_WEB].definition
            allows_implicit_project_selection = True

            def config_approvals(self, layer: ConfigLayer) -> list[Any]:
                return SERVICES[ServiceType.PUBLISH_ON_WEB].config_approvals(layer)

        assert ApprovableService().approval_specs()  # guards the premise of the test
        assert ApprovableService().implicit_project_entry() is None


class TestTheCatalogAnswer:
    def test_every_service_answers_as_recorded(self) -> None:
        allowed = {st for st, service in SERVICES.items() if service.implicit_project_entry() is not None}
        assert allowed == IMPLICIT_SERVICES

    def test_publish_on_web_is_not_implicit(self) -> None:
        """The example from the plan: the project decides which domains it may publish
        on, and that is not a default anyone can invent."""
        assert get_service(ServiceType.PUBLISH_ON_WEB).implicit_project_entry() is None

    def test_every_implicit_entry_keeps_the_project_file_valid(self) -> None:
        project: dict[str, Any] = {
            "schema-version": 2,
            "name": "proj",
            "description": "test",
            "clusters": ["odcn-production"],
            "users": [{"email": "a@b.nl", "role": "admin"}],
            "services": [
                service.implicit_project_entry()
                for service in SERVICES.values()
                if service.implicit_project_entry() is not None
            ],
        }
        validate_project_schema(project)


def _project() -> dict[str, Any]:
    return {
        "schema-version": 2,
        "name": "proj",
        "description": "test",
        "clusters": ["odcn-production"],
        "users": [{"email": "a@b.nl", "role": "admin"}],
        "services": [],
        "components": [
            {
                "name": "mgr",
                "type": "single",
                "ports": {"inbound": [8443], "outbound": [80, 443]},
                "path": [{"match": "/"}],
                "services": [],
            }
        ],
    }


class TestEnsureProjectSelection:
    def test_allowed_service_is_added_as_its_own_entry(self) -> None:
        data = _project()
        ServiceAdapter.ensure_project_selection(data, ServiceType.POSTGRESQL_DATABASE.value)
        assert data["services"] == ["postgresql-database"]
        validate_project_schema(data)

    def test_refused_service_raises_and_leaves_the_file_alone(self) -> None:
        data = _project()
        with pytest.raises(ServiceValidationError, match="publish-on-web"):
            ServiceAdapter.ensure_project_selection(data, ServiceType.PUBLISH_ON_WEB.value)
        assert data["services"] == []

    def test_a_refusal_blocks_the_whole_list(self) -> None:
        """Nothing is written unless every name is allowed, so a rejected request does
        not leave half a selection behind."""
        data = _project()
        with pytest.raises(ServiceValidationError):
            ServiceAdapter.ensure_project_selection(data, ServiceType.REDIS.value, ServiceType.KEYCLOAK.value)
        assert data["services"] == []

    def test_existing_entry_is_never_duplicated_or_demoted(self) -> None:
        data = _project()
        data["services"] = [{"name": "keycloak", "config": {"template": "x"}}]
        ServiceAdapter.ensure_project_selection(data, ServiceType.KEYCLOAK.value)
        assert data["services"] == [{"name": "keycloak", "config": {"template": "x"}}]

    def test_unknown_service_is_rejected(self) -> None:
        data = _project()
        with pytest.raises(ServiceValidationError):
            ServiceAdapter.ensure_project_selection(data, "not-a-service")


class TestConfigWritePath:
    """``set_service_config`` on a component/deployment asks the same question."""

    def test_component_config_enrols_an_allowed_service(self) -> None:
        data = _project()
        ServiceAdapter.set_service_config(
            data, ServiceType.HEALTH_CHECK.value, ConfigLayer.COMPONENT, {"port": 8080}, component_name="mgr"
        )
        assert "health-check" in [service_entry_name(e) for e in data["services"]]

    def test_component_config_refuses_a_service_that_needs_a_decision(self) -> None:
        data = _project()
        with pytest.raises(ServiceValidationError, match="publish-on-web"):
            ServiceAdapter.set_service_config(
                data,
                ServiceType.PUBLISH_ON_WEB.value,
                ConfigLayer.COMPONENT,
                {"domain": "example.nl"},
                component_name="mgr",
            )
        assert data["services"] == []
        assert data["components"][0]["services"] == []


def _pm(project_data: dict[str, Any]) -> MagicMock:
    pm = MagicMock()
    pm.get_contents = AsyncMock(return_value=project_data)
    pm.get_name = AsyncMock(return_value="proj")
    pm.save_and_commit_project = AsyncMock()
    return pm


class TestComponentApi:
    async def test_add_component_enrols_the_database_it_binds(self) -> None:
        data = _project()
        data["components"] = []
        pm = _pm(data)

        result = await ProjectManager.add_component(
            pm, name="mgr", image="", deployment_names=[], services=[ServiceType.POSTGRESQL_DATABASE.value]
        )

        assert result["success"] is True
        assert [service_entry_name(e) for e in data["services"]] == ["postgresql-database"]
        pm.save_and_commit_project.assert_awaited_once()
        validate_project_schema(data)

    async def test_add_component_refuses_a_service_that_needs_a_decision(self) -> None:
        data = _project()
        data["components"] = []
        pm = _pm(data)

        result = await ProjectManager.add_component(
            pm, name="mgr", image="", deployment_names=[], services=[ServiceType.KEYCLOAK.value]
        )

        assert result["success"] is False
        assert result["error_type"] == "invalid_services"
        assert "keycloak" in result["error"]
        assert data["services"] == []
        assert data["components"] == []
        pm.save_and_commit_project.assert_not_called()

    async def test_update_component_enrols_an_allowed_service(self) -> None:
        data = _project()
        pm = _pm(data)

        result = await ProjectManager.update_component(pm, name="mgr", services=[ServiceType.REDIS.value])

        assert result["success"] is True
        assert [service_entry_name(e) for e in data["services"]] == ["redis"]
        assert [service_entry_name(e) for e in data["components"][0]["services"]] == ["redis"]
        validate_project_schema(data)

    async def test_update_component_refuses_a_service_that_needs_a_decision(self) -> None:
        data = _project()
        pm = _pm(data)

        result = await ProjectManager.update_component(pm, name="mgr", services=[ServiceType.PUBLISH_ON_WEB.value])

        assert result["success"] is False
        assert result["error_type"] == "invalid_services"
        assert "publish-on-web" in result["error"]
        assert data["services"] == []
        assert data["components"][0]["services"] == []
        pm.save_and_commit_project.assert_not_called()
