"""WP3: virtual-key cleanup is a property of the data boundary, not a side effect.

A virtual key (``_services-config``) is a form-transport concern that must never reach
project data. ``_devirtualize`` (run once per ``get_merged_data``) folds each virtual
key onto its real sibling and drops it at *every* level of the structure -- so cleanup
does not depend on which editables happened to run.

This pins that behaviour, because the redundant side-effect strips in
``forms/editables/processor.py`` (which cleaned the virtual key only for components an
editable iteration reached) have been removed; ``_devirtualize`` is now the sole
guarantee. Reverting its recursion to a root-only pop turns these red.
"""

from __future__ import annotations

from typing import Any

from opi.forms.wizard.state import _devirtualize

VIRT_MAPPINGS = {"_services-config": "services"}


def test_root_virtual_key_folded_and_dropped() -> None:
    data: dict[str, Any] = {
        "services": ["keycloak"],
        "_services-config": [{"name": "keycloak", "config": {"template": "x"}}],
    }
    _devirtualize(data, VIRT_MAPPINGS)
    assert "_services-config" not in data
    assert data["services"] == [{"name": "keycloak", "config": {"template": "x"}}]


def test_component_level_virtual_key_dropped() -> None:
    """The root-only pop left ``components[i]._services-config`` behind, which the schema
    (additionalProperties: false on component) rejected."""
    data: dict[str, Any] = {
        "components": [
            {
                "name": "c1",
                "services": ["persistent-storage"],
                "_services-config": [{"reference": "persistent-storage", "config": [{"name": "data"}]}],
            }
        ]
    }
    _devirtualize(data, VIRT_MAPPINGS)
    component = data["components"][0]
    assert "_services-config" not in component
    assert component["services"] == [{"reference": "persistent-storage", "config": [{"name": "data"}]}]


def test_component_without_matching_real_key_still_loses_virtual_key() -> None:
    """The boundary property: the virtual key is popped at every dict level regardless
    of whether there is anything to fold it onto (i.e. regardless of editables). A
    component carrying only a stray virtual key must not leak it."""
    data: dict[str, Any] = {"components": [{"name": "c1", "_services-config": [{"name": "keycloak"}]}]}
    _devirtualize(data, VIRT_MAPPINGS)
    assert "_services-config" not in data["components"][0]


def test_deeply_nested_virtual_key_dropped() -> None:
    """The walk descends through arbitrary nesting, not just the first two levels."""
    data: dict[str, Any] = {
        "deployments": [{"name": "d1", "components": [{"reference": "c1", "_services-config": [{"name": "redis"}]}]}]
    }
    _devirtualize(data, VIRT_MAPPINGS)
    assert "_services-config" not in data["deployments"][0]["components"][0]
