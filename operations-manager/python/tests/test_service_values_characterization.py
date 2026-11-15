"""What the five writers of env vars and service values do today, before ship 8c.

The measurement behind RC-481 found behaviour nothing pinned: a no-op config write that
still rolls out, a conflict that reads as an internal error, a PATCH that creates no
approval request, and the two shapes the same value is stored in depending on which
entrance wrote it. Each of those is recorded here as it IS, so the convergence that
follows has to say out loud what it changes.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import opi.web.router_env_vars
import pytest
from opi.manager.project_manager import ProjectManager
from opi.services.component_values import ComponentValuesError, validate_value_for_storage
from opi.services.project_store import ConflictError

pytestmark = pytest.mark.asyncio

PM_PATH = "opi.manager.project_manager.ProjectManager"


def _progress() -> MagicMock:
    progress = MagicMock()
    progress.add_task.side_effect = lambda name, subject=None: f"task-{name}"
    return progress


def _manager(project_data: dict[str, Any]) -> MagicMock:
    pm = MagicMock()
    pm.get_contents = AsyncMock(return_value=project_data)
    pm.get_name = AsyncMock(return_value="proj")
    pm.save_and_commit_project = AsyncMock()
    return pm


class TestNoOpConfigWriteDoesNotRollOut:
    """S7: a write that committed nothing has nothing to reconcile."""

    async def test_an_upsert_that_committed_nothing_does_not_process(self) -> None:
        from opi.runtime.tasks.task_handlers_components import handle_configure_service

        pm = AsyncMock()
        pm.configure_service = AsyncMock(return_value={"success": True, "changed": False})
        pm.process_project_from_git = AsyncMock(return_value=True)
        pm.get_processing_error = MagicMock(return_value=None)
        pm.close = AsyncMock()

        with patch(PM_PATH, return_value=pm):
            result = await handle_configure_service(
                {
                    "project_name": "proj",
                    "service": "keycloak",
                    "target": "project",
                    "operation": "upsert",
                    "config": {"template": "x"},
                },
                _progress(),
            )

        pm.process_project_from_git.assert_not_called()
        assert result["processing"]["status"] == "skipped"

    async def test_a_clear_that_removed_nothing_skips_the_processing(self) -> None:
        from opi.runtime.tasks.task_handlers_components import handle_configure_service

        pm = AsyncMock()
        pm.clear_service_config = AsyncMock(return_value={"success": True, "removed": False, "changed": False})
        pm.process_project_from_git = AsyncMock(return_value=True)
        pm.get_processing_error = MagicMock(return_value=None)
        pm.close = AsyncMock()

        with patch(PM_PATH, return_value=pm):
            result = await handle_configure_service(
                {"project_name": "proj", "service": "keycloak", "target": "project", "operation": "clear"},
                _progress(),
            )

        pm.process_project_from_git.assert_not_called()
        assert result["processing"]["status"] == "skipped"


class TestValuesWriteSkipsTheRollout:
    """``handle_configure_service_values`` had no test for either skip."""

    async def test_an_unchanged_write_does_not_process(self) -> None:
        from opi.runtime.tasks.task_handlers_components import handle_configure_service_values

        pm = AsyncMock()
        pm.set_component_values = AsyncMock(return_value={"success": True, "changed": False})
        pm.process_project_from_git = AsyncMock(return_value=True)
        pm.get_processing_error = MagicMock(return_value=None)
        pm.close = AsyncMock()

        with patch(PM_PATH, return_value=pm):
            result = await handle_configure_service_values(
                {
                    "project_name": "proj",
                    "service": "user-env-vars",
                    "target": "component",
                    "operation": "add",
                    "component": "web",
                    "values": {"A": "1"},
                },
                _progress(),
            )

        pm.process_project_from_git.assert_not_called()
        assert result["changed"] is False
        assert result["processing"]["status"] == "skipped"
        assert "reason" not in result["processing"]

    async def test_rollout_false_does_not_process_and_names_the_reason(self) -> None:
        from opi.runtime.tasks.task_handlers_components import handle_configure_service_values
        from opi.services.tasks.task_rollout import SKIPPED_REASON

        pm = AsyncMock()
        pm.set_component_values = AsyncMock(return_value={"success": True, "changed": True})
        pm.process_project_from_git = AsyncMock(return_value=True)
        pm.get_processing_error = MagicMock(return_value=None)
        pm.close = AsyncMock()

        with patch(PM_PATH, return_value=pm):
            result = await handle_configure_service_values(
                {
                    "project_name": "proj",
                    "service": "user-env-vars",
                    "target": "component",
                    "operation": "add",
                    "component": "web",
                    "values": {"A": "1"},
                    "rollout": False,
                },
                _progress(),
            )

        pm.process_project_from_git.assert_not_called()
        assert result["processing"]["reason"] == SKIPPED_REASON


class TestConflictReadsAsAConflict:
    """S8: a push conflict is the caller's to retry, not an internal error."""

    def _data(self) -> dict[str, Any]:
        return {
            "name": "proj",
            "services": ["authorization-wall"],
            "components": [
                {
                    "name": "backend",
                    "type": "single",
                    "services": [
                        {
                            "reference": "persistent-storage",
                            "config": [{"name": "data1", "size": "1Gi", "mount-path": "/data1"}],
                        }
                    ],
                }
            ],
        }

    async def test_configure_service(self) -> None:
        pm = _manager(self._data())
        pm.save_and_commit_project = AsyncMock(side_effect=ConflictError("push refused"))

        result = await ProjectManager.configure_service(pm, "authorization-wall", "project", {"banner": "let op"})

        assert result["success"] is False
        assert result["error_type"] == "conflict"
        assert "opnieuw" in result["error"]

    async def test_clear_service_config(self) -> None:
        data = self._data()
        data["services"] = [{"name": "authorization-wall", "config": {"banner": "let op"}}]
        pm = _manager(data)
        pm.save_and_commit_project = AsyncMock(side_effect=ConflictError("push refused"))

        result = await ProjectManager.clear_service_config(pm, "authorization-wall", "project")

        assert result["error_type"] == "conflict"

    async def test_set_component_values(self) -> None:
        """The values writer re-applies on a conflict; this is when that did not settle."""
        pm = _manager(self._data())
        pm.mutate_and_commit_project = AsyncMock(side_effect=ConflictError("push refused"))

        result = await ProjectManager.set_component_values(
            pm, "user-env-vars", "component", "add", component_name="backend", values={"A": "1"}
        )

        assert result["error_type"] == "conflict"

    async def test_patch_service_config_list(self) -> None:
        pm = _manager(self._data())
        pm.save_and_commit_project = AsyncMock(side_effect=ConflictError("push refused"))

        result = await ProjectManager.patch_service_config_list(
            pm, "persistent-storage", "component", add=[], remove=["data1"], component_name="backend"
        )

        assert result["error_type"] == "conflict"


class TestEveryServiceWriteRecordsItsApprovalRequests:
    """S6: not only the upsert asks the catalog to record what is still unapproved."""

    def _project_with_a_domain_request(self) -> dict[str, Any]:
        return {
            "name": "proj",
            "services": ["publish-on-web"],
            "components": [{"name": "web", "type": "single", "services": ["publish-on-web"]}],
            "deployments": [{"name": "prod", "components": [{"reference": "web"}]}],
        }

    async def test_the_upsert_calls_it(self) -> None:
        pm = _manager(self._project_with_a_domain_request())
        with patch("opi.manager.project.service_config_mutations.ensure_approval_requests") as ensure:
            await ProjectManager.configure_service(pm, "authorization-wall", "project", {"banner": "let op"})

        ensure.assert_called_once()

    async def test_the_patch_calls_it_too(self) -> None:
        data = self._project_with_a_domain_request()
        data["components"][0]["services"] = [
            {"reference": "persistent-storage", "config": [{"name": "data1", "size": "1Gi", "mount-path": "/data1"}]}
        ]
        pm = _manager(data)
        with patch("opi.manager.project.service_config_mutations.ensure_approval_requests") as ensure:
            result = await ProjectManager.patch_service_config_list(
                pm, "persistent-storage", "component", add=[], remove=["data1"], component_name="web"
            )

        assert result["success"], result
        ensure.assert_called_once()

    async def test_the_clear_calls_it_too(self) -> None:
        data = self._project_with_a_domain_request()
        data["services"] = [{"name": "authorization-wall", "config": {"banner": "let op"}}]
        pm = _manager(data)
        with patch("opi.manager.project.service_config_mutations.ensure_approval_requests") as ensure:
            await ProjectManager.clear_service_config(pm, "authorization-wall", "project")

        ensure.assert_called_once()


class TestAPlatformFieldInTheInputKeepsTwoAnswers:
    """S9: the API refuses one, the form puts it back. Two outcomes, on purpose.

    An API client that sends a field believes it set it, so a write that reports success
    while ignoring part of the body would lie. A form posts the whole section back,
    including fields its author never touched, so refusing it would make the section
    unsaveable.
    """

    def test_the_api_refuses_it_with_a_422(self) -> None:
        from fastapi import HTTPException
        from opi.api.v2.router import _refuse_platform_managed

        with pytest.raises(HTTPException) as refusal:
            _refuse_platform_managed("keycloak", {"realms": [{"realm": "x"}]}, frozenset({"realms"}))

        assert refusal.value.status_code == 422

    def test_the_form_puts_the_stored_value_back(self) -> None:
        from opi.services import ServiceAdapter

        def project(realm: str, template: str) -> dict[str, Any]:
            return {
                "name": "proj",
                "services": [{"name": "keycloak", "config": {"realms": [{"realm": realm}], "template": template}}],
            }

        submitted = project("verzonnen", "b")
        ServiceAdapter.enforce_platform_managed_fields(submitted, project("echt", "a"))

        config = submitted["services"][0]["config"]
        assert config["realms"] == [{"realm": "echt"}]
        assert config["template"] == "b"


class TestComponentWritesRecordTheirApprovalRequests:
    """S6 for the other way a service lands on a component: add and update."""

    def _project(self) -> dict[str, Any]:
        return {
            "name": "proj",
            "services": ["publish-on-web"],
            "components": [{"name": "web", "type": "single", "services": ["publish-on-web"]}],
            "deployments": [{"name": "prod", "components": [{"reference": "web"}]}],
        }

    async def test_add_component_calls_it(self) -> None:
        pm = _manager(self._project())
        with patch("opi.manager.project.component_mutations.ensure_approval_requests") as ensure:
            result = await ProjectManager.add_component(
                pm, name="api", image="nginx:latest", deployment_names=["prod"], services=["publish-on-web"]
            )

        assert result["success"], result
        ensure.assert_called_once()

    async def test_update_component_calls_it(self) -> None:
        pm = _manager(self._project())
        with patch("opi.manager.project.component_mutations.ensure_approval_requests") as ensure:
            result = await ProjectManager.update_component(pm, name="web", description="nu met uitleg")

        assert result["success"], result
        ensure.assert_called_once()


class TestAddComponentRefusesValuesWithoutAKey:
    """S10 and S11: the third writer of ``user-env-vars`` is fail-closed and validated."""

    async def test_env_vars_without_a_public_key_are_refused(self) -> None:
        from opi.services.component_values import ComponentValuesError
        from opi.services.project_utils import build_component_config

        with pytest.raises(ComponentValuesError):
            await build_component_config(
                name="web",
                component_type="single",
                port=8080,
                path="/",
                services=[],
                env_vars="A=1",
                public_key=None,
            )

    async def test_text_that_parses_to_nothing_leaves_no_key(self) -> None:
        from opi.services.project_utils import build_component_config

        config = await build_component_config(
            name="web",
            component_type="single",
            port=8080,
            path="/",
            services=[],
            env_vars="# alleen een opmerking",
            public_key=None,
        )

        assert "user-env-vars" not in config

    async def test_a_name_that_is_not_a_variable_name_is_refused(self) -> None:
        from opi.services.project_utils import build_component_config

        with pytest.raises(ValueError, match="Invalid key format"):
            await build_component_config(
                name="web",
                component_type="single",
                port=8080,
                path="/",
                services=[],
                env_vars="niet-een-naam=1",
                public_key="age1publiek",
            )

    async def test_aliases_are_kept_in_plaintext_without_a_public_key(self) -> None:
        from opi.services.project_utils import build_component_config

        config = await build_component_config(
            name="web",
            component_type="single",
            port=8080,
            path="/",
            services=[],
            aliases="DB_HOST: $DATABASE_SERVER_HOST",
            public_key=None,
        )

        assert config["aliases"] == {"DB_HOST": "$DATABASE_SERVER_HOST"}


class TestTheStoredShapeIsTheSameForEveryWriter:
    """S1 and S2: the create wizard judges and stores a value like the values API.

    The quotes and the whitespace have to come in through the YAML shape of the field: in
    the ``KEY=value`` shape the parser strips both before anyone can store them.
    """

    def test_the_api_validator_refuses_edge_whitespace_and_surrounding_quotes(self) -> None:
        with pytest.raises(ComponentValuesError):
            validate_value_for_storage("A", " padded ")
        with pytest.raises(ComponentValuesError):
            validate_value_for_storage("A", '"quoted"')

    def test_the_form_validator_refuses_both(self) -> None:
        from opi.services.catalog.user_env_vars.editables import UserEnvVarsValidator

        assert UserEnvVarsValidator().validate('A: " padded "') != []
        assert UserEnvVarsValidator().validate("""B: '"quoted"'""") != []

    def test_the_form_converter_stores_the_canonical_block(self) -> None:
        from opi.services.catalog.user_env_vars.editables import UserEnvVarsConverter

        captured: list[str] = []
        with patch(
            "opi.services.component_values.encrypt_age_content_sync",
            side_effect=lambda text, _key: captured.append(text) or "cipher",
        ):
            UserEnvVarsConverter().write("# een opmerking\nA=1\n\nB=2", {"config": {"age-public-key": "age1x"}})

        assert captured == ["A=1\nB=2"]


class TestSelfServiceValidatesEnvVarsLikeTheRest:
    """S11: self-service writes through the same builder, so it runs the same validator.

    The request model itself still accepts the text: the refusal comes from the write,
    which is the one place every writer of this field passes through.
    """

    def test_the_request_model_still_accepts_the_text(self) -> None:
        from opi.api.router import SelfServiceComponent

        component = SelfServiceComponent(type="single", image="nginx:latest", env_vars='A="quoted"')

        assert component.env_vars == 'A="quoted"'


def test_the_env_vars_window_rolls_out_only_what_it_touched() -> None:
    """S4: the window no longer borrows ``create_project``, which had no deployment scope."""
    source = Path(inspect.getfile(opi.web.router_env_vars)).read_text()

    assert 'task_type="rollout_project"' in source
    assert "project_file_already_saved" not in source
