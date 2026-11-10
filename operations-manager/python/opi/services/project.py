"""Project: the one generic entry point to read, query and mutate a project dict.

The counterpart to ``project_store``, which is the one entry point for persisting the
file. Two kinds of method live here, both generic: path and reference access
(``get``/``set``/``delete``/``find`` over any path or collection), and service-generic
access (``service_config(name)``, ``service_config_model(name)``: "give me X of service
Y", parameterised by service NAME).

A method hardcoded to one service (a ``get_keycloak_config()``) does NOT belong here, and
Project holds no service meaning: ``service_config_model`` locates the raw config
generically and DELEGATES validation to that service's provider object.

Two invariants:

* **The dict stays the source of truth.** Project wraps the dict by reference and mutates
  it in place through the order-preserving path engine, so ruamel ``CommentedMap`` and
  ``CommentedSeq`` order and comments survive. Persist through ``project_store``; never
  round-trip the whole file through models.
* **Look up by stable identifiers only** (``name``, ``reference``, ``cluster``), never by
  generated values (realm names, passwords, generation counters).

It wraps the path engine in ``opi/forms/editables/path.py`` and ``service_path.py``, which
already does field-match (``{F=V}``), mixed string-or-dict service lists (``{K}``) and
order-preserving find-or-create.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from opi.forms.editables.service_path import (
    find_service_in_list,
    smart_delete_value,
    smart_get_value,
    smart_path_exists,
    smart_set_value,
)

if TYPE_CHECKING:
    from pydantic import BaseModel

    from opi.services.project_service import ProjectSummary, ProjectUser

logger = logging.getLogger(__name__)


class Project:
    """A thin, generic, reference-aware view over a parsed project dict.

    Wraps the dict by reference and mutates it in place (order-preserving). Get the
    underlying dict back via ``.data`` to persist it through ``project_store``.
    """

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    @property
    def data(self) -> dict[str, Any]:
        """The wrapped dict (the source of truth), for persistence."""
        return self._data

    def get(self, path: str, default: Any = None) -> Any:
        """Read a value by path, e.g. ``components{name=api}/ports/inbound[0]`` or
        ``services/keycloak/config/template``. Returns ``default`` if absent.

        Path syntax (from the wrapped engine): ``key``, ``key[N]``, ``key[*]``,
        ``key{K}`` (dict-key in a mixed string|dict list), ``key{F=V}`` (field
        match). Segments are ``/``-separated.
        """
        value = smart_get_value(self._data, path)
        return default if value is None else value

    def exists(self, path: str) -> bool:
        """Whether a path resolves to a present (truthy) value."""
        return smart_path_exists(self._data, path)

    def set(self, path: str, value: Any) -> Project:
        """Set a value by path, creating intermediates and preserving list order.
        Chainable."""
        smart_set_value(self._data, path, value)
        return self

    def delete(self, path: str) -> None:
        """Delete the value/key at a path (no-op if absent)."""
        smart_delete_value(self._data, path)

    # Programmatic find-by-identifier over a list of records. The path syntax
    # `{F=V}` matches a single field; these support multiple fields and "find all".
    @staticmethod
    def locate(items: list[Any], **by: Any) -> dict[str, Any] | None:
        """First dict in ``items`` whose fields equal all of ``by``, or None. Look
        up by stable identifiers only. E.g. ``locate(deployment["components"],
        reference="frontend")``."""
        for item in items or []:
            if isinstance(item, dict) and all(item.get(k) == v for k, v in by.items()):
                return item
        return None

    @staticmethod
    def locate_all(items: list[Any], **by: Any) -> list[dict[str, Any]]:
        """All dicts in ``items`` matching ``by`` (order preserved)."""
        return [item for item in items or [] if isinstance(item, dict) and all(item.get(k) == v for k, v in by.items())]

    def find(self, collection: str, **by: Any) -> dict[str, Any] | None:
        """First record in a top-level collection (``components``, ``deployments``,
        ``users``, ``repositories``, ...) matching ``by`` -- by stable identifier
        (``name``/``reference``), never a generated value."""
        return self.locate(self._data.get(collection) or [], **by)

    def find_all(self, collection: str, **by: Any) -> list[dict[str, Any]]:
        """All records in a top-level collection matching ``by`` (order preserved)."""
        return self.locate_all(self._data.get(collection) or [], **by)

    # Generic over ALL services (name is a parameter). No service is special-cased.
    def service_entry(self, name: str) -> dict[str, Any] | str | None:
        """The raw services-list entry for ``name`` (a bare string, a
        ``{name: {...}}`` dict, or None if the project does not use the service)."""
        services = self._data.get("services")
        if not isinstance(services, list):
            return None
        _, entry = find_service_in_list(services, name)
        return entry

    def uses_service(self, name: str) -> bool:
        """Whether the project declares the service at all (string or dict form)."""
        return self.service_entry(name) is not None

    def service_config(self, name: str) -> Any:
        """The raw config of a project-level service (dict/list), or None if the
        service is absent or has no config block."""
        return smart_get_value(self._data, f"services/{name}/config")

    def service_config_model(self, name: str) -> BaseModel | None:
        """Validated, typed config for a service, or None if the project does not use
        it. Locates the raw config generically and **delegates** validation to the
        service's provider (fail-closed). Raises ``pydantic.ValidationError`` on bad
        values, or ``TypeError`` if the service takes no config; unknown service
        names return None.
        """
        # Lazy imports keep this module dependency-light and cycle-free.
        from opi.services.registry import get_service
        from opi.services.services_enums import ServiceType

        if not self.uses_service(name):
            return None
        try:
            service_type = ServiceType(name)
        except ValueError:
            return None
        return get_service(service_type).validate_config(self.service_config(name))

    def get_summary(self, filename: str) -> ProjectSummary | None:
        """Lightweight typed projection of this project (name, members, api-key).

        Returns None when the project lacks a name.

        The api-key comes back AS STORED, so possibly AGE-encrypted: decrypting it stays
        with the caller (ProjectService). From schema version 2.12 on it is absent and the
        field is None, because the key is then a token of the ``project-api`` service
        account.
        """
        # Lazy import avoids the project <-> project_service cycle.
        from opi.services.project_service import ProjectSummary

        name = self._data.get("name")
        config = self._data.get("config") or {}
        api_key = config.get("api-key")
        if not name:
            return None
        users_data = self._data.get("users") or []
        users = [member for member in (member_from(entry) for entry in users_data) if member is not None]
        return ProjectSummary(
            name=str(name),
            api_key=str(api_key) if api_key else None,
            filename=filename,
            users=users or None,
            data=self._data,
        )


def member_from(entry: Any) -> ProjectUser | None:
    """One member out of the raw list, or None when the entry is not one.

    A member that does not parse is skipped and not raised: half a line in ``users`` would
    otherwise keep the WHOLE project out of the cache, and its API unreachable with it.
    """
    # Lazy import, same cycle reason as in get_summary.
    from opi.services.project_service import ProjectUser

    if not isinstance(entry, dict):
        return None
    try:
        return ProjectUser.model_validate(entry)
    except ValidationError as e:
        logger.warning("Lid in het projectbestand overgeslagen: %s", e)
        return None
