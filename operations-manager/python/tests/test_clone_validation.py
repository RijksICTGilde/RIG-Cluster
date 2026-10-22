"""Tests for the :validate-clone pre-flight check (opi/manager/clone_validation.py).

The endpoint used to call ``project_manager._clone_manager``, an attribute that no
longer exists, so every call answered with an AttributeError dressed up as a 500.
These tests pin the replacement: a pure function over the project file that says per
check what is missing.
"""

import pytest
from opi.manager.clone_validation import resolve_clone_source_deployment, validate_clone_readiness
from opi.utils.naming import generate_bucket_name, generate_database_name


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


class TestResolveCloneSourceDeployment:
    """The function the provisioning path (database, bucket, volume) shares with
    the dry run, so a reference judged valid there is the same one the clone itself
    is allowed to act on."""

    def test_existing_reference_returns_the_source_deployment(self):
        project = {"deployments": [{"name": "acceptatie", "cluster": "local"}, {"name": "productie"}]}
        source = resolve_clone_source_deployment(project, "acceptatie", "productie")
        assert source == {"name": "acceptatie", "cluster": "local"}

    def test_empty_reference_is_refused(self):
        project = {"deployments": [{"name": "productie"}]}
        with pytest.raises(ValueError, match="without a reference"):
            resolve_clone_source_deployment(project, "", "productie")

    def test_self_reference_is_refused(self):
        project = {"deployments": [{"name": "productie"}]}
        with pytest.raises(ValueError, match="clones from itself"):
            resolve_clone_source_deployment(project, "productie", "productie")

    def test_reference_outside_the_project_is_refused(self):
        """A reference like 'ander-project-prod' would, unchecked, let the provisioning
        path compose another project's database or bucket name (see module docstring)."""
        project = {"deployments": [{"name": "productie"}]}
        with pytest.raises(ValueError, match="not found in project"):
            resolve_clone_source_deployment(project, "ander-project-prod", "productie")

    def test_a_reference_with_a_separator_cannot_compose_another_projects_names(self):
        """The source names glue project and deployment with a separator, so a reference
        carrying that separator composes the names of another project's deployment. The
        guard refuses it, which is what closes the collision."""
        assert generate_database_name("mijn", "project-prod") == generate_database_name("mijn-project", "prod")
        assert generate_bucket_name("mijn", "project-prod") == generate_bucket_name("mijn-project", "prod")

        project = {"name": "mijn", "deployments": [{"name": "prod"}, {"name": "acceptatie"}]}
        with pytest.raises(ValueError, match="not found in project"):
            resolve_clone_source_deployment(project, "project-prod", "acceptatie")

        source = resolve_clone_source_deployment(project, "prod", "acceptatie")
        assert generate_database_name("mijn", source["name"]) == "mijn_prod"
