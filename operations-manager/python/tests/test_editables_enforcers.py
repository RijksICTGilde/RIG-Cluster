from __future__ import annotations

import pytest
from opi.forms.editables.enforcers import (
    AdminRequiredEnforcer,
    ComponentServicesEnforcer,
    ServiceDependencyEnforcer,
    UniqueNamesEnforcer,
)


class TestAdminRequiredEnforcer:
    def test_valid_with_admin(self):
        users = [{"email": "admin@example.nl", "role": "admin"}]
        result = AdminRequiredEnforcer().enforce(users, {})
        assert result == users

    def test_valid_multiple_users_one_admin(self):
        users = [
            {"email": "dev@example.nl", "role": "developer"},
            {"email": "admin@example.nl", "role": "admin"},
        ]
        result = AdminRequiredEnforcer().enforce(users, {})
        assert result == users

    def test_invalid_no_admin(self):
        users = [{"email": "dev@example.nl", "role": "developer"}]
        with pytest.raises(ValueError, match="administrator"):
            AdminRequiredEnforcer().enforce(users, {})

    def test_invalid_empty_list(self):
        with pytest.raises(ValueError, match="minimaal één gebruiker"):
            AdminRequiredEnforcer().enforce([], {})

    def test_invalid_none(self):
        with pytest.raises(ValueError, match="minimaal één gebruiker"):
            AdminRequiredEnforcer().enforce(None, {})

    def test_multiple_admins(self):
        users = [
            {"email": "a@b.c", "role": "admin"},
            {"email": "d@e.f", "role": "admin"},
        ]
        result = AdminRequiredEnforcer().enforce(users, {})
        assert len(result) == 2


class TestUniqueNamesEnforcer:
    def test_valid_unique_names(self):
        items = [{"name": "web"}, {"name": "api"}, {"name": "worker"}]
        result = UniqueNamesEnforcer().enforce(items, {})
        assert result == items

    def test_invalid_duplicate_names(self):
        items = [{"name": "web"}, {"name": "api"}, {"name": "web"}]
        with pytest.raises(ValueError, match="web"):
            UniqueNamesEnforcer().enforce(items, {})

    def test_empty_list(self):
        result = UniqueNamesEnforcer().enforce([], {})
        assert result == []

    def test_none_value(self):
        result = UniqueNamesEnforcer().enforce(None, {})
        assert result is None

    def test_custom_field_name(self):
        enforcer = UniqueNamesEnforcer(field_name="email")
        items = [{"email": "a@b.c"}, {"email": "a@b.c"}]
        with pytest.raises(ValueError, match=r"a@b\.c"):
            enforcer.enforce(items, {})

    def test_items_without_name_field(self):
        """Items missing the name field should not cause errors."""
        items = [{"name": "web"}, {"other": "data"}, {"name": "api"}]
        result = UniqueNamesEnforcer().enforce(items, {})
        assert result == items


class TestServiceDependencyEnforcer:
    def test_valid_services(self):
        value = ["publish-on-web", "keycloak"]
        context = {"project_services": ["publish-on-web", "keycloak", "redis"]}
        result = ServiceDependencyEnforcer().enforce(value, context)
        assert result == value

    def test_invalid_service(self):
        value = ["publish-on-web", "nonexistent"]
        context = {"project_services": ["publish-on-web", "keycloak"]}
        with pytest.raises(ValueError, match="nonexistent"):
            ServiceDependencyEnforcer().enforce(value, context)

    def test_empty_uses_services(self):
        result = ServiceDependencyEnforcer().enforce([], {"project_services": ["a"]})
        assert result == []

    def test_empty_project_services(self):
        """If no project services defined, anything goes."""
        result = ServiceDependencyEnforcer().enforce(["a"], {"project_services": []})
        assert result == ["a"]

    def test_no_context(self):
        result = ServiceDependencyEnforcer().enforce(["a"], {})
        assert result == ["a"]

    def test_none_value(self):
        result = ServiceDependencyEnforcer().enforce(None, {})
        assert result is None


class TestComponentServicesEnforcer:
    def _make_data(self, services, components):
        return {"services": services, "components": components}

    def test_valid_services_pass(self):
        data = self._make_data(
            ["keycloak", "redis"],
            [{"name": "web", "uses-services": ["keycloak"]}],
        )
        result = ComponentServicesEnforcer().enforce(data, data)
        assert result is data

    def test_invalid_service_raises_with_component_name(self):
        data = self._make_data(
            ["keycloak"],
            [{"name": "web", "uses-services": ["nonexistent"]}],
        )
        with pytest.raises(ValueError, match=r"Component 'web'.*nonexistent"):
            ComponentServicesEnforcer().enforce(data, data)

    def test_all_default_allowed(self):
        data = self._make_data(
            ["keycloak"],
            [{"name": "web", "uses-services": "__all__"}],
        )
        result = ComponentServicesEnforcer().enforce(data, data)
        assert result is data

    def test_empty_services_skips_validation(self):
        data = self._make_data(
            [],
            [{"name": "web", "uses-services": ["anything"]}],
        )
        result = ComponentServicesEnforcer().enforce(data, data)
        assert result is data

    def test_component_without_uses_services(self):
        data = self._make_data(
            ["keycloak"],
            [{"name": "web"}],
        )
        result = ComponentServicesEnforcer().enforce(data, data)
        assert result is data

    def test_dict_format_services(self):
        """Handles service-keyed dict format like {"keycloak": {...}}."""
        data = self._make_data(
            [{"keycloak": {"config": {}}}],
            [{"name": "web", "uses-services": ["keycloak"]}],
        )
        result = ComponentServicesEnforcer().enforce(data, data)
        assert result is data

    def test_legacy_name_dict_services(self):
        """Handles legacy {"name": "keycloak"} format."""
        data = self._make_data(
            [{"name": "keycloak"}],
            [{"name": "web", "uses-services": ["keycloak"]}],
        )
        result = ComponentServicesEnforcer().enforce(data, data)
        assert result is data
