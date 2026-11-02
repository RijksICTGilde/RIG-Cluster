"""Tests for authorization decisions derived from project files.

These moved out of test_project_service.py when ProjectService was split: the cache
stayed there (now inside ProjectStore), the authorization checks moved to
opi.services.project_authorization, and the platform-admin allowlist to UserService.
"""

import logging

import pytest
from opi.services.project_authorization import (
    get_user_roles_for_project,
    is_user_authorized_for_project,
)
from opi.services.project_service import ProjectUser, get_project_service
from opi.services.user_service import get_user_service


@pytest.fixture
def service():
    svc = get_project_service()
    svc.clear_all_projects()
    get_user_service()._platform_admin_emails.clear()
    return svc


class TestCaseInsensitiveEmailMatching:
    """Authorization checks must be case-insensitive - Keycloak emails can differ in casing."""

    def test_is_user_authorized_case_insensitive(self, service):
        users = [ProjectUser(email="Alice@Example.COM", role="admin")]
        service.register("proj", "k", "f.yaml", users=users)
        assert is_user_authorized_for_project("proj", "alice@example.com") is True
        assert is_user_authorized_for_project("proj", "ALICE@EXAMPLE.COM") is True

    def test_get_user_roles_case_insensitive(self, service):
        users = [ProjectUser(email="Alice@Example.COM", roles=["developer"])]
        service.register("proj", "k", "f.yaml", users=users)
        assert get_user_roles_for_project("proj", "alice@example.com") == ["developer"]

    def test_no_users_returns_not_authorized(self, service):
        """Project without users should deny access, not crash."""
        service.register("proj", "k", "f.yaml")
        assert is_user_authorized_for_project("proj", "a@b.com") is False
        assert get_user_roles_for_project("proj", "a@b.com") == []


class TestAdminEmails:
    """Admin users bypass per-project user lists and can view all projects."""

    def test_admin_authorized_for_any_project(self, service):
        get_user_service().add_platform_admins(["admin@example.com"])
        service.register("proj", "k", "f.yaml", users=[ProjectUser(email="other@x.com", role="dev")])
        assert is_user_authorized_for_project("proj", "admin@example.com") is True

    def test_admin_authorized_even_without_users(self, service):
        get_user_service().add_platform_admins(["admin@example.com"])
        service.register("proj", "k", "f.yaml")
        assert is_user_authorized_for_project("proj", "admin@example.com") is True

    def test_admin_check_is_case_insensitive(self, service):
        get_user_service().add_platform_admins(["Admin@Example.COM"])
        service.register("proj", "k", "f.yaml")
        assert get_user_service().is_platform_admin("admin@example.com") is True
        assert is_user_authorized_for_project("proj", "ADMIN@EXAMPLE.COM") is True

    def test_non_admin_still_denied(self, service):
        get_user_service().add_platform_admins(["admin@example.com"])
        service.register("proj", "k", "f.yaml", users=[ProjectUser(email="other@x.com", role="dev")])
        assert is_user_authorized_for_project("proj", "nobody@x.com") is False

    def test_is_admin_returns_false_for_unknown(self, service):
        assert get_user_service().is_platform_admin("unknown@x.com") is False

    def test_admin_gets_the_platform_roles_on_any_project(self, service):
        get_user_service().add_platform_admins(["admin@example.com"])
        service.register("proj", "k", "f.yaml", users=[ProjectUser(email="other@x.com", roles=["developer"])])
        assert get_user_roles_for_project("proj", "admin@example.com") == ["admin", "ops"]

    def test_admin_gets_the_platform_roles_even_without_users(self, service):
        get_user_service().add_platform_admins(["admin@example.com"])
        service.register("proj", "k", "f.yaml")
        assert get_user_roles_for_project("proj", "admin@example.com") == ["admin", "ops"]


class TestAdminAccessIsLogged:
    """De INFO-regel is in productie het enige spoor van een platformbeheerder die een
    project opent waar hij geen lid van is."""

    def _info_lines(self, caplog) -> list[str]:
        return [
            record.getMessage()
            for record in caplog.records
            if record.name == "opi.services.project_authorization" and record.levelno == logging.INFO
        ]

    def test_admin_access_is_logged_at_info(self, service, caplog):
        get_user_service().add_platform_admins(["admin@example.com"])
        service.register("proj", "k", "f.yaml")
        with caplog.at_level(logging.INFO, logger="opi.services.project_authorization"):
            assert is_user_authorized_for_project("proj", "admin@example.com") is True
        assert self._info_lines(caplog) == ["User admin@example.com authorized for project proj (admin)"]

    def test_a_listing_logs_nothing_per_project(self, service, caplog):
        # Anders levert een platformbeheerder die het overzicht opent een regel per project
        # op het hele platform, zonder dat er een project geopend is.
        get_user_service().add_platform_admins(["admin@example.com"])
        service.register("proj", "k", "f.yaml")
        with caplog.at_level(logging.INFO, logger="opi.services.project_authorization"):
            assert is_user_authorized_for_project("proj", "admin@example.com", audit=False) is True
        assert self._info_lines(caplog) == []
