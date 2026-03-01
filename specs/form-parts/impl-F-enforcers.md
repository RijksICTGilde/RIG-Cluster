# Sub-part F: Enforcers

**Layer:** 1 (depends on Sub-part A for protocol definitions)
**Files to create:**
- `opi/forms/editables/enforcers.py`
- `tests/test_editables_enforcers.py`

**Root directory:** `/Users/robbertuittenbroek/IdeaProjects/RIG-Cluster/operations-manager/python/`

---

## Overview

3 sync enforcers implementing the `EditableEnforcer` protocol from `opi/forms/editables/editable.py`.

```python
class EditableEnforcer(Protocol):
    def enforce(self, value: Any, context: dict[str, Any]) -> Any:
        """Enforce business rules. Raises ValueError on violation. Returns value."""
        ...
```

All error messages must be in **Dutch**. Enforcers raise `ValueError` on violation (unlike validators which return error lists).

---

## Enforcers

### AdminRequiredEnforcer

Ensures the users list contains at least one admin. Used on the Users part.

```python
class AdminRequiredEnforcer:
    """Ensures at least one user has role='admin'."""

    def enforce(self, value: Any, context: dict[str, Any]) -> Any:
        """
        Args:
            value: List of user dicts, each with 'email' and 'role' keys.
            context: Not used.

        Returns:
            The value unchanged if valid.

        Raises:
            ValueError: If no user has role='admin'.
        """
        if not value or not isinstance(value, list):
            raise ValueError("Er moet minimaal één gebruiker zijn")
        has_admin = any(
            isinstance(user, dict) and user.get("role") == "admin"
            for user in value
        )
        if not has_admin:
            raise ValueError("Er moet minimaal één administrator zijn")
        return value
```

### UniqueNamesEnforcer

Ensures names in a sequence are unique. Used on Components and Deployments parts.

```python
class UniqueNamesEnforcer:
    """Ensures all items in a sequence have unique values for a given field."""

    def __init__(self, field_name: str = "name") -> None:
        self.field_name = field_name

    def enforce(self, value: Any, context: dict[str, Any]) -> Any:
        """
        Args:
            value: List of dicts (e.g., components, deployments).
            context: Not used.

        Returns:
            The value unchanged if valid.

        Raises:
            ValueError: If duplicate names found.
        """
        if not value or not isinstance(value, list):
            return value
        names: list[str] = []
        for item in value:
            if isinstance(item, dict):
                name = item.get(self.field_name)
                if name:
                    names.append(str(name))
        duplicates = [name for name in set(names) if names.count(name) > 1]
        if duplicates:
            dup_str = ", ".join(sorted(duplicates))
            raise ValueError(f"Dubbele namen gevonden: {dup_str}")
        return value
```

### ServiceDependencyEnforcer

Ensures component `uses-services` references only valid project-level services.

```python
class ServiceDependencyEnforcer:
    """Ensures component services are valid project-level services."""

    def enforce(self, value: Any, context: dict[str, Any]) -> Any:
        """
        Args:
            value: List of service name strings (component's uses-services).
            context: Must contain 'project_services' key with list of valid service names.

        Returns:
            The value unchanged if valid.

        Raises:
            ValueError: If any service is not in the project services list.
        """
        if not value or not isinstance(value, list):
            return value
        project_services = context.get("project_services", [])
        if not project_services:
            return value
        invalid = [s for s in value if s not in project_services]
        if invalid:
            invalid_str = ", ".join(invalid)
            raise ValueError(
                f"Ongeldige services: {invalid_str}. "
                f"Beschikbare services: {', '.join(project_services)}"
            )
        return value
```

---

## Tests: test_editables_enforcers.py

```python
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
        with pytest.raises(ValueError):
            AdminRequiredEnforcer().enforce([], {})

    def test_invalid_none(self):
        with pytest.raises(ValueError):
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
        with pytest.raises(ValueError, match="a@b.c"):
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
```

## Code Style

- Use lowercase type hints: `dict`, `list`
- Use `|` for unions: `str | None`
- Use `from __future__ import annotations`
- Run `ruff check --fix && ruff format` after implementation
- Run `pyright` for type checking
