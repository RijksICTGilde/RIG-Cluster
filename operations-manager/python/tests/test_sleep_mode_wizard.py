"""Wizard/editables coverage for the sleep-mode service config section.

Pins that the project-level config section exists with the 9 config fields, that its
value providers return the expected options, and -- most importantly -- that the form
converters produce values the ``SleepModeConfig`` model accepts (the booleans become
real YAML booleans, the match textarea becomes a list).
"""

from __future__ import annotations

import pytest
from opi.forms.editables.converters import BooleanConverter, CommaSeparatedListConverter
from opi.forms.visualizers.providers import (
    SleepAfterDeployOptionsProvider,
    SleepAfterWakeOptionsProvider,
    WakeModeOptionsProvider,
    WakerComponentOptionsProvider,
    YesNoOptionsProvider,
)
from opi.services.catalog.base import ConfigLayer
from opi.services.catalog.sleep_mode.config_model import SleepModeConfig, validate_match_pattern
from opi.services.registry import get_service
from opi.services.services_enums import ServiceType


def _section():
    return get_service(ServiceType.SLEEP_MODE).config_form_section(ConfigLayer.PROJECT)


class TestSection:
    def test_section_identity_and_fields(self) -> None:
        section = _section()
        assert section.section_id == "sleep-mode-config"
        assert len(section.editables) == 9
        # Layout covers every config key, built from the enum-derived paths.
        assert section.layout == [
            "services/sleep-mode/config/enabled",
            "services/sleep-mode/config/wake-mode",
            "services/sleep-mode/config/match",
            "services/sleep-mode/config/sleep-after-deploy",
            "services/sleep-mode/config/sleep-after-wake",
            "services/sleep-mode/config/waker",
            "services/sleep-mode/config/waker-component",
            "services/sleep-mode/config/title",
            "services/sleep-mode/config/description",
        ]

    def test_visible_only_when_selected(self) -> None:
        section = _section()
        assert section.visible({"services": [{"name": "sleep-mode", "config": {}}]}) is True
        assert section.visible({"services": ["publish-on-web"]}) is False

    def test_api_fields_cover_the_model(self) -> None:
        fields = get_service(ServiceType.SLEEP_MODE).config_api_fields(ConfigLayer.PROJECT)
        assert set(fields) == {
            "enabled",
            "match",
            "sleep-after-deploy",
            "sleep-after-wake",
            "waker",
            "waker-component",
            "wake-mode",
            "title",
            "description",
        }


class TestBooleanFields:
    def test_enabled_and_waker_are_yes_no_selects(self) -> None:
        # Without a values_provider the Ja/Nee select renders empty and submits false, so
        # a wizard-selected sleep-mode would come out disabled (regression guard).
        from opi.services.catalog.sleep_mode.editables import SLEEP_ENABLED_EDITABLE, SLEEP_WAKER_EDITABLE

        assert SLEEP_ENABLED_EDITABLE.values_provider == "YesNoOptionsProvider"
        assert SLEEP_WAKER_EDITABLE.values_provider == "YesNoOptionsProvider"


class TestProviders:
    def test_option_text_is_jinja_safe(self) -> None:
        # The wizard re-renders step content as a template, so a quote or brace in an
        # option label/description breaks parsing (a single-quoted word did, once).
        providers = [
            WakeModeOptionsProvider(),
            YesNoOptionsProvider(),
            SleepAfterDeployOptionsProvider(),
            SleepAfterWakeOptionsProvider(),
            WakerComponentOptionsProvider(),
        ]
        for provider in providers:
            for option in provider.get_options():
                text = f"{option.get('label', '')} {option.get('description', '')}"
                assert not any(c in text for c in ("'", "{", "}", "%")), f"unsafe option text: {text!r}"

    def test_wake_mode_options(self) -> None:
        assert [o["value"] for o in WakeModeOptionsProvider().get_options()] == ["auto", "confirm", "manual"]

    def test_yes_no_options(self) -> None:
        assert [o["value"] for o in YesNoOptionsProvider().get_options()] == ["true", "false"]

    def test_duration_options_are_valid_durations(self) -> None:
        from opi.services.catalog.sleep_mode.config_model import parse_duration

        for provider in (SleepAfterDeployOptionsProvider(), SleepAfterWakeOptionsProvider()):
            for option in provider.get_options():
                parse_duration(option["value"])  # raises on an invalid preset

    def test_waker_component_lists_project_components(self) -> None:
        yaml_data = {"components": [{"name": "frontend"}, {"name": "backend"}]}
        options = WakerComponentOptionsProvider(yaml_data=yaml_data).get_options()
        assert options[0] == {"value": "", "label": "Automatisch"}
        assert [o["value"] for o in options[1:]] == ["frontend", "backend"]

    def test_waker_component_empty_without_components(self) -> None:
        options = WakerComponentOptionsProvider().get_options()
        assert options == [{"value": "", "label": "Automatisch"}]


class TestConverterRoundTrip:
    def test_boolean_converter_writes_real_bool(self) -> None:
        conv = BooleanConverter()
        assert conv.write("true") is True
        assert conv.write("false") is False
        assert conv.read(True) == "true"
        assert conv.read(False) == "false"

    def test_match_is_comma_separated(self) -> None:
        conv = CommaSeparatedListConverter()
        assert conv.write("pr-*, *-preview") == ["pr-*", "*-preview"]
        assert conv.read(["pr-*", "*-preview"]) == "pr-*, *-preview"

    def test_form_values_validate_against_the_model(self) -> None:
        # The exact shape the section's write-converters produce for a submitted form.
        config = {
            "enabled": BooleanConverter().write("true"),
            "wake-mode": "confirm",
            "match": CommaSeparatedListConverter().write("pr-*, *-preview"),
            "sleep-after-deploy": "48h",
            "sleep-after-wake": "1h",
            "waker": BooleanConverter().write("false"),
        }
        model = SleepModeConfig.model_validate(config)
        assert model.enabled is True
        assert model.waker is False
        assert model.match == ["pr-*", "*-preview"]
        assert model.wake_mode == "confirm"


class TestMatchValidation:
    @pytest.mark.parametrize("pattern", ["pr-*", "*-preview", "main", "*"])
    def test_simple_patterns_allowed(self, pattern: str) -> None:
        assert validate_match_pattern(pattern) == pattern
        assert SleepModeConfig.model_validate({"match": [pattern]}).match == [pattern]

    @pytest.mark.parametrize("pattern", ["pr-*-x", "pr?", "a[b]", "*mid*", "**"])
    def test_fancy_patterns_rejected(self, pattern: str) -> None:
        with pytest.raises(ValueError, match="Invalid match pattern"):
            validate_match_pattern(pattern)
        with pytest.raises(ValueError, match="Invalid match pattern"):
            SleepModeConfig.model_validate({"match": [pattern]})
