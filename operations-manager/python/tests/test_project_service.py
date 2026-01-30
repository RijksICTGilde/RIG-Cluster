"""Tests for opi.services.project_service module.

Focuses on non-trivial logic: data parsing/validation in load_project_from_data,
case-insensitive email matching, singleton behavior, and defensive copy semantics.
"""

import pytest
from opi.services.project_service import (
    ProjectService,
    ProjectUser,
    get_project_service,
)


@pytest.fixture(autouse=True)
def reset_project_service_singleton():
    """Reset ProjectService singleton between tests."""
    ProjectService._instance = None
    ProjectService._initialized = False
    yield
    ProjectService._instance = None
    ProjectService._initialized = False


@pytest.fixture
def service():
    return ProjectService()


class TestSingleton:
    """Singleton pattern matters — double-init would wipe state."""

    def test_second_init_preserves_data(self):
        s1 = ProjectService()
        s1.register("proj", "key", "f.yaml")
        s2 = ProjectService()
        assert s2.get_project("proj") is not None, "Second __init__ must not reset state"

    def test_get_project_service_returns_same_instance(self):
        s1 = get_project_service()
        s1.register("proj", "key", "f.yaml")
        s2 = get_project_service()
        assert s2.get_project("proj") is not None


class TestGetAllProjectsReturnsCopy:
    """get_all_projects must return a copy — callers mutating the dict would corrupt state."""

    def test_mutation_does_not_affect_internal_state(self, service):
        service.register("a", "k1", "a.yaml")
        leaked = service.get_all_projects()
        leaked.pop("a")
        assert service.get_project("a") is not None


class TestLoadProjectFromData:
    """load_project_from_data has real parsing logic: field extraction, validation, user filtering."""

    def test_extracts_api_key_from_nested_config(self, service):
        data = {
            "name": "myproject",
            "config": {"api-key": "secret123"},
        }
        assert service.load_project_from_data(data, "proj.yaml") is True
        assert service.get_project("myproject").api_key == "secret123"

    def test_parses_users_with_email_and_role(self, service):
        data = {
            "name": "proj",
            "config": {"api-key": "k"},
            "users": [
                {"email": "a@b.com", "role": "admin"},
                {"email": "c@d.com", "role": "viewer"},
            ],
        }
        service.load_project_from_data(data, "f.yaml")
        users = service.get_project_users("proj")
        assert len(users) == 2
        assert users[0].email == "a@b.com"
        assert users[1].role == "viewer"

    def test_rejects_missing_name(self, service):
        assert service.load_project_from_data({"config": {"api-key": "k"}}, "f.yaml") is False

    def test_rejects_missing_api_key(self, service):
        assert service.load_project_from_data({"name": "proj", "config": {}}, "f.yaml") is False

    def test_rejects_missing_config_section(self, service):
        assert service.load_project_from_data({"name": "proj"}, "f.yaml") is False

    def test_skips_malformed_users(self, service):
        """Users missing required fields or not dicts should be silently dropped."""
        data = {
            "name": "proj",
            "config": {"api-key": "k"},
            "users": [
                {"email": "valid@x.com", "role": "admin"},
                {"bad": "entry"},  # missing email/role
                "not-a-dict",  # wrong type entirely
                {"email": "no-role@x.com"},  # missing role
            ],
        }
        service.load_project_from_data(data, "f.yaml")
        users = service.get_project_users("proj")
        assert len(users) == 1
        assert users[0].email == "valid@x.com"

    def test_empty_users_list_stores_none(self, service):
        """Empty users list should become None (the register call passes None for empty)."""
        data = {"name": "proj", "config": {"api-key": "k"}, "users": []}
        service.load_project_from_data(data, "f.yaml")
        assert service.get_project("proj").users is None

    def test_preserves_full_project_data(self, service):
        """load_project_from_data must pass the full data dict through to register() so Project.data is set."""
        data = {
            "name": "proj",
            "config": {"api-key": "k", "extra": "value"},
            "deployments": [{"cluster": "local", "namespace": "ns"}],
        }
        service.load_project_from_data(data, "f.yaml")
        project = service.get_project("proj")
        assert project.data is not None, "Project.data should contain the original project data"
        assert project.data["deployments"][0]["cluster"] == "local"


class TestCaseInsensitiveEmailMatching:
    """Authorization checks must be case-insensitive — Keycloak emails can differ in casing."""

    def test_is_user_authorized_case_insensitive(self, service):
        users = [ProjectUser(email="Alice@Example.COM", role="admin")]
        service.register("proj", "k", "f.yaml", users=users)
        assert service.is_user_authorized_for_project("proj", "alice@example.com") is True
        assert service.is_user_authorized_for_project("proj", "ALICE@EXAMPLE.COM") is True

    def test_get_user_role_case_insensitive(self, service):
        users = [ProjectUser(email="Alice@Example.COM", role="dev")]
        service.register("proj", "k", "f.yaml", users=users)
        assert service.get_user_role_for_project("proj", "alice@example.com") == "dev"

    def test_no_users_returns_not_authorized(self, service):
        """Project without users should deny access, not crash."""
        service.register("proj", "k", "f.yaml")
        assert service.is_user_authorized_for_project("proj", "a@b.com") is False
        assert service.get_user_role_for_project("proj", "a@b.com") is None
