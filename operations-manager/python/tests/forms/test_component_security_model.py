"""Unit tests for the hidden per-component ``security`` block.

Covers the Pydantic model in ``opi.forms.models.project_file`` (kebab-case
YAML key aliases) and verifies the field is genuinely optional so existing
projects keep validating without modification.
"""

from __future__ import annotations

from opi.forms.models.project_file import ComponentModel, SecurityConfig


def test_component_accepts_full_security_block() -> None:
    """All three fields set via YAML kebab-case keys."""
    component = ComponentModel.model_validate(
        {
            "name": "web",
            "security": {
                "run-as-user": 999,
                "run-as-group": 999,
                "fs-group": 999,
            },
        }
    )

    assert component.security is not None
    assert component.security.run_as_user == 999
    assert component.security.run_as_group == 999
    assert component.security.fs_group == 999


def test_component_without_security_defaults_to_none() -> None:
    """No ``security`` block in YAML => attribute is ``None``."""
    component = ComponentModel.model_validate({"name": "web"})
    assert component.security is None


def test_component_with_partial_security_only_sets_provided_fields() -> None:
    """Only ``run-as-user`` set; others stay ``None``."""
    component = ComponentModel.model_validate(
        {
            "name": "web",
            "security": {"run-as-user": 1002},
        }
    )

    assert component.security is not None
    assert component.security.run_as_user == 1002
    assert component.security.run_as_group is None
    assert component.security.fs_group is None


def test_security_config_round_trips_to_yaml_dict_with_aliases() -> None:
    """Dumping with ``by_alias=True`` must emit kebab-case keys (YAML convention)."""
    cfg = SecurityConfig(run_as_user=1, run_as_group=2, fs_group=3)
    dumped = cfg.model_dump(by_alias=True, exclude_none=True)
    assert dumped == {"run-as-user": 1, "run-as-group": 2, "fs-group": 3}


def test_security_config_rejects_negative_uid() -> None:
    """UIDs/GIDs are unsigned; ``ge=0`` constraint must reject negatives."""
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SecurityConfig(run_as_user=-1)
