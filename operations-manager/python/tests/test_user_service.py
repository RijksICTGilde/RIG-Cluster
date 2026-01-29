"""Tests for opi.services.user_service module.

Focuses on non-trivial logic: user enrichment (org structuring, role flags),
display name priority chain, organization stats aggregation, access stats
cross-referencing, and email validation in the allowlist.
"""

import pytest
from opi.services.user_service import UserService


@pytest.fixture
def service():
    return UserService()


class TestEnrichUserInfo:
    """_enrich_user_info restructures flat Keycloak claims into nested org data and derives role flags."""

    def test_structures_flat_org_claims_into_nested_dict(self, service):
        result = service._enrich_user_info({
            "email": "a@b.com",
            "organization.name": "Acme",
            "organization.number": "12345",
            "organization.role": "admin",
        })
        assert result["organization"] == {"name": "Acme", "number": "12345", "role": "admin"}
        assert result["has_organization"] is True

    def test_no_org_claims_means_no_organization_key(self, service):
        result = service._enrich_user_info({"email": "a@b.com"})
        assert "organization" not in result
        assert result["has_organization"] is False

    def test_partial_org_claims_only_includes_non_empty(self, service):
        """If only organization.name is set but not number/role, only name appears."""
        result = service._enrich_user_info({
            "email": "a@b.com",
            "organization.name": "Acme",
        })
        assert result["organization"] == {"name": "Acme"}
        assert "number" not in result["organization"]

    def test_role_flag_admin(self, service):
        result = service._enrich_user_info({
            "email": "a@b.com",
            "organization.name": "X",
            "organization.role": "admin",
        })
        assert result["is_admin"] is True
        assert result["is_developer"] is False
        assert result["is_manager"] is False

    def test_role_flag_administrator_variant(self, service):
        result = service._enrich_user_info({
            "email": "a@b.com",
            "organization.name": "X",
            "organization.role": "Administrator",
        })
        assert result["is_admin"] is True

    def test_role_flag_dev(self, service):
        result = service._enrich_user_info({
            "email": "a@b.com",
            "organization.name": "X",
            "organization.role": "dev",
        })
        assert result["is_developer"] is True
        assert result["is_admin"] is False

    def test_role_flag_manager_variants(self, service):
        for role in ("manager", "lead", "supervisor"):
            result = service._enrich_user_info({
                "email": "a@b.com",
                "organization.name": "X",
                "organization.role": role,
            })
            assert result["is_manager"] is True, f"role '{role}' should set is_manager"

    def test_no_role_flags_when_no_role(self, service):
        """Org without role should not inject is_admin/is_developer/is_manager at all."""
        result = service._enrich_user_info({
            "email": "a@b.com",
            "organization.name": "X",
        })
        assert "is_admin" not in result
        assert "is_developer" not in result
        assert "is_manager" not in result


class TestGetDisplayName:
    """Display name priority: name > given+family > preferred_username > email > 'Unknown User'."""

    def test_priority_chain(self, service):
        # All fields present — name wins
        full = {
            "name": "Alice",
            "given_name": "Bob",
            "family_name": "Smith",
            "preferred_username": "alice123",
            "email": "alice@x.com",
        }
        assert service._get_display_name(full) == "Alice"

        # No name — given+family wins
        del full["name"]
        assert service._get_display_name(full) == "Bob Smith"

        # No family name — preferred_username wins (given_name alone is not enough)
        del full["family_name"]
        assert service._get_display_name(full) == "alice123"

        # No preferred_username — email wins
        del full["preferred_username"]
        assert service._get_display_name(full) == "alice@x.com"

        # Nothing left
        del full["email"]
        del full["given_name"]
        assert service._get_display_name(full) == "Unknown User"


class TestOrganizationStats:
    """get_organization_stats aggregates across users — tricky set→list conversion and counting."""

    def test_aggregation_across_multiple_orgs(self, service):
        service.store_user({"email": "a@b.com", "organization.name": "Acme", "organization.role": "admin"})
        service.store_user({"email": "c@d.com", "organization.name": "Acme", "organization.role": "dev"})
        service.store_user({"email": "e@f.com", "organization.name": "Other", "organization.role": "admin"})
        service.store_user({"email": "g@h.com"})  # no org

        stats = service.get_organization_stats()

        assert stats["total_users"] == 4
        assert stats["users_with_organization"] == 3
        assert stats["organizations"]["Acme"]["count"] == 2
        assert stats["organizations"]["Other"]["count"] == 1
        assert stats["roles"]["admin"] == 2
        assert stats["roles"]["dev"] == 1
        # roles per org should be lists (converted from sets for JSON serialization)
        assert isinstance(stats["organizations"]["Acme"]["roles"], list)
        assert set(stats["organizations"]["Acme"]["roles"]) == {"admin", "dev"}


class TestAccessStats:
    """get_access_stats cross-references the user store and the email allowlist."""

    def test_users_with_access_counts_intersection(self, service):
        service.add_allowed_email("a@b.com")
        service.add_allowed_email("c@d.com")
        service.store_user({"email": "a@b.com"})
        service.store_user({"email": "e@f.com"})  # stored but not allowed

        stats = service.get_access_stats()
        assert stats["total_allowed_emails"] == 2
        assert stats["total_stored_users"] == 2
        assert stats["users_with_access"] == 1  # only a@b.com


class TestEmailAllowlistValidation:
    """Email allowlist has validation logic (requires @) and case-insensitive storage."""

    def test_rejects_email_without_at_sign(self, service):
        service.add_allowed_email("invalid")
        assert service.is_email_allowed("invalid") is False

    def test_rejects_empty_string(self, service):
        service.add_allowed_email("")
        assert len(service.get_allowed_emails()) == 0

    def test_stores_and_matches_case_insensitively(self, service):
        service.add_allowed_email("Alice@Example.COM")
        assert service.is_email_allowed("alice@example.com") is True
        assert service.is_email_allowed("ALICE@EXAMPLE.COM") is True

    def test_add_allowed_emails_filters_invalid(self, service):
        service.add_allowed_emails(["a@b.com", "invalid", "", "c@d.com"])
        allowed = service.get_allowed_emails()
        assert len(allowed) == 2
        assert "a@b.com" in allowed
        assert "c@d.com" in allowed

    def test_is_email_allowed_empty_input(self, service):
        """Empty string input should return False, not crash."""
        assert service.is_email_allowed("") is False
