"""ProjectView -- one generic entry point to read/query/mutate a project dict.

Rationale (RC-5): the project file is read ~110 different ways today -- hand-rolled
``for x in components: if x.get("name") == n`` loops, ``next(iter(dict))`` key
extractions, three competing path engines. ProjectView consolidates the
*read/locate/mutate* side into a single generic entry point, the counterpart to
``project_store`` (the single entry point for *persisting* the file).

Scope (settled with the reviewer): this is the one entry point and the uniform way
we access project-file data. Two kinds of method live here, both generic:

* **Path / reference access** -- ``get``/``set``/``delete``/``find`` over any path
  or collection.
* **Service-generic access** -- ``service_config(name)`` / ``service_config_model(name)``
  etc.: "give me X of service Y", parameterised by service *name*. This is wanted.

What does NOT belong here is a method hardcoded to one specific service (a
``get_keycloak_config()``). And ProjectView holds no service *meaning*: methods like
``service_config_model`` locate the raw config generically and **delegate**
validation to that service's provider object (where the meaning lives).

Design invariants:

* **The dict stays the source of truth.** ProjectView wraps the dict by reference
  and mutates it in place through the order-preserving path engine, so ruamel
  ``CommentedMap``/``CommentedSeq`` order and comments survive. Persist via
  ``project_store``; never round-trip the whole file through models.
* **Look up by stable identifiers only** -- ``name`` / ``reference`` / ``cluster`` --
  never by generated values (realm names, passwords, generation counters).

It wraps the existing path engine (``opi/forms/editables/path.py`` +
``service_path.py``), which already does field-match (``{F=V}``), mixed string|dict
service lists (``{K}``), and order-preserving find-or-create. That engine is
dependency-light (no forms/services imports), so wrapping it here creates no import
cycle; a later cleanup may relocate it out of ``forms/``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from opi.forms.editables.service_path import (
    find_service_in_list,
    smart_delete_value,
    smart_get_value,
    smart_path_exists,
    smart_set_value,
)

if TYPE_CHECKING:
    from pydantic import BaseModel


class ProjectView:
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

    # --- generic path access -----------------------------------------------------
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

    def set(self, path: str, value: Any) -> ProjectView:
        """Set a value by path, creating intermediates and preserving list order.
        Chainable."""
        smart_set_value(self._data, path, value)
        return self

    def delete(self, path: str) -> None:
        """Delete the value/key at a path (no-op if absent)."""
        smart_delete_value(self._data, path)

    # --- reference-aware record lookup -------------------------------------------
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

    # --- service-generic access ("give me X of service Y", by name) --------------
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
        from opi.services.registry import get_provider
        from opi.services.services_enums import ServiceType

        if not self.uses_service(name):
            return None
        try:
            service_type = ServiceType(name)
        except ValueError:
            return None
        return get_provider(service_type).validate_config(self.service_config(name))
