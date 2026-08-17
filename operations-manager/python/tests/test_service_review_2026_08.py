"""Regression tests for the fixes made during the augustus-2026 service-review sweep.

Each test here was written to fail against the pre-sweep code and pass after the fix,
so it documents exactly what changed and guards against a regression. Grouped by the
checklist section that motivated the fix. See docs/service-review-2026-08.md.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from opi.services.registry import get_service
from opi.services.services_enums import ServiceType

if TYPE_CHECKING:
    import pytest


class TestEditableGuardrails:
    """Checklist 4: an editable on a bounded value set carries a validator; an optional
    field carries remove_when_none + a converter so an empty field writes no key."""

    def test_metrics_scraper_port_editable_rejects_out_of_range(self) -> None:
        from opi.services.catalog.metrics_scraper.editables import METRICS_PORT_EDITABLE

        assert METRICS_PORT_EDITABLE.validator is not None
        assert METRICS_PORT_EDITABLE.validator.validate(70000)
        assert METRICS_PORT_EDITABLE.validator.validate(0)
        assert METRICS_PORT_EDITABLE.validator.validate(8080) == []

    def test_health_check_port_editable_rejects_out_of_range(self) -> None:
        from opi.services.catalog.health_check.editables import HEALTH_CHECK_PORT_EDITABLE

        assert HEALTH_CHECK_PORT_EDITABLE.validator is not None
        assert HEALTH_CHECK_PORT_EDITABLE.validator.validate(70000)
        assert HEALTH_CHECK_PORT_EDITABLE.validator.validate(8080) == []

    def test_attachments_env_name_editable_validates_env_var_name(self) -> None:
        from opi.services.catalog.attachments.editables import ATTACHMENT_USE_ENV_NAME_EDITABLE

        assert ATTACHMENT_USE_ENV_NAME_EDITABLE.validator is not None
        # A leading digit / a dash are illegal env-var names (mirrors the model regex).
        assert ATTACHMENT_USE_ENV_NAME_EDITABLE.validator.validate("1BAD")
        assert ATTACHMENT_USE_ENV_NAME_EDITABLE.validator.validate("BAD-NAME")
        assert ATTACHMENT_USE_ENV_NAME_EDITABLE.validator.validate("GOOD_NAME") == []

    def test_auth_wall_banner_empty_writes_no_key(self) -> None:
        from opi.services.catalog.authorization_wall.editables import AUTH_WALL_BANNER_EDITABLE

        # Optional free-text field: an empty submission must leave no key, not banner: "".
        assert AUTH_WALL_BANNER_EDITABLE.remove_when_none is True
        assert AUTH_WALL_BANNER_EDITABLE.converter is not None
        assert AUTH_WALL_BANNER_EDITABLE.converter.write("") is None
        assert AUTH_WALL_BANNER_EDITABLE.converter.write("Beperkte toegang") == "Beperkte toegang"


class TestAcceptedConfigFieldHints:
    """Checklist 3: a modelled service declares config_api_fields for the layer it carries
    config on, so a validation error tells the user which keys the service accepts."""

    def test_redis_declares_accepted_project_fields(self) -> None:
        from opi.services.catalog.base import ConfigLayer

        svc = get_service(ServiceType.REDIS)
        assert svc.config_api_fields(ConfigLayer.PROJECT) == ["acl-key-prefix"]
        assert svc.config_api_fields(ConfigLayer.COMPONENT) == []

    def test_minio_declares_accepted_fields(self) -> None:
        from opi.services.catalog.base import ConfigLayer

        svc = get_service(ServiceType.MINIO_STORAGE)
        fields = svc.config_api_fields(ConfigLayer.PROJECT)
        assert "enable-versioning" in fields


class TestDeploymentGenerationIdentity:
    """Checklist 5: deployment clone-state identity is resolved with service_entry_name,
    not a raw item.get('reference') that only matches the {reference} entry form."""

    def test_generation_read_via_name_form_entry(self) -> None:
        from opi.handlers.project_file_handler import ProjectFileHandler

        handler = ProjectFileHandler()
        # A {name, config} record (not {reference}) must still be found.
        project_data = {
            "deployments": [
                {
                    "name": "deployment-1",
                    "services": [{"name": "minio-storage", "config": {"generation": 3}}],
                }
            ]
        }
        generation = handler.get_deployment_service_generation(project_data, "deployment-1", "minio-storage")
        assert generation == 3

    def test_generation_read_bare_string_entry_is_none(self) -> None:
        from opi.handlers.project_file_handler import ProjectFileHandler

        handler = ProjectFileHandler()
        project_data = {"deployments": [{"name": "deployment-1", "services": ["minio-storage"]}]}
        assert handler.get_deployment_service_generation(project_data, "deployment-1", "minio-storage") is None


class TestApprovalVerdictLogging:
    """Checklist 10: recording an approval verdict is a state change and must log one
    INFO line naming the subject, the new status and the approver."""

    def test_recorded_verdict_logs_subject_and_status(self, caplog: pytest.LogCaptureFixture) -> None:
        from opi.services.approvals import apply_approval_verdicts, collect_approval_items

        project_data = {
            "name": "test-project",
            "domains": {
                "allowed-domains": [
                    {
                        "domain": "example.nl",
                        "status": "requested",
                        "supports-dots": False,
                        "history": [{"date": "2026-04-01T10:00:00+00:00", "status": "requested"}],
                    }
                ]
            },
        }
        items = collect_approval_items(project_data)
        for item in items:
            if item.get("domain") == "example.nl":
                item["status"] = "approved"
        with caplog.at_level(logging.INFO, logger="opi.services.approvals"):
            apply_approval_verdicts(project_data, items, admin_email="admin@test.nl")
        assert "example.nl" in caplog.text
        assert "approved" in caplog.text
        assert "admin@test.nl" in caplog.text


class TestNoConfigDictInLogs:
    """Checklist 10: a log line carries identifying values, never a whole config dict."""

    def test_redis_config_read_does_not_dump_dict(self, caplog: pytest.LogCaptureFixture) -> None:
        from opi.manager.redis_manager import RedisManager

        project_data = {"services": [{"name": "redis", "config": {"acl-key-prefix": False}}]}
        with caplog.at_level(logging.DEBUG, logger="opi.manager.redis_manager"):
            RedisManager._get_redis_service_config(project_data)
        # The raw dict repr must not appear in any log line.
        assert "{'acl-key-prefix': False}" not in caplog.text
        assert "{'acl-key-prefix': 'False'}" not in caplog.text

    def test_minio_config_read_does_not_dump_dict(self, caplog: pytest.LogCaptureFixture) -> None:
        from opi.manager.minio_manager import MinioManager

        # _get_minio_service_config reads the PROJECT-level minio entry; it does not touch
        # self, so __new__ avoids constructing a full manager for this pure read.
        manager = MinioManager.__new__(MinioManager)
        project_data = {
            "deployments": [{"name": "deployment-1"}],
            "services": [{"name": "minio-storage", "config": {"enable-versioning": True}}],
        }
        with caplog.at_level(logging.DEBUG, logger="opi.manager.minio_manager"):
            manager._get_minio_service_config(project_data, "deployment-1")
        assert "{'enable-versioning': True}" not in caplog.text

    def test_database_service_config_read_does_not_dump_dict(self, caplog: pytest.LogCaptureFixture) -> None:
        from opi.manager.database_manager import DatabaseManager

        manager = DatabaseManager.__new__(DatabaseManager)
        project_data = {
            "services": [
                {
                    "name": "namespace-postgresql-database",
                    "config": {"instances": 2, "storage": "5Gi", "privileges": ["SUPERUSER"]},
                }
            ]
        }
        with caplog.at_level(logging.DEBUG, logger="opi.manager.database_manager"):
            manager._get_database_service_config(project_data)
        # The validated config carries a privilege value that must not be dumped wholesale.
        assert "'SUPERUSER'" not in caplog.text
        assert "instances=2" in caplog.text
