"""Tests for custom domain support: defers_to pattern, conditions, converters, validators."""

from __future__ import annotations

import pytest
from opi.forms.editables.conditions import SentinelValueCondition
from opi.forms.editables.converters import CustomDomainSelectConverter
from opi.forms.editables.editable import Editable, WidgetType
from opi.forms.editables.processor import EditableFormProcessor
from opi.forms.editables.validators import BaseDomainValidator, CustomDomainValidator
from opi.forms.visualizers.visualizer import EditableVisualizer

# ---------------------------------------------------------------------------
# SentinelValueCondition
# ---------------------------------------------------------------------------


class TestSentinelValueCondition:
    def test_matches_default_sentinel(self):
        cond = SentinelValueCondition()
        assert cond.check("__custom__") is True

    def test_no_match_regular_value(self):
        cond = SentinelValueCondition()
        assert cond.check("rijksapp.nl") is False

    def test_no_match_empty(self):
        cond = SentinelValueCondition()
        assert cond.check("") is False
        assert cond.check(None) is False

    def test_custom_sentinel(self):
        cond = SentinelValueCondition(sentinel="__other__")
        assert cond.check("__other__") is True
        assert cond.check("__custom__") is False


# ---------------------------------------------------------------------------
# CustomDomainSelectConverter
# ---------------------------------------------------------------------------


class TestCustomDomainSelectConverter:
    def setup_method(self):
        self.converter = CustomDomainSelectConverter()

    def test_view_supported_domain_passes_through(self):
        # Supported domains should pass through unchanged
        from opi.connectors.subdomain import get_supported_base_domains

        supported = get_supported_base_domains()
        if supported:
            domain = next(iter(supported))
            assert self.converter.view(domain) == domain

    def test_view_custom_domain_returns_sentinel(self):
        assert self.converter.view("mijnorganisatie.nl") == "__custom__"

    def test_view_empty_passes_through(self):
        assert self.converter.view("") == ""
        assert self.converter.view(None) is None

    def test_read_delegates_to_view(self):
        assert self.converter.read("mijnorganisatie.nl") == "__custom__"

    def test_write_passes_through(self):
        assert self.converter.write("__custom__") == "__custom__"
        assert self.converter.write("mijnapp.nl") == "mijnapp.nl"


# ---------------------------------------------------------------------------
# CustomDomainValidator
# ---------------------------------------------------------------------------


class TestCustomDomainValidator:
    def setup_method(self):
        self.validator = CustomDomainValidator()

    def test_valid_domain(self):
        assert self.validator.validate("voorbeeld.nl") == []
        assert self.validator.validate("mijn-app.example.com") == []
        assert self.validator.validate("sub.domain.co.uk") == []

    def test_invalid_domain(self):
        assert len(self.validator.validate("niet geldig")) > 0
        assert len(self.validator.validate("-start-hyphen.nl")) > 0
        assert len(self.validator.validate("enkel")) > 0  # no TLD

    def test_empty_is_valid(self):
        assert self.validator.validate("") == []
        assert self.validator.validate(None) == []


# ---------------------------------------------------------------------------
# BaseDomainValidator with __custom__ sentinel
# ---------------------------------------------------------------------------


class TestBaseDomainValidatorSentinel:
    def setup_method(self):
        self.validator = BaseDomainValidator()

    def test_sentinel_value_accepted(self):
        assert self.validator.validate("__custom__") == []


# ---------------------------------------------------------------------------
# Processor: defers_to and transient handling
# ---------------------------------------------------------------------------


def _make_editable_pair() -> tuple[Editable, Editable, list[EditableVisualizer]]:
    """Create a parent editable with defers_to and a transient child."""
    parent_ed = Editable(
        yaml_path="items[0]/domain",
        converter=CustomDomainSelectConverter(),
        defers_to="items[0]/domain:custom",
        defer_when=SentinelValueCondition(),
    )
    child_ed = Editable(
        yaml_path="items[0]/domain:custom",
        transient=True,
    )
    parent_vis = EditableVisualizer(editable=parent_ed, widget=WidgetType.TEXT, label="Domain")
    child_vis = EditableVisualizer(editable=child_ed, widget=WidgetType.TEXT, label="Custom")
    return parent_ed, child_ed, [parent_vis, child_vis]


class TestProcessorDeferral:
    def test_defers_when_sentinel(self):
        """When parent has sentinel value, transient value replaces it."""
        _parent_ed, _child_ed, editables = _make_editable_pair()
        processor = EditableFormProcessor()
        data = {"items": [{"domain": "__custom__", "domain:custom": "mijnapp.nl"}]}

        result = processor.apply_to_yaml(
            {"items[0]/domain": "__custom__", "items[0]/domain:custom": "mijnapp.nl"},
            editables,
            data,
        )

        # Deferral should copy custom value to domain
        assert result["items"][0]["domain"] == "mijnapp.nl"
        # Transient should be stripped
        assert "domain:custom" not in result["items"][0]

    def test_no_defer_when_regular_value(self):
        """When parent has a regular value, no deferral happens."""
        _parent_ed, _child_ed, editables = _make_editable_pair()
        processor = EditableFormProcessor()
        data = {"items": [{"domain": "rijksapp.nl"}]}

        result = processor.apply_to_yaml(
            {"items[0]/domain": "rijksapp.nl"},
            editables,
            data,
        )

        assert result["items"][0]["domain"] == "rijksapp.nl"
        assert "domain:custom" not in result["items"][0]

    def test_transient_always_stripped(self):
        """Transient fields are stripped even when deferral doesn't trigger."""
        _parent_ed, _child_ed, editables = _make_editable_pair()
        processor = EditableFormProcessor()
        data = {"items": [{"domain": "rijksapp.nl", "domain:custom": "leftover"}]}

        result = processor.apply_to_yaml(
            {"items[0]/domain": "rijksapp.nl", "items[0]/domain:custom": "leftover"},
            editables,
            data,
        )

        assert "domain:custom" not in result["items"][0]


class TestProcessorPopulateDeferred:
    def test_populates_transient_for_custom_domain(self):
        """When stored value triggers defer condition (via converter), populate transient."""
        _parent_ed, _child_ed, editables = _make_editable_pair()
        processor = EditableFormProcessor()
        # Stored data has a custom domain (not in supported list)
        data = {"items": [{"domain": "mijnapp.nl"}]}

        processor.populate_deferred_fields(data, editables)

        # Transient field should now have the stored value
        assert data["items"][0]["domain:custom"] == "mijnapp.nl"

    def test_no_populate_for_supported_domain(self):
        """When stored value is a supported domain, no transient population."""
        _parent_ed, _child_ed, editables = _make_editable_pair()
        processor = EditableFormProcessor()

        from opi.connectors.subdomain import get_supported_base_domains

        supported = get_supported_base_domains()
        if not supported:
            pytest.skip("No supported domains configured")

        domain = next(iter(supported))
        data = {"items": [{"domain": domain}]}

        processor.populate_deferred_fields(data, editables)

        assert "domain:custom" not in data["items"][0]


# ---------------------------------------------------------------------------
# Sequence-level defers_to (wildcard paths)
# ---------------------------------------------------------------------------


def _make_sequence_pair() -> list[EditableVisualizer]:
    """Create a sequence with parent + transient child using [*] paths."""
    parent_ed = Editable(
        yaml_path="deployments[*]/base-domain",
        converter=CustomDomainSelectConverter(),
        defers_to="deployments[*]/base-domain:custom",
        defer_when=SentinelValueCondition(),
    )
    child_ed = Editable(
        yaml_path="deployments[*]/base-domain:custom",
        transient=True,
    )
    seq_ed = Editable(yaml_path="deployments", children=[parent_ed, child_ed])

    parent_vis = EditableVisualizer(editable=parent_ed, widget=WidgetType.TEXT, label="Domain")
    child_vis = EditableVisualizer(editable=child_ed, widget=WidgetType.TEXT, label="Custom")
    seq_vis = EditableVisualizer(
        editable=seq_ed,
        widget=WidgetType.SEQUENCE,
        label="Deployments",
        children=[parent_vis, child_vis],
    )
    return [seq_vis]


class TestSequenceDeferral:
    def test_defers_within_sequence_items(self):
        """Deferral works per-item in a sequence."""
        editables = _make_sequence_pair()
        processor = EditableFormProcessor()
        data = {
            "deployments": [
                {"base-domain": "__custom__", "base-domain:custom": "custom1.nl"},
                {"base-domain": "rijksapp.nl"},
            ]
        }
        parsed = {
            "deployments[0]/base-domain": "__custom__",
            "deployments[0]/base-domain:custom": "custom1.nl",
            "deployments[1]/base-domain": "rijksapp.nl",
        }

        result = processor.apply_to_yaml(parsed, editables, data)

        assert result["deployments"][0]["base-domain"] == "custom1.nl"
        assert "base-domain:custom" not in result["deployments"][0]
        assert result["deployments"][1]["base-domain"] == "rijksapp.nl"
        assert "base-domain:custom" not in result["deployments"][1]

    def test_populate_deferred_in_sequence(self):
        """Populate works per-item in a sequence."""
        editables = _make_sequence_pair()
        processor = EditableFormProcessor()
        data = {
            "deployments": [
                {"base-domain": "custom1.nl"},
                {"base-domain": "rijksapp.nl"},
            ]
        }

        processor.populate_deferred_fields(data, editables)

        # First deployment has custom domain — should be populated
        assert data["deployments"][0].get("base-domain:custom") == "custom1.nl"
        # Second has supported domain — no population
        assert "base-domain:custom" not in data["deployments"][1]


# ---------------------------------------------------------------------------
# Editable field definitions
# ---------------------------------------------------------------------------


class TestDomainEditableDefinitions:
    def test_create_wizard_base_domain_has_defers_to(self):
        from opi.forms.editables.fields.domains import DOMAIN_BASE_DOMAIN_EDITABLE

        assert DOMAIN_BASE_DOMAIN_EDITABLE.defers_to == "deployments[0]/base-domain:custom"
        assert DOMAIN_BASE_DOMAIN_EDITABLE.defer_when is not None

    def test_create_wizard_custom_field_is_transient(self):
        from opi.forms.editables.fields.domains import DOMAIN_CUSTOM_BASE_DOMAIN_EDITABLE

        assert DOMAIN_CUSTOM_BASE_DOMAIN_EDITABLE.transient is True
        assert DOMAIN_CUSTOM_BASE_DOMAIN_EDITABLE.yaml_path == "deployments[0]/base-domain:custom"

    def test_edit_wizard_base_domain_has_defers_to(self):
        from opi.forms.editables.fields.deployments import DEPLOYMENT_BASE_DOMAIN_EDITABLE

        assert DEPLOYMENT_BASE_DOMAIN_EDITABLE.defers_to == "deployments[*]/base-domain:custom"
        assert DEPLOYMENT_BASE_DOMAIN_EDITABLE.defer_when is not None

    def test_edit_wizard_custom_field_is_transient(self):
        from opi.forms.editables.fields.deployments import DEPLOYMENT_CUSTOM_BASE_DOMAIN_EDITABLE

        assert DEPLOYMENT_CUSTOM_BASE_DOMAIN_EDITABLE.transient is True
        assert DEPLOYMENT_CUSTOM_BASE_DOMAIN_EDITABLE.yaml_path == "deployments[*]/base-domain:custom"

    def test_custom_field_in_deployments_sequence_children(self):
        from opi.forms.editables.fields.deployments import (
            DEPLOYMENT_CUSTOM_BASE_DOMAIN_EDITABLE,
            DEPLOYMENTS_SEQUENCE_EDITABLE,
        )

        assert DEPLOYMENT_CUSTOM_BASE_DOMAIN_EDITABLE in DEPLOYMENTS_SEQUENCE_EDITABLE.children


# ---------------------------------------------------------------------------
# Provider __custom__ options
# ---------------------------------------------------------------------------


class TestProviderCustomOption:
    def test_cluster_base_domain_provider_has_custom_option(self):
        from opi.forms.visualizers.providers import ClusterBaseDomainOptionsProvider

        provider = ClusterBaseDomainOptionsProvider()
        options = provider.get_options()
        values = [o["value"] for o in options]
        assert "__custom__" in values

    def test_base_domain_provider_has_custom_option(self):
        from opi.forms.visualizers.providers import BaseDomainOptionsProvider

        provider = BaseDomainOptionsProvider()
        options = provider.get_options()
        values = [o["value"] for o in options]
        assert "__custom__" in values

    def test_domain_format_provider_supports_dots_for_custom(self):
        from opi.forms.visualizers.providers import DomainFormatOptionsProvider

        provider = DomainFormatOptionsProvider(base_domain="__custom__")
        options = provider.get_options()
        # Custom domains get dot-separated options (more than just dash variants)
        assert len(options) > 4  # 4 dash + 4 dot options


# ---------------------------------------------------------------------------
# validate_base_domain accepts custom domains
# ---------------------------------------------------------------------------


class TestValidateBaseDomainCustom:
    def test_accepts_valid_custom_domain(self):
        from opi.connectors.subdomain import validate_base_domain

        is_valid, error = validate_base_domain("mijnapp.nl")
        assert is_valid is True
        assert error is None

    def test_rejects_invalid_domain(self):
        from opi.connectors.subdomain import validate_base_domain

        is_valid, _error = validate_base_domain("not a domain")
        assert is_valid is False

    def test_rejects_single_label(self):
        from opi.connectors.subdomain import validate_base_domain

        is_valid, _error = validate_base_domain("nodots")
        assert is_valid is False
