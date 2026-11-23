"""``build_component_resource_rows``: per resource field, the value and who manages it (RC-518).

Reads through the same two readers (``extract_component_resources`` and
``get_component_resource_pins``) that the write path and the tuner also use, so a row here
cannot drift from what is actually pinned.
"""

from __future__ import annotations

from typing import Any

from opi.http.render.lotc_switch import COMPONENT_RESOURCE_FIELDS, build_component_resource_rows
from opi.services.project_file_handler import ProjectFileHandler


def _project(component_resources: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema-version": 2,
        "name": "rows",
        "components": [{"name": "api", "type": "single", "resources": component_resources}],
        "deployments": [],
    }


def test_every_field_appears_in_the_declared_order() -> None:
    project = _project({"requests": {"memory": "64Mi", "cpu": "32m"}, "limits": {"memory": "256Mi", "cpu": "1"}})

    rows = build_component_resource_rows(project, "api", ProjectFileHandler())

    assert [row["field"] for row in rows] == [field for field, _ in COMPONENT_RESOURCE_FIELDS]
    assert all(row["pinned"] is False and row["set_at"] is None for row in rows)


def test_a_pinned_field_is_marked_pinned_with_a_date() -> None:
    handler = ProjectFileHandler()
    project = _project({"requests": {"memory": "64Mi"}, "limits": {"memory": "256Mi"}})
    handler.apply_user_resource_intent(project, "api", {"limits_memory": "900Mi"}, origin="portal")

    rows = build_component_resource_rows(project, "api", handler)

    by_field = {row["field"]: row for row in rows}
    assert by_field["limits_memory"]["pinned"] is True
    assert by_field["limits_memory"]["set_at"] is not None
    assert by_field["limits_memory"]["value"] == "900Mi", "the catalog already holds the new value"
    for field in ("requests_memory", "requests_cpu", "limits_cpu"):
        assert by_field[field]["pinned"] is False
        assert by_field[field]["set_at"] is None


def test_without_any_pin_nothing_is_marked_pinned() -> None:
    project = _project({})

    rows = build_component_resource_rows(project, "api", ProjectFileHandler())

    assert all(row["pinned"] is False for row in rows)
