"""Tests for per-component metrics scraper configuration.

Covers:
- Virtualize support in _process_sequence_json (individual fields, not nested sequence)
- evaluate_show_when standalone function
- Fieldset conditional rendering with depends_on/show_when
- Round-trip: forward → back → forward navigation preserves values
"""

from __future__ import annotations

from opi.forms.editables.converters import IntegerConverter
from opi.forms.editables.editable import Editable, WidgetType, apply_virtualize
from opi.forms.editables.path import get_value, resolve_path
from opi.forms.editables.processor import EditableFormProcessor
from opi.forms.editables.validators import PathValidator
from opi.forms.layout import Fieldset
from opi.forms.visualizers.bridge import evaluate_show_when
from opi.forms.visualizers.visualizer import EditableVisualizer

# ---------------------------------------------------------------------------
# Fixtures: reusable editable/visualizer definitions
# ---------------------------------------------------------------------------


def _make_metrics_editables() -> tuple[EditableVisualizer, EditableVisualizer, EditableVisualizer, EditableVisualizer]:
    """Build a minimal component sequence with services checkbox + metrics fields."""
    services_vis = EditableVisualizer(
        editable=Editable(yaml_path="components[*]/services"),
        widget=WidgetType.CHECKBOX_GROUP,
        label="Services",
    )
    port_vis = EditableVisualizer(
        editable=Editable(
            yaml_path="components[*]/services{metrics-scraper}/port",
            converter=IntegerConverter(),
            required=True,
            depends_on="components[*]/services",
            show_when={"contains": "metrics-scraper"},
            virtualize=("services", "_services-config"),
        ),
        widget=WidgetType.TEXT,
        label="Metrics poort",
    )
    path_vis = EditableVisualizer(
        editable=Editable(
            yaml_path="components[*]/services{metrics-scraper}/path",
            validator=PathValidator(),
            required=True,
            depends_on="components[*]/services",
            show_when={"contains": "metrics-scraper"},
            virtualize=("services", "_services-config"),
        ),
        widget=WidgetType.TEXT,
        label="Metrics pad",
    )
    comp_seq = EditableVisualizer(
        editable=Editable(yaml_path="components"),
        widget=WidgetType.SEQUENCE,
        label="Components",
        children=[services_vis, port_vis, path_vis],
    )
    return comp_seq, services_vis, port_vis, path_vis


# ---------------------------------------------------------------------------
# apply_virtualize / path resolution
# ---------------------------------------------------------------------------


class TestVirtualizePaths:
    def test_apply_virtualize_replaces_segment(self):
        concrete = "components[0]/services{metrics-scraper}/port"
        result = apply_virtualize(concrete, ("services", "_services-config"))
        assert result == "components[0]/_services-config{metrics-scraper}/port"

    def test_apply_virtualize_none_returns_original(self):
        concrete = "components[0]/services{metrics-scraper}/port"
        result = apply_virtualize(concrete, None)
        assert result == concrete

    def test_get_value_with_virtual_path(self):
        submitted = {
            "components": [
                {
                    "services": ["publish-on-web", "metrics-scraper"],
                    "_services-config": [
                        {"metrics-scraper": {"port": "9090", "path": "/metrics"}},
                    ],
                }
            ]
        }
        read_path = "components[0]/_services-config{metrics-scraper}/port"
        assert get_value(submitted, read_path) == "9090"

    def test_get_value_with_virtual_path_missing_entry(self):
        submitted = {
            "components": [
                {
                    "services": ["publish-on-web"],
                    "_services-config": [],
                }
            ]
        }
        read_path = "components[0]/_services-config{metrics-scraper}/port"
        assert get_value(submitted, read_path) is None

    def test_resolve_path_and_virtualize(self):
        """Full chain: wildcard path → resolve → virtualize."""
        yaml_path = "components[*]/services{metrics-scraper}/port"
        concrete = resolve_path(yaml_path, 0)
        assert concrete == "components[0]/services{metrics-scraper}/port"
        virtual = apply_virtualize(concrete, ("services", "_services-config"))
        assert virtual == "components[0]/_services-config{metrics-scraper}/port"


# ---------------------------------------------------------------------------
# evaluate_show_when (standalone)
# ---------------------------------------------------------------------------


class TestEvaluateShowWhen:
    def test_none_show_when_truthy(self):
        assert evaluate_show_when("something", None) is True

    def test_none_show_when_falsy(self):
        assert evaluate_show_when(None, None) is False
        assert evaluate_show_when("", None) is False
        assert evaluate_show_when([], None) is False

    def test_contains_match(self):
        assert (
            evaluate_show_when(
                ["publish-on-web", "metrics-scraper"],
                {"contains": "metrics-scraper"},
            )
            is True
        )

    def test_contains_no_match(self):
        assert (
            evaluate_show_when(
                ["publish-on-web"],
                {"contains": "metrics-scraper"},
            )
            is False
        )

    def test_contains_mixed_list(self):
        """Services list with dict entries (session state format)."""
        mixed = ["publish-on-web", {"metrics-scraper": {"port": 8080}}]
        assert evaluate_show_when(mixed, {"contains": "metrics-scraper"}) is True

    def test_contains_not_a_list(self):
        assert evaluate_show_when("metrics-scraper", {"contains": "metrics-scraper"}) is False

    def test_contains_none_value(self):
        assert evaluate_show_when(None, {"contains": "metrics-scraper"}) is False

    def test_contains_any_match(self):
        assert (
            evaluate_show_when(
                ["publish-on-web", "keycloak"],
                {"contains_any": ["keycloak", "metrics-scraper"]},
            )
            is True
        )

    def test_contains_any_no_match(self):
        assert (
            evaluate_show_when(
                ["publish-on-web"],
                {"contains_any": ["keycloak", "metrics-scraper"]},
            )
            is False
        )

    def test_exact_value_match(self):
        assert evaluate_show_when("frontend", {"type": "frontend"}) is True

    def test_exact_value_no_match(self):
        assert evaluate_show_when("backend", {"type": "frontend"}) is False

    def test_value_in_list(self):
        assert evaluate_show_when("single", {"type": ["single", "frontend"]}) is True

    def test_value_not_in_list(self):
        assert evaluate_show_when("backend", {"type": ["single", "frontend"]}) is False


# ---------------------------------------------------------------------------
# Processor: virtualized individual fields in sequence
# ---------------------------------------------------------------------------


class TestProcessorVirtualizeFields:
    async def test_reads_from_virtual_path_writes_to_real_path(self):
        """Metrics port/path are read from _services-config but written to services{...}."""
        processor = EditableFormProcessor()
        comp_seq, *_ = _make_metrics_editables()

        submitted = {
            "components": [
                {
                    "name": "web",
                    "services": ["publish-on-web", "metrics-scraper"],
                    "_services-config": [
                        {"metrics-scraper": {"port": "9090", "path": "/health"}},
                    ],
                }
            ]
        }
        yaml_data = {
            "components": [
                {
                    "name": "web",
                    "services": ["publish-on-web", "metrics-scraper"],
                }
            ]
        }

        result, errors = await processor.process_json_submission(submitted, [comp_seq], yaml_data)

        assert errors == {}
        # Value written to real path via smart_set_value
        comp_services = result["components"][0]["services"]
        has_metrics = any(isinstance(s, dict) and "metrics-scraper" in s for s in comp_services)
        assert has_metrics, f"metrics-scraper config should be in services, got: {comp_services}"

    async def test_validation_passes_with_filled_fields(self):
        processor = EditableFormProcessor()
        comp_seq, *_ = _make_metrics_editables()

        submitted = {
            "components": [
                {
                    "name": "web",
                    "services": ["publish-on-web", "metrics-scraper"],
                    "_services-config": [
                        {"metrics-scraper": {"port": "8080", "path": "/metrics"}},
                    ],
                }
            ]
        }
        yaml_data = {"components": [{"name": "web", "services": ["publish-on-web", "metrics-scraper"]}]}

        _, errors = await processor.process_json_submission(submitted, [comp_seq], yaml_data)
        assert errors == {}

    async def test_validation_fails_when_required_fields_empty(self):
        processor = EditableFormProcessor()
        comp_seq, *_ = _make_metrics_editables()

        submitted = {
            "components": [
                {
                    "name": "web",
                    "services": ["publish-on-web", "metrics-scraper"],
                    "_services-config": [
                        {"metrics-scraper": {"port": "", "path": ""}},
                    ],
                }
            ]
        }
        yaml_data = {"components": [{"name": "web", "services": ["publish-on-web", "metrics-scraper"]}]}

        _, errors = await processor.process_json_submission(submitted, [comp_seq], yaml_data)
        assert "components[0]/services{metrics-scraper}/port" in errors
        assert "components[0]/services{metrics-scraper}/path" in errors

    async def test_metrics_fields_skipped_when_service_not_selected(self):
        """When metrics-scraper is not in services, fields are skipped (no errors)."""
        processor = EditableFormProcessor()
        comp_seq, *_ = _make_metrics_editables()

        submitted = {
            "components": [
                {
                    "name": "web",
                    "services": ["publish-on-web"],
                }
            ]
        }
        yaml_data = {"components": [{"name": "web", "services": ["publish-on-web"]}]}

        _, errors = await processor.process_json_submission(submitted, [comp_seq], yaml_data)
        assert errors == {}

    async def test_round_trip_preserves_virtual_data(self):
        """Simulate forward → back → forward: values must survive in session state.

        After the first submission, the result becomes session state. When
        re-used as submitted data (backward navigation), the processor must
        still find the metrics values - either from _services-config or from
        the real services path.
        """
        processor = EditableFormProcessor()
        comp_seq, *_ = _make_metrics_editables()

        # First submission (fresh form data with _services-config)
        submitted_1 = {
            "components": [
                {
                    "name": "web",
                    "services": ["publish-on-web", "metrics-scraper"],
                    "_services-config": [
                        {"metrics-scraper": {"port": "8080", "path": "/metrics"}},
                    ],
                }
            ]
        }
        yaml_data = {"components": [{"name": "web", "services": ["publish-on-web", "metrics-scraper"]}]}

        result_1, errors_1 = await processor.process_json_submission(submitted_1, [comp_seq], yaml_data)
        assert errors_1 == {}, f"First submission should pass, got: {errors_1}"

        # Second submission: result_1 is used as both yaml_data AND submitted
        # (simulates backward navigation where session state = submitted data)
        result_2, errors_2 = await processor.process_json_submission(result_1, [comp_seq], result_1)
        assert errors_2 == {}, f"Round-trip should preserve values without errors, got: {errors_2}"

    async def test_path_validator_applied_to_metrics_path(self):
        processor = EditableFormProcessor()
        comp_seq, *_ = _make_metrics_editables()

        submitted = {
            "components": [
                {
                    "name": "web",
                    "services": ["publish-on-web", "metrics-scraper"],
                    "_services-config": [
                        {"metrics-scraper": {"port": "8080", "path": "no-leading-slash"}},
                    ],
                }
            ]
        }
        yaml_data = {"components": [{"name": "web", "services": ["publish-on-web", "metrics-scraper"]}]}

        _, errors = await processor.process_json_submission(submitted, [comp_seq], yaml_data)
        assert "components[0]/services{metrics-scraper}/path" in errors


# ---------------------------------------------------------------------------
# Fieldset depends_on / show_when
# ---------------------------------------------------------------------------


class TestFieldsetConditional:
    def test_fieldset_has_depends_on_attribute(self):
        fs = Fieldset(
            legend="Test",
            depends_on="services",
            show_when={"contains": "metrics-scraper"},
            children=["port", "path"],
        )
        assert fs.depends_on == "services"
        assert fs.show_when == {"contains": "metrics-scraper"}

    def test_fieldset_defaults_to_no_condition(self):
        fs = Fieldset(legend="Test", children=["name"])
        assert fs.depends_on is None
        assert fs.show_when is None

    def test_fieldset_dataclass_replace_preserves_depends_on(self):
        import dataclasses

        fs = Fieldset(
            legend="Test",
            depends_on="services",
            show_when={"contains": "metrics-scraper"},
            children=["port"],
        )
        replaced = dataclasses.replace(fs, children=["port", "path"])
        assert replaced.depends_on == "services"
        assert replaced.show_when == {"contains": "metrics-scraper"}
