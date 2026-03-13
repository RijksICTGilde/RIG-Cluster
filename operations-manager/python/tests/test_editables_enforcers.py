from __future__ import annotations

import pytest
from opi.forms.editables.enforcers import (
    AdminRequiredEnforcer,
    ComponentServicesEnforcer,
    ServiceDependencyEnforcer,
    UniqueNamesEnforcer,
)


class TestAdminRequiredEnforcer:
    async def test_valid_with_admin(self):
        users = [{"email": "admin@example.nl", "role": "admin"}]
        result = await AdminRequiredEnforcer().enforce(users, {})
        assert result == users

    async def test_valid_multiple_users_one_admin(self):
        users = [
            {"email": "dev@example.nl", "role": "developer"},
            {"email": "admin@example.nl", "role": "admin"},
        ]
        result = await AdminRequiredEnforcer().enforce(users, {})
        assert result == users

    async def test_invalid_no_admin(self):
        users = [{"email": "dev@example.nl", "role": "developer"}]
        with pytest.raises(ValueError, match="administrator"):
            await AdminRequiredEnforcer().enforce(users, {})

    async def test_invalid_empty_list(self):
        with pytest.raises(ValueError, match="minimaal één gebruiker"):
            await AdminRequiredEnforcer().enforce([], {})

    async def test_invalid_none(self):
        with pytest.raises(ValueError, match="minimaal één gebruiker"):
            await AdminRequiredEnforcer().enforce(None, {})

    async def test_multiple_admins(self):
        users = [
            {"email": "a@b.c", "role": "admin"},
            {"email": "d@e.f", "role": "admin"},
        ]
        result = await AdminRequiredEnforcer().enforce(users, {})
        assert len(result) == 2


class TestUniqueNamesEnforcer:
    async def test_valid_unique_names(self):
        items = [{"name": "web"}, {"name": "api"}, {"name": "worker"}]
        result = await UniqueNamesEnforcer().enforce(items, {})
        assert result == items

    async def test_invalid_duplicate_names(self):
        items = [{"name": "web"}, {"name": "api"}, {"name": "web"}]
        with pytest.raises(ValueError, match="web"):
            await UniqueNamesEnforcer().enforce(items, {})

    async def test_empty_list(self):
        result = await UniqueNamesEnforcer().enforce([], {})
        assert result == []

    async def test_none_value(self):
        result = await UniqueNamesEnforcer().enforce(None, {})
        assert result is None

    async def test_custom_field_name(self):
        enforcer = UniqueNamesEnforcer(field_name="email")
        items = [{"email": "a@b.c"}, {"email": "a@b.c"}]
        with pytest.raises(ValueError, match=r"a@b\.c"):
            await enforcer.enforce(items, {})

    async def test_items_without_name_field(self):
        """Items missing the name field should not cause errors."""
        items = [{"name": "web"}, {"other": "data"}, {"name": "api"}]
        result = await UniqueNamesEnforcer().enforce(items, {})
        assert result == items


class TestServiceDependencyEnforcer:
    async def test_valid_services(self):
        value = ["publish-on-web", "keycloak"]
        context = {"project_services": ["publish-on-web", "keycloak", "redis"]}
        result = await ServiceDependencyEnforcer().enforce(value, context)
        assert result == value

    async def test_invalid_service(self):
        value = ["publish-on-web", "nonexistent"]
        context = {"project_services": ["publish-on-web", "keycloak"]}
        with pytest.raises(ValueError, match="nonexistent"):
            await ServiceDependencyEnforcer().enforce(value, context)

    async def test_empty_uses_services(self):
        result = await ServiceDependencyEnforcer().enforce([], {"project_services": ["a"]})
        assert result == []

    async def test_empty_project_services(self):
        """If no project services defined, anything goes."""
        result = await ServiceDependencyEnforcer().enforce(["a"], {"project_services": []})
        assert result == ["a"]

    async def test_no_context(self):
        result = await ServiceDependencyEnforcer().enforce(["a"], {})
        assert result == ["a"]

    async def test_none_value(self):
        result = await ServiceDependencyEnforcer().enforce(None, {})
        assert result is None


class TestComponentServicesEnforcer:
    def _make_data(self, services, components):
        return {"services": services, "components": components}

    async def test_valid_services_pass(self):
        data = self._make_data(
            ["keycloak", "redis"],
            [{"name": "web", "services": ["keycloak"]}],
        )
        result = await ComponentServicesEnforcer().enforce(data, data)
        assert result is data

    async def test_invalid_service_raises_with_component_name(self):
        data = self._make_data(
            ["keycloak"],
            [{"name": "web", "services": ["nonexistent"]}],
        )
        with pytest.raises(ValueError, match=r"Component 'web'.*nonexistent"):
            await ComponentServicesEnforcer().enforce(data, data)

    async def test_all_default_allowed(self):
        data = self._make_data(
            ["keycloak"],
            [{"name": "web", "services": "__all__"}],
        )
        result = await ComponentServicesEnforcer().enforce(data, data)
        assert result is data

    async def test_empty_services_skips_validation(self):
        data = self._make_data(
            [],
            [{"name": "web", "services": ["anything"]}],
        )
        result = await ComponentServicesEnforcer().enforce(data, data)
        assert result is data

    async def test_component_without_uses_services(self):
        data = self._make_data(
            ["keycloak"],
            [{"name": "web"}],
        )
        result = await ComponentServicesEnforcer().enforce(data, data)
        assert result is data

    async def test_dict_format_services(self):
        """Handles service-keyed dict format like {"keycloak": {...}}."""
        data = self._make_data(
            [{"keycloak": {"config": {}}}],
            [{"name": "web", "services": ["keycloak"]}],
        )
        result = await ComponentServicesEnforcer().enforce(data, data)
        assert result is data

    async def test_legacy_name_dict_services(self):
        """Handles legacy {"name": "keycloak"} format."""
        data = self._make_data(
            [{"name": "keycloak"}],
            [{"name": "web", "services": ["keycloak"]}],
        )
        result = await ComponentServicesEnforcer().enforce(data, data)
        assert result is data
