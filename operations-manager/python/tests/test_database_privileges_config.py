"""Test privileges configuration support.

Validation moved from hand-rolled checks in DatabaseManager to the typed config
model (NamespacePostgresConfig) in RC-5 Phase 2, so invalid config now surfaces as
pydantic.ValidationError instead of bespoke ValueError/TypeError messages.
"""

import pytest
from opi.manager.database_manager import DatabaseManager
from pydantic import ValidationError


def test_get_database_service_config_with_superuser_privilege(mocker):
    """Test that SUPERUSER privilege can be configured."""
    project_data = {
        "name": "test-project",
        "services": [
            {
                "namespace-postgresql-database": {
                    "config": {
                        "image": "ghcr.io/cloudnative-pg/postgresql:17",
                        "instances": 1,
                        "storage": "10Gi",
                        "privileges": ["SUPERUSER"],
                    }
                }
            }
        ],
    }

    # Mock ProjectManager
    mock_pm = mocker.Mock()
    db_manager = DatabaseManager(mock_pm, db_host="localhost", admin_username="admin", admin_password="password")

    config = db_manager._get_database_service_config(project_data)

    assert "privileges" in config
    assert config["privileges"] == ["SUPERUSER"]


def test_get_database_service_config_with_multiple_privileges(mocker):
    """Test that multiple privileges can be configured."""
    project_data = {
        "name": "test-project",
        "services": [
            {
                "namespace-postgresql-database": {
                    "config": {
                        "privileges": ["SUPERUSER", "CREATEDB", "CREATEROLE"],
                    }
                }
            }
        ],
    }

    mock_pm = mocker.Mock()
    db_manager = DatabaseManager(mock_pm, db_host="localhost", admin_username="admin", admin_password="password")

    config = db_manager._get_database_service_config(project_data)

    assert "privileges" in config
    assert config["privileges"] == ["SUPERUSER", "CREATEDB", "CREATEROLE"]


def test_get_database_service_config_default_privileges(mocker):
    """Test that default privileges is empty list."""
    project_data = {
        "name": "test-project",
        "services": [
            {
                "namespace-postgresql-database": {
                    "config": {
                        "image": "ghcr.io/cloudnative-pg/postgresql:17",
                    }
                }
            }
        ],
    }

    mock_pm = mocker.Mock()
    db_manager = DatabaseManager(mock_pm, db_host="localhost", admin_username="admin", admin_password="password")

    config = db_manager._get_database_service_config(project_data)

    assert "privileges" in config
    assert config["privileges"] == []


def test_get_database_service_config_invalid_privilege(mocker):
    """Test that invalid privileges raise ValueError."""
    project_data = {
        "name": "test-project",
        "services": [
            {
                "namespace-postgresql-database": {
                    "config": {
                        "privileges": ["INVALID_PRIVILEGE"],
                    }
                }
            }
        ],
    }

    mock_pm = mocker.Mock()
    db_manager = DatabaseManager(mock_pm, db_host="localhost", admin_username="admin", admin_password="password")

    with pytest.raises(ValidationError, match="privileges"):
        db_manager._get_database_service_config(project_data)


def test_get_database_service_config_privileges_not_list(mocker):
    """Test that privileges must be a list."""
    project_data = {
        "name": "test-project",
        "services": [
            {
                "namespace-postgresql-database": {
                    "config": {
                        "privileges": "SUPERUSER",  # String instead of list
                    }
                }
            }
        ],
    }

    mock_pm = mocker.Mock()
    db_manager = DatabaseManager(mock_pm, db_host="localhost", admin_username="admin", admin_password="password")

    with pytest.raises(ValidationError, match="privileges"):
        db_manager._get_database_service_config(project_data)


def test_get_database_service_config_privilege_not_string(mocker):
    """Test that each privilege must be a string."""
    project_data = {
        "name": "test-project",
        "services": [
            {
                "namespace-postgresql-database": {
                    "config": {
                        "privileges": ["SUPERUSER", 123],  # Number instead of string
                    }
                }
            }
        ],
    }

    mock_pm = mocker.Mock()
    db_manager = DatabaseManager(mock_pm, db_host="localhost", admin_username="admin", admin_password="password")

    with pytest.raises(ValidationError, match="privileges"):
        db_manager._get_database_service_config(project_data)
