"""Tests for the :validate-clone pre-flight check (opi/manager/clone_validation.py).

The endpoint used to call ``project_manager._clone_manager``, an attribute that no
longer exists, so every call answered with an AttributeError dressed up as a 500.
These tests pin the replacement: a pure function over the project file that says per
check what is missing.
"""

from opi.manager.clone_validation import validate_clone_readiness


def _project(deployment: dict, **extra) -> dict:
    return {"name": "proj", "deployments": [deployment], **extra}


def _names(result: dict) -> dict[str, str]:
    """Map check name -> status, for readable assertions."""
    return {check["name"]: check["status"] for check in result["validation"]["checks"]}


class TestNoCloneConfiguration:
    def test_unknown_deployment_fails_on_existence(self):
        result = validate_clone_readiness({"deployments": []}, "productie")
        assert result["validation"]["passed"] is False
        assert _names(result) == {"deployment_exists": "failed"}

    def test_deployment_without_clone_from_fails(self):
        result = validate_clone_readiness(_project({"name": "productie"}), "productie")
        assert result["validation"]["passed"] is False
        assert _names(result) == {"clone_configuration": "failed"}

    def test_clone_from_as_string_is_refused(self):
        result = validate_clone_readiness(_project({"name": "productie", "clone-from": "acceptatie"}), "productie")
        assert result["validation"]["passed"] is False
        assert "mapping" in result["validation"]["checks"][0]["message"]

    def test_unknown_type_is_named(self):
        result = validate_clone_readiness(
            _project({"name": "productie", "clone-from": {"type": "magic", "reference": "x"}}),
            "productie",
        )
        assert result["validation"]["passed"] is False
        assert _names(result)["clone_type"] == "failed"


class TestDeploymentSource:
    def test_existing_source_passes(self):
        project = {
            "deployments": [
                {"name": "acceptatie"},
                {"name": "productie", "clone-from": {"type": "deployment", "reference": "acceptatie"}},
            ]
        }
        result = validate_clone_readiness(project, "productie")
        assert result["validation"]["passed"] is True
        assert _names(result)["source_deployment_exists"] == "success"

    def test_missing_source_fails(self):
        project = _project({"name": "productie", "clone-from": {"type": "deployment", "reference": "weg"}})
        result = validate_clone_readiness(project, "productie")
        assert result["validation"]["passed"] is False
        assert _names(result)["source_deployment_exists"] == "failed"

    def test_self_reference_fails(self):
        project = _project({"name": "productie", "clone-from": {"type": "deployment", "reference": "productie"}})
        result = validate_clone_readiness(project, "productie")
        assert result["validation"]["passed"] is False

    def test_completed_once_clone_is_reported_but_still_valid(self):
        project = {
            "deployments": [
                {"name": "acceptatie"},
                {
                    "name": "productie",
                    "clone-from": {
                        "type": "deployment",
                        "reference": "acceptatie",
                        "mode": "once",
                        "status": {"completed": True},
                    },
                },
            ]
        }
        result = validate_clone_readiness(project, "productie")
        assert result["validation"]["passed"] is True
        assert _names(result)["clone_pending"] == "success"


class TestRemoteSource:
    def _project_with_remote(self, remote: dict) -> dict:
        return _project(
            {"name": "productie", "clone-from": {"type": "remote-source", "reference": "extern"}},
            **{"remote-sources": [remote]},
        )

    def test_complete_remote_source_passes(self):
        project = self._project_with_remote(
            {
                "name": "extern",
                "chisel": {"server-url": "https://chisel.example.org"},
                "services": {"postgresql-database": {"host": "db"}},
            }
        )
        result = validate_clone_readiness(project, "productie")
        assert result["validation"]["passed"] is True
        assert _names(result) == {
            "clone_configuration": "success",
            "remote_source_exists": "success",
            "chisel_configuration": "success",
            "services_configuration": "success",
        }

    def test_unknown_remote_source_fails(self):
        project = self._project_with_remote({"name": "andere"})
        result = validate_clone_readiness(project, "productie")
        assert _names(result)["remote_source_exists"] == "failed"

    def test_remote_source_without_chisel_and_services_fails_both(self):
        project = self._project_with_remote({"name": "extern"})
        result = validate_clone_readiness(project, "productie")
        assert result["validation"]["passed"] is False
        assert _names(result)["chisel_configuration"] == "failed"
        assert _names(result)["services_configuration"] == "failed"


class TestBackupSource:
    def test_complete_backup_items_pass(self):
        project = _project(
            {
                "name": "productie",
                "clone-from": {
                    "type": "backup",
                    "reference": "acceptatie",
                    "backup_items": [{"resource_type": "database", "snapshot_id": "41bbdb"}],
                },
            }
        )
        result = validate_clone_readiness(project, "productie")
        assert result["validation"]["passed"] is True
        assert _names(result)["backup_items"] == "success"

    def test_backup_without_items_fails(self):
        project = _project({"name": "productie", "clone-from": {"type": "backup", "reference": "acceptatie"}})
        result = validate_clone_readiness(project, "productie")
        assert result["validation"]["passed"] is False

    def test_backup_item_without_snapshot_fails(self):
        project = _project(
            {
                "name": "productie",
                "clone-from": {
                    "type": "backup",
                    "reference": "acceptatie",
                    "backup_items": [{"resource_type": "database"}],
                },
            }
        )
        result = validate_clone_readiness(project, "productie")
        assert result["validation"]["passed"] is False
        assert _names(result)["backup_items"] == "failed"
