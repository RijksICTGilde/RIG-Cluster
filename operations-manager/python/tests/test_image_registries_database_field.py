"""The database's registry can be changed through the form (RC-498).

The field existed only in the config model, so the API or a hand-edited project file were
the only ways in. At ``algor-odc`` that was one of the references holding the registry in
place, and the UI showed nothing to change.

The empty option means: no registry of our own. The image then travels the project's own
registries when it falls under one, and the cluster's shared proxy otherwise, exactly as a
component's image does.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from opi.forms.editables.processor import EditableFormProcessor
from opi.forms.editables.service_path import smart_get_value
from opi.forms.layout import layout_field_names
from opi.forms.visualizers.bridge import editable_to_form_field, should_render_editable
from opi.forms.visualizers.providers import AUTOMATIC_REGISTRY_LABEL
from opi.services.catalog.base import ConfigLayer
from opi.services.catalog.namespace_postgres.visualizers import POSTGRESQL_REGISTRY
from opi.services.registry import get_service
from opi.services.services_enums import ServiceType

_DB = ServiceType.NAMESPACE_POSTGRESQL_DATABASE.value
_PATH = f"services/{_DB}/config/registry"
_REGISTRY = {"name": "code-overheid", "display-name": "Code Overheid", "upstream": "code.overheid.nl/team"}


def _section() -> Any:
    section = get_service(ServiceType.NAMESPACE_POSTGRESQL_DATABASE).config_form_section(ConfigLayer.PROJECT)
    assert section is not None
    return section


def _project(registries: list[dict[str, Any]], registry: str | None = None) -> dict[str, Any]:
    config: dict[str, Any] = {"image": "code.overheid.nl/team/postgres:17", "instances": 1, "storage": "1Gi"}
    if registry is not None:
        config["registry"] = registry
    return {
        "name": "toets",
        "services": [
            {"name": "image-registries", "config": {"registries": registries}},
            {"name": _DB, "config": config},
        ],
    }


def _submission(registry: str) -> dict[str, Any]:
    return {"_services-config": {_DB: {"config": {"instances": 1, "storage": "1Gi", "registry": registry}}}}


async def _save(project: dict[str, Any], registry: str) -> dict[str, Any]:
    saved, errors = await EditableFormProcessor().process_json_submission(
        _submission(registry), _section().editables, copy.deepcopy(project), edit_mode=True
    )
    assert not errors, errors
    return saved


def test_the_field_is_on_the_screen() -> None:
    """Registered as well as drawn: one half without the other is the trap."""
    section = _section()
    assert _PATH in [vis.editable.yaml_path for vis in section.editables]
    assert _PATH in layout_field_names(section.layout)


@pytest.mark.asyncio
async def test_a_chosen_registry_lands_in_the_config() -> None:
    saved = await _save(_project([_REGISTRY]), "code-overheid")
    assert smart_get_value(saved, _PATH) == "code-overheid"


@pytest.mark.asyncio
async def test_the_empty_option_removes_the_key() -> None:
    saved = await _save(_project([_REGISTRY], registry="code-overheid"), "")
    assert smart_get_value(saved, _PATH) is None
    assert "registry" not in smart_get_value(saved, f"services/{_DB}/config")


def test_without_registries_the_field_is_absent() -> None:
    """``hidden_without_options``: an empty option list is not a field."""
    assert should_render_editable(POSTGRESQL_REGISTRY, _project([])) is False
    assert should_render_editable(POSTGRESQL_REGISTRY, _project([_REGISTRY])) is True


def test_a_name_that_no_longer_exists_stays_visible() -> None:
    """Otherwise the field sits on "Automatisch" while the file says something else."""
    field = editable_to_form_field(POSTGRESQL_REGISTRY, _project([_REGISTRY], registry="weg"))
    labels = [option["label"] for option in field.options or []]
    assert "weg (bestaat niet meer)" in labels
    assert field.value == "weg"


@pytest.mark.asyncio
async def test_a_name_that_no_longer_exists_does_not_mutely_block_the_form() -> None:
    """The form does not hold such a value back; whoever leaves it gets the guard from
    ``references.py``, the database included (``test_image_registries_weg_terug.py``)."""
    saved = await _save(_project([_REGISTRY], registry="weg"), "code-overheid")
    assert smart_get_value(saved, _PATH) == "code-overheid"


def test_the_empty_label_is_right_for_a_database() -> None:
    """Without a choice the database image travels this project's registries like any other
    image (``resolve_project_image`` in ``infrastructure.py``), so the label must not promise
    a public route."""
    field = editable_to_form_field(POSTGRESQL_REGISTRY, _project([_REGISTRY]))
    assert (field.options or [])[0] == {"value": "", "label": AUTOMATIC_REGISTRY_LABEL}
