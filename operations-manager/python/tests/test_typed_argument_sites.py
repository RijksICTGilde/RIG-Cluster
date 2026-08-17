"""Regression tests for arguments that did not match their parameter.

Second half of RC-40. ``reportArgumentType`` found 102 places where a value was
handed to a parameter it does not fit -- almost always ``X | None`` into a ``str``.
Most were narrowings the code already guaranteed, but a handful were real: a call
that could not work, or a type that lied about what the code does. Those are here.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestMinioPurgeUsesTheManagedCluster:
    """The MinIO purge branch called ``get_minio_host(None)``.

    ``get_cluster_config`` refuses an unknown cluster, and None is one, so the whole
    branch raised before purging anything -- swallowed by its own ``except Exception``
    and logged as "failed to initialize the MinIO connector". This instance manages
    exactly one cluster, and that is the one the alias must point at.
    """

    def _bucket_mark(self) -> dict:
        return {
            "id": "mark-1",
            "resource_type": "minio_bucket",
            "resource_name": "demo-prod",
            "project_name": "demo",
            "deployment_name": "prod",
            "cluster": "odcn-production",
        }

    async def test_alias_is_configured_for_the_managed_cluster(self) -> None:
        from opi.jobs import reconciliation
        from opi.services.marked_for_deletion_service import MarkedForDeletionService

        service = AsyncMock(spec=MarkedForDeletionService)
        results: dict[str, Any] = {"purged": [], "errors": []}
        minio_conn = AsyncMock()

        with (
            patch.object(reconciliation.settings, "CLUSTER_MANAGER", "odcn-production"),
            patch.object(reconciliation, "create_minio_connector", return_value=minio_conn),
            patch("opi.core.cluster_config.get_minio_host", return_value="minio.rig") as host,
            patch("opi.core.cluster_config.get_minio_port", return_value=9000) as port,
            patch.object(reconciliation, "_purge_minio_bucket", new=AsyncMock()) as purge,
        ):
            await reconciliation._purge_marks([self._bucket_mark()], service, results, dry_run=False)

        host.assert_called_once_with("odcn-production")
        port.assert_called_once_with("odcn-production")
        minio_conn.configure_alias.assert_awaited_once()
        # The point of the branch: it got far enough to purge.
        purge.assert_awaited_once()
        assert results["errors"] == []


class TestServiceConfigSectionIsRequired:
    """A service section listed as a wizard step may not be None.

    ``config_form_section`` is allowed to answer None -- a service with no fields at
    that layer. Every section that goes through ``_with_service_help`` is a step in a
    flow, so a None would sit in ``FormFlow.sections`` and surface as an AttributeError
    somewhere in the middle of the wizard.
    """

    def test_a_missing_section_is_refused_at_definition_time(self) -> None:
        from opi.forms.visualizers.wizard_sections import _with_service_help
        from opi.services.services_enums import ServiceType

        with pytest.raises(ValueError, match="configuratiesectie"):
            _with_service_help(None, ServiceType.KEYCLOAK)

    def test_a_section_is_returned_and_stamped_with_the_help(self) -> None:
        from opi.forms.visualizers.sections import FormSection
        from opi.forms.visualizers.wizard_sections import _with_service_help
        from opi.services.services_enums import ServiceType

        section = FormSection(section_id="x", title="X", editables=[])
        result = _with_service_help(section, ServiceType.KEYCLOAK)

        assert result is section

    def test_every_flow_section_is_a_real_section(self) -> None:
        """The property the refusal above protects, checked on the real flows."""
        from opi.forms.visualizers.flows import CREATE_FLOW, EDIT_FLOW

        for flow in (CREATE_FLOW, EDIT_FLOW):
            assert all(s is not None for s in flow.sections), flow.flow_id


class TestGenerationMayBeUnknown:
    """``record_clone`` declared ``generation: int`` while its writer normalised None.

    The bottom of that call chain has always had "never write None as generation - 0
    is the default". The annotation said otherwise, so a first-generation clone -- which
    genuinely has no generation -- was a type error at every call site.
    """

    def test_a_clone_without_a_generation_is_recorded_as_zero(self) -> None:
        from opi.handlers.project_file_handler import ProjectFileHandler
        from opi.manager.revision_manager import RevisionManager

        manager = RevisionManager(ProjectFileHandler())
        project_data: dict[str, Any] = {
            "name": "demo",
            "deployments": [
                {
                    "name": "prod",
                    "services": [{"reference": "postgresql-database", "config": {}}],
                }
            ],
        }

        manager.record_clone(
            project_data=project_data,
            deployment_name="prod",
            service_type="postgresql-database",
            generation=None,
            resource_name="demo_prod",
            source="deployment:acc",
        )

        service = project_data["deployments"][0]["services"][0]
        revisions = service["config"]["revisions"]
        assert revisions[-1]["generation"] == 0
        assert revisions[-1]["status"] == "active"


class TestDeploymentOrderWithoutNames:
    """Ordering is by name; a deployment without one cannot take part in it."""

    def test_clone_sources_still_come_first(self) -> None:
        from opi.services.deployment_order import order_deployments_by_clone_dependency

        deployments = [
            {"name": "prod", "clone-from": {"type": "deployment", "reference": "acc"}},
            {"name": "acc"},
        ]

        ordered = order_deployments_by_clone_dependency(deployments)

        assert [d["name"] for d in ordered] == ["acc", "prod"]

    def test_a_nameless_deployment_is_kept(self) -> None:
        from opi.services.deployment_order import order_deployments_by_clone_dependency

        deployments: list[dict[str, Any]] = [{"cluster": "local"}, {"name": "acc"}]

        ordered = order_deployments_by_clone_dependency(deployments)

        assert len(ordered) == 2
        assert {d.get("name") for d in ordered} == {None, "acc"}


class TestStructuralValidationRefusesNamelessEntries:
    """The structural validator collected names into ``set[str]`` without checking.

    It runs after schema validation, where a name is required, so a nameless entry
    means something bypassed that gate. It fails closed, which is what the rest of
    this validator does.
    """

    async def test_component_without_a_name(self) -> None:
        from opi.manager.project_validation import ProjectIntegrityError, validate_project_structure

        with pytest.raises(ProjectIntegrityError, match="component zonder naam"):
            await validate_project_structure({"name": "demo", "components": [{"type": "deployment"}]})

    async def test_deployment_without_a_name(self) -> None:
        from opi.manager.project_validation import ProjectIntegrityError, validate_project_structure

        with pytest.raises(ProjectIntegrityError, match="deployment zonder naam"):
            await validate_project_structure({"name": "demo", "deployments": [{"cluster": "local"}]})


class TestRenderFieldsWithoutLayout:
    """``render_fields_from_editables`` is handed ``FormSection.layout``, which is optional.

    The parameter did not accept None, and passing it would have rendered nothing
    sensible. It now falls back to every field in order -- the same default the
    schema-driven renderers build.
    """

    def test_no_layout_renders_every_field(self) -> None:
        from opi.forms.editables.editable import Editable, WidgetType
        from opi.forms.renderer import FormRenderer
        from opi.forms.visualizers.visualizer import EditableVisualizer
        from opi.forms.widgets.lotc import LOTCWidgetAdapter

        editables = [
            EditableVisualizer(editable=Editable(yaml_path="display-name"), label="Naam", widget=WidgetType.TEXT),
            EditableVisualizer(
                editable=Editable(yaml_path="description"), label="Omschrijving", widget=WidgetType.TEXT
            ),
        ]
        renderer = FormRenderer(LOTCWidgetAdapter())

        html = renderer.render_fields_from_editables(
            editables=editables,
            yaml_data={"display-name": "Demo", "description": "Iets"},
            layout=None,
        )

        assert "Naam" in html
        assert "Omschrijving" in html


class TestProjectStoreAcceptsAsyncChanges:
    """``ChangeFunction`` may be sync or async; the awaited result kept the Awaitable.

    Reassigning the same name left ``mutated`` a union of "the dict" and "the thing you
    await to get the dict" for the whole rest of the method.
    """

    @pytest.mark.parametrize("is_async", [False, True])
    async def test_both_shapes_reach_validation_and_persistence(self, is_async: bool) -> None:
        from opi.services.project_store import GitProjectStore

        store = GitProjectStore.__new__(GitProjectStore)
        connector = AsyncMock()
        connector.get_local_commit_hash = AsyncMock(return_value="abc123")

        def sync_change(data: dict[str, Any]) -> dict[str, Any]:
            return {**data, "display-name": "Nieuw"}

        async def async_change(data: dict[str, Any]) -> dict[str, Any]:
            return {**data, "display-name": "Nieuw"}

        with (
            patch.object(GitProjectStore, "_locked", MagicMock()),
            patch.object(GitProjectStore, "get_connector", AsyncMock(return_value=connector)),
            patch.object(GitProjectStore, "_relative_path", MagicMock(return_value="projects/demo.yaml")),
            # Second read is the write-through: None means "use what we just wrote".
            patch.object(GitProjectStore, "_read_committed", AsyncMock(side_effect=[{"name": "demo"}, None])),
            patch.object(GitProjectStore, "_validate", AsyncMock()) as validate,
            patch.object(GitProjectStore, "_persist", AsyncMock(return_value="def456")),
            patch.object(GitProjectStore, "_refresh_cache", MagicMock()),
        ):
            result = await store.mutate(
                "demo",
                async_change if is_async else sync_change,
                message="test",
                actor="tester",
            )

        assert result.after["display-name"] == "Nieuw"
        assert validate.await_args.args[0]["display-name"] == "Nieuw"
