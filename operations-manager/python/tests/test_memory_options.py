"""Tests for memory options, validation, and enforcement."""

from __future__ import annotations

import pytest
from opi.forms.editables.enforcers import ComponentServicesEnforcer, FieldError
from opi.forms.editables.validators import MemoryRangeValidator, MemoryRequestRangeValidator
from opi.forms.visualizers.providers import (
    MemoryOptionsProvider,
    MemoryRequestOptionsProvider,
    get_memory_steps,
)
from opi.services.resource_analyzer import parse_k8s_memory_to_mi

# ---------------------------------------------------------------------------
# parse_k8s_memory_to_mi
# ---------------------------------------------------------------------------


class TestParseK8sMemoryToMi:
    def test_mi_unit(self):
        assert parse_k8s_memory_to_mi("512Mi") == 512.0

    def test_gi_unit(self):
        assert parse_k8s_memory_to_mi("1Gi") == 1024.0

    def test_fractional_gi(self):
        assert parse_k8s_memory_to_mi("0.5Gi") == 512.0

    def test_plain_bytes(self):
        result = parse_k8s_memory_to_mi("268435456")
        assert abs(result - 256.0) < 0.01

    def test_ki_unit(self):
        result = parse_k8s_memory_to_mi("32768Ki")
        assert abs(result - 32.0) < 0.01

    def test_ti_unit(self):
        # Ti (tebibyte) is a valid k8s binary unit: 1Ti = 1024 * 1024 Mi.
        assert parse_k8s_memory_to_mi("512Ti") == 512 * 1024.0 * 1024

    def test_invalid_unit(self):
        with pytest.raises(ValueError, match="Unknown memory unit"):
            parse_k8s_memory_to_mi("512Xi")

    def test_invalid_format(self):
        with pytest.raises(ValueError, match="Cannot parse"):
            parse_k8s_memory_to_mi("not-a-number")


# ---------------------------------------------------------------------------
# MemoryRangeValidator
# ---------------------------------------------------------------------------


class TestMemoryRangeValidator:
    def setup_method(self):
        self.validator = MemoryRangeValidator(min_mi=25, max_mi=1024)

    def test_valid_minimum(self):
        assert self.validator.validate("25Mi") == []

    def test_valid_maximum(self):
        assert self.validator.validate("1Gi") == []

    def test_valid_mid_range(self):
        assert self.validator.validate("384Mi") == []

    def test_valid_standard_step(self):
        assert self.validator.validate("256Mi") == []

    def test_below_minimum(self):
        errors = self.validator.validate("16Mi")
        assert len(errors) == 1
        assert "25Mi" in errors[0]

    def test_above_maximum(self):
        errors = self.validator.validate("2Gi")
        assert len(errors) == 1
        assert "1024" in errors[0]

    def test_none_is_valid(self):
        assert self.validator.validate(None) == []

    def test_empty_string_is_valid(self):
        assert self.validator.validate("") == []

    def test_invalid_format(self):
        errors = self.validator.validate("lots")
        assert len(errors) == 1
        assert "Ongeldige geheugenwaarde" in errors[0]

    def test_gi_within_range(self):
        assert self.validator.validate("0.5Gi") == []


# ---------------------------------------------------------------------------
# MemoryOptionsProvider
# ---------------------------------------------------------------------------


class TestMemoryOptionsProvider:
    def test_standard_steps(self):
        options = MemoryOptionsProvider().get_options()
        values = [o["value"] for o in options]
        assert values == [v for v, _ in get_memory_steps()]

    def test_current_value_in_steps_no_duplicate(self):
        options = MemoryOptionsProvider(current_value="256Mi").get_options()
        values = [o["value"] for o in options]
        assert values.count("256Mi") == 1

    def test_current_value_not_in_steps_injected(self):
        options = MemoryOptionsProvider(current_value="384Mi").get_options()
        values = [o["value"] for o in options]
        assert "384Mi" in values
        # Should be between 256Mi and 512Mi
        idx_384 = values.index("384Mi")
        idx_256 = values.index("256Mi")
        idx_512 = values.index("512Mi")
        assert idx_256 < idx_384 < idx_512

    def test_current_value_below_all_steps(self):
        options = MemoryOptionsProvider(current_value="16Mi").get_options()
        values = [o["value"] for o in options]
        assert values[0] == "16Mi"

    def test_current_value_above_all_steps(self):
        options = MemoryOptionsProvider(current_value="5Gi").get_options()
        values = [o["value"] for o in options]
        assert values[-1] == "5Gi"

    def test_current_value_invalid_format_ignored(self):
        options = MemoryOptionsProvider(current_value="invalid").get_options()
        values = [o["value"] for o in options]
        assert values == [v for v, _ in get_memory_steps()]

    def test_current_value_none(self):
        options = MemoryOptionsProvider(current_value=None).get_options()
        values = [o["value"] for o in options]
        assert values == [v for v, _ in get_memory_steps()]

    def test_injected_label_is_mb(self):
        options = MemoryOptionsProvider(current_value="384Mi").get_options()
        opt = next(o for o in options if o["value"] == "384Mi")
        assert opt["label"] == "384 Mi"


# ---------------------------------------------------------------------------
# MemoryRequestOptionsProvider
# ---------------------------------------------------------------------------


class TestMemoryRequestOptionsProvider:
    @pytest.fixture(autouse=True)
    def _patch_cluster(self, monkeypatch):
        monkeypatch.setattr("opi.core.config.settings", type("S", (), {"CLUSTER_MANAGER": "local"})())

    def test_options_capped_at_request_max(self):
        options = MemoryRequestOptionsProvider().get_options()
        values = [o["value"] for o in options]
        assert "1Gi" in values
        assert "2Gi" not in values
        assert "4Gi" not in values

    def test_current_value_above_cap_still_injected(self):
        """Tuner-assigned values above the cap should still appear in the dropdown."""
        options = MemoryRequestOptionsProvider(current_value="1536Mi").get_options()
        values = [o["value"] for o in options]
        assert "1536Mi" in values


# ---------------------------------------------------------------------------
# MemoryRequestRangeValidator
# ---------------------------------------------------------------------------


class TestMemoryRequestRangeValidator:
    @pytest.fixture(autouse=True)
    def _patch_cluster(self, monkeypatch):
        monkeypatch.setattr("opi.core.config.settings", type("S", (), {"CLUSTER_MANAGER": "local"})())

    def test_valid_at_request_cap(self):
        assert MemoryRequestRangeValidator(min_mi=25).validate("1Gi") == []

    def test_above_request_cap_rejected(self):
        errors = MemoryRequestRangeValidator(min_mi=25).validate("2Gi")
        assert len(errors) == 1
        assert "1024" in errors[0]

    def test_below_minimum_rejected(self):
        errors = MemoryRequestRangeValidator(min_mi=25).validate("16Mi")
        assert len(errors) == 1

    def test_within_range_valid(self):
        assert MemoryRequestRangeValidator(min_mi=25).validate("512Mi") == []


# ---------------------------------------------------------------------------
# ComponentServicesEnforcer — memory request/limit validation
# ---------------------------------------------------------------------------


class TestComponentServicesEnforcerMemory:
    def _make_data(self, request: str, limit: str) -> dict:
        return {
            "services": ["publish-on-web"],
            "components": [
                {
                    "name": "web",
                    "services": ["publish-on-web"],
                    "resources": {"requests": {"memory": request}, "limits": {"memory": limit}},
                }
            ],
        }

    async def test_request_below_limit_passes(self):
        data = self._make_data("256Mi", "512Mi")
        result = await ComponentServicesEnforcer().enforce(data, {})
        assert result is data

    async def test_request_equals_limit_passes(self):
        data = self._make_data("512Mi", "512Mi")
        result = await ComponentServicesEnforcer().enforce(data, {})
        assert result is data

    async def test_request_above_limit_raises_field_error(self):
        data = self._make_data("1Gi", "512Mi")
        with pytest.raises(FieldError, match="mag niet lager zijn") as exc_info:
            await ComponentServicesEnforcer().enforce(data, {})
        assert exc_info.value.field_path == "components[0]/resources/limits/memory"

    async def test_mixed_units_request_above_limit(self):
        data = self._make_data("768Mi", "0.5Gi")
        with pytest.raises(FieldError, match="mag niet lager zijn"):
            await ComponentServicesEnforcer().enforce(data, {})

    async def test_mixed_units_request_below_limit(self):
        data = self._make_data("256Mi", "1Gi")
        result = await ComponentServicesEnforcer().enforce(data, {})
        assert result is data

    async def test_missing_resources_passes(self):
        data = {
            "services": ["publish-on-web"],
            "components": [{"name": "web", "services": ["publish-on-web"]}],
        }
        result = await ComponentServicesEnforcer().enforce(data, {})
        assert result is data

    async def test_missing_memory_passes(self):
        data = {
            "services": ["publish-on-web"],
            "components": [
                {
                    "name": "web",
                    "services": ["publish-on-web"],
                    "resources": {"cpu": {"request": "50m"}},
                }
            ],
        }
        result = await ComponentServicesEnforcer().enforce(data, {})
        assert result is data

    async def test_error_targets_limit_field_path(self):
        data = self._make_data("1Gi", "256Mi")
        with pytest.raises(FieldError) as exc_info:
            await ComponentServicesEnforcer().enforce(data, {})
        assert exc_info.value.field_path == "components[0]/resources/limits/memory"
        assert "1Gi" in str(exc_info.value)
        assert "256Mi" in str(exc_info.value)

    async def test_tuner_values_accepted(self):
        data = self._make_data("184Mi", "384Mi")
        result = await ComponentServicesEnforcer().enforce(data, {})
        assert result is data


# ---------------------------------------------------------------------------
# Editable yaml_paths align with K8s-style resource format
# ---------------------------------------------------------------------------


class TestResourceEditableYamlPaths:
    """Verify editable yaml_paths produce K8s-style resources (requests/limits)."""

    def test_memory_request_path_is_k8s_style(self):
        from opi.forms.editables.fields.components import COMPONENT_RESOURCES_MEMORY_REQUEST_EDITABLE

        assert "resources/requests/memory" in COMPONENT_RESOURCES_MEMORY_REQUEST_EDITABLE.yaml_path

    def test_memory_limit_path_is_k8s_style(self):
        from opi.forms.editables.fields.components import COMPONENT_RESOURCES_MEMORY_LIMIT_EDITABLE

        assert "resources/limits/memory" in COMPONENT_RESOURCES_MEMORY_LIMIT_EDITABLE.yaml_path

    def test_cpu_request_path_is_k8s_style(self):
        from opi.forms.editables.fields.components import COMPONENT_RESOURCES_CPU_REQUEST_EDITABLE

        assert "resources/requests/cpu" in COMPONENT_RESOURCES_CPU_REQUEST_EDITABLE.yaml_path

    def test_cpu_limit_path_is_k8s_style(self):
        from opi.forms.editables.fields.components import COMPONENT_RESOURCES_CPU_LIMIT_EDITABLE

        assert "resources/limits/cpu" in COMPONENT_RESOURCES_CPU_LIMIT_EDITABLE.yaml_path

    def test_seed_data_matches_parse_resources_block(self):
        """Wizard seed data should be parseable by _parse_resources_block."""
        from opi.handlers.project_file_handler import _parse_resources_block

        seed_resources = {
            "requests": {"cpu": "50m", "memory": "256Mi"},
            "limits": {"cpu": "1", "memory": "512Mi"},
        }
        parsed = _parse_resources_block(seed_resources)
        assert parsed["requests_memory"] == "256Mi"
        assert parsed["limits_memory"] == "512Mi"
        assert parsed["requests_cpu"] == "50m"
        assert parsed["limits_cpu"] == "1"
