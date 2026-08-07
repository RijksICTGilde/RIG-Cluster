"""What a summary screen may show.

Three things are pinned here. A field can declare that its value does not belong
in a summary (``summarizer``), and that declaration has to hold everywhere a
summary is built -- including one and two levels into a sequence, which is
exactly where it used to fall through. Whatever does get shown is escaped,
because the review page renders this HTML with ``| safe``. And a section that
summarizes itself returns data, not markup, so the escaping is a property of the
path and not a rule the author of a summary has to remember.
"""

from __future__ import annotations

import ast
import inspect
from typing import Any

import pytest
from opi.forms.editables.editable import Editable, WidgetType
from opi.forms.editables.summarizers import HiddenSummary, MaskedSummary
from opi.forms.visualizers import wizard_sections
from opi.forms.visualizers.sections import FormSection
from opi.forms.visualizers.visualizer import EditableVisualizer
from opi.web import router_wizard
from opi.web.router_wizard import _build_section_fields, _build_section_summary

XSS = "<img src=x onerror=alert(1)>"


def _field(path: str, label: str, **editable_kwargs) -> EditableVisualizer:
    return EditableVisualizer(
        editable=Editable(yaml_path=path, **editable_kwargs),
        widget=WidgetType.TEXT,
        label=label,
    )


def _section(*editables: EditableVisualizer) -> FormSection:
    return FormSection(section_id="test", title="Test", editables=list(editables))


class TestSummarizerHidesValues:
    def test_hidden_field_is_left_out_entirely(self):
        section = _section(
            _field("name", "Naam"),
            _field("token", "Token", summarizer=HiddenSummary()),
        )
        html = _build_section_summary(section, {"name": "demo", "token": "s3cr3t"})

        assert "demo" in html
        assert "s3cr3t" not in html
        assert "Token" not in html

    def test_masked_field_says_it_is_set_without_saying_what(self):
        section = _section(_field("token", "Token", summarizer=MaskedSummary()))
        html = _build_section_summary(section, {"token": "s3cr3t"})

        assert "Ingesteld" in html
        assert "s3cr3t" not in html

    def test_masked_field_is_omitted_when_empty(self):
        section = _section(_field("token", "Token", summarizer=MaskedSummary()))
        assert "Token" not in _build_section_summary(section, {"token": ""})

    def test_summarizer_wins_from_the_converter(self):
        """Two hooks could answer; the summary one decides on a summary screen."""

        class ShoutingConverter:
            def read(self, value, context_data=None):
                return value

            def write(self, value, context_data=None):
                return value

            def view(self, value, context_data=None):
                return f"VIEW:{value}"

        section = _section(
            _field("token", "Token", converter=ShoutingConverter(), summarizer=HiddenSummary()),
        )
        html = _build_section_summary(section, {"token": "s3cr3t"})

        assert "s3cr3t" not in html
        assert "VIEW" not in html

    def test_hidden_field_stays_hidden_inside_a_sequence(self):
        section = _section(
            EditableVisualizer(
                editable=Editable(yaml_path="components"),
                widget=WidgetType.SEQUENCE,
                label="Componenten",
                children=[
                    _field("components[*]/name", "Naam"),
                    _field("components[*]/token", "Token", summarizer=HiddenSummary()),
                ],
            )
        )
        html = _build_section_summary(section, {"components": [{"name": "web", "token": "s3cr3t"}]})

        assert "web" in html
        assert "s3cr3t" not in html

    def test_hidden_field_stays_hidden_one_level_deeper(self):
        """The nested-sequence branch used to format its own values and skipped this."""
        section = _section(
            EditableVisualizer(
                editable=Editable(yaml_path="components"),
                widget=WidgetType.SEQUENCE,
                label="Componenten",
                children=[
                    _field("components[*]/name", "Naam"),
                    EditableVisualizer(
                        editable=Editable(yaml_path="components[*]/volumes"),
                        widget=WidgetType.SEQUENCE,
                        label="Volumes",
                        children=[
                            _field("components[*]/volumes[*]/name", "Naam"),
                            _field("components[*]/volumes[*]/token", "Token", summarizer=HiddenSummary()),
                        ],
                    ),
                ],
            )
        )
        data = {"components": [{"name": "web", "volumes": [{"name": "data", "token": "s3cr3t"}]}]}
        html = _build_section_summary(section, data)

        assert "data" in html
        assert "s3cr3t" not in html

    def test_a_nested_item_with_nothing_left_to_show_is_not_dumped_raw(self):
        """Every field hidden must not fall back to printing the whole item."""
        section = _section(
            EditableVisualizer(
                editable=Editable(yaml_path="components"),
                widget=WidgetType.SEQUENCE,
                label="Componenten",
                children=[
                    _field("components[*]/name", "Naam"),
                    EditableVisualizer(
                        editable=Editable(yaml_path="components[*]/volumes"),
                        widget=WidgetType.SEQUENCE,
                        label="Volumes",
                        children=[
                            _field("components[*]/volumes[*]/token", "Token", summarizer=HiddenSummary()),
                        ],
                    ),
                ],
            )
        )
        data = {"components": [{"name": "web", "volumes": [{"token": "s3cr3t"}]}]}
        html = _build_section_summary(section, data)

        assert "s3cr3t" not in html
        assert "web" in html  # the rest of the item is still summarized

    def test_the_modal_summary_honours_it_too(self):
        """Two builders, one rule -- the edit modal uses the structured one."""
        section = _section(
            _field("name", "Naam"),
            _field("token", "Token", summarizer=HiddenSummary()),
        )
        fields = _build_section_fields(section, {"name": "demo", "token": "s3cr3t"})

        assert [f["label"] for f in fields] == ["Naam"]


class TestSummaryEscapesWhatItShows:
    """wizard_review.html.j2 renders this HTML with ``| safe``, so it escapes here."""

    def test_field_value_is_escaped(self):
        section = _section(_field("description", "Omschrijving"))
        html = _build_section_summary(section, {"description": XSS})

        assert XSS not in html
        assert "&lt;img" in html

    def test_sequence_item_label_is_escaped(self):
        section = _section(
            EditableVisualizer(
                editable=Editable(yaml_path="components"),
                widget=WidgetType.SEQUENCE,
                label="Componenten",
                children=[_field("components[*]/name", "Naam")],
            )
        )
        html = _build_section_summary(section, {"components": [{"name": XSS}]})

        assert XSS not in html
        assert "&lt;img" in html

    def test_plain_sequence_item_is_escaped(self):
        section = _section(
            EditableVisualizer(
                editable=Editable(yaml_path="services"),
                widget=WidgetType.SEQUENCE,
                label="Diensten",
                children=[],
            )
        )
        html = _build_section_summary(section, {"services": [XSS]})

        assert XSS not in html
        assert "&lt;img" in html  # shown, escaped -- not silently dropped

    def test_nested_sequence_value_is_escaped(self):
        section = _section(
            EditableVisualizer(
                editable=Editable(yaml_path="components"),
                widget=WidgetType.SEQUENCE,
                label="Componenten",
                children=[
                    _field("components[*]/name", "Naam"),
                    EditableVisualizer(
                        editable=Editable(yaml_path="components[*]/volumes"),
                        widget=WidgetType.SEQUENCE,
                        label="Volumes",
                        children=[_field("components[*]/volumes[*]/name", "Naam")],
                    ),
                ],
            )
        )
        data = {"components": [{"name": "web", "volumes": [{"name": XSS}]}]}
        html = _build_section_summary(section, data)

        assert XSS not in html
        assert "&lt;img" in html


class TestServiceCardsGoThroughTheSameGate:
    """The service-cards branch of the modal builder had its own path around
    ``_format_value``; a summarizer therefore did not hold there."""

    @staticmethod
    def _cards(**editable_kwargs) -> FormSection:
        return FormSection(
            section_id="test",
            title="Test",
            editables=[
                EditableVisualizer(
                    editable=Editable(yaml_path="services", **editable_kwargs),
                    widget=WidgetType.SERVICE_CARDS,
                    label="Services",
                )
            ],
        )

    def test_without_a_summarizer_the_cards_are_still_listed(self):
        fields = _build_section_fields(self._cards(), {"services": ["keycloak", "minio"]})

        assert fields == [{"label": "Services", "value": ["keycloak", "minio"], "is_list": True}]

    def test_a_hidden_summarizer_holds_here_too(self):
        fields = _build_section_fields(self._cards(summarizer=HiddenSummary()), {"services": ["keycloak"]})

        assert fields == []

    def test_a_masked_summarizer_replaces_the_list(self):
        fields = _build_section_fields(self._cards(summarizer=MaskedSummary()), {"services": ["keycloak"]})

        assert fields == [{"label": "Services", "value": "Ingesteld", "is_list": False}]


def _real_summary_fns() -> list[tuple[str, Any]]:
    """Every ``summary_fn`` a real section declares, by section id."""
    sections = [
        wizard_sections.BACKUP_SELECT_SECTION,
        wizard_sections.RESTORE_SELECT_SECTION,
        wizard_sections.RESTORE_TARGET_SECTION,
        wizard_sections.build_deployment_add_info_section(0),
    ]
    found = [(s.section_id, s.summary_fn) for s in sections if s.summary_fn]
    assert len(found) == len(sections), "a section lost its summary_fn -- check this list"
    return found


HOSTILE_DATA: dict[str, Any] = {
    "deployment_name": XSS,
    "resource_types": [XSS],
    "backup_run_id": XSS,
    "restore_mode": "existing",
    "target_deployment": XSS,
    "deployments": [{"name": XSS}],
}


class TestASummaryFnCannotBuildMarkup:
    """``summary_fn`` returns (label, value) pairs; both builders put the tags
    around them and escape. A summary that ships HTML has nothing to escape it."""

    @pytest.mark.parametrize(("section_id", "summary_fn"), _real_summary_fns())
    def test_it_returns_plain_text_pairs(self, section_id: str, summary_fn):
        items = summary_fn(HOSTILE_DATA)

        assert isinstance(items, list)
        for label, value in items:
            assert isinstance(label, str)
            assert isinstance(value, str)
            assert "<" not in label, f"{section_id} builds markup in a label"

    @pytest.mark.parametrize(("section_id", "summary_fn"), _real_summary_fns())
    def test_the_wizard_review_escapes_it(self, section_id: str, summary_fn):
        section = FormSection(section_id=section_id, title="Test", summary_fn=summary_fn)
        html = _build_section_summary(section, HOSTILE_DATA)

        assert XSS not in html
        assert "&lt;img" in html  # shown, escaped -- not silently dropped

    @pytest.mark.parametrize(("section_id", "summary_fn"), _real_summary_fns())
    def test_the_modal_review_gets_data_not_html(self, section_id: str, summary_fn):
        section = FormSection(section_id=section_id, title="Test", summary_fn=summary_fn)
        fields = _build_section_fields(section, HOSTILE_DATA)

        # "html" bypasses the template's escaping; a section's own summary never
        # takes that route.
        assert all("html" not in field for field in fields)
        assert all(field["is_list"] is False for field in fields)


#: Interpolations that are HTML the module already built (and already escaped the
#: values inside), so escaping them again would print tags.
ALLOWED_RAW = {
    "''.join(parts)",
    "''.join(item_parts)",
    "len(items)",
}

#: The functions in router_wizard.py that build summary HTML themselves.
SUMMARY_BUILDERS = (
    "_summary_pairs_html",
    "_build_section_summary",
    "_build_sequence_summary",
)


class TestTheSummaryBuildersEscapeEveryInterpolation:
    """A source guard on the builders that do produce HTML.

    They are f-strings by nature, so a new one is one keystroke away from
    printing a raw value. Every hole in those f-strings must either go through
    ``_summary_text`` or be a fragment this module built itself.
    """

    def test_no_unescaped_hole_in_a_markup_f_string(self):
        tree = ast.parse(inspect.getsource(router_wizard))
        offenders: list[str] = []

        for func in ast.walk(tree):
            if not isinstance(func, ast.FunctionDef) or func.name not in SUMMARY_BUILDERS:
                continue
            for node in ast.walk(func):
                if not isinstance(node, ast.JoinedStr):
                    continue
                literal = "".join(p.value for p in node.values if isinstance(p, ast.Constant))
                if "<" not in literal:
                    continue
                for part in node.values:
                    if not isinstance(part, ast.FormattedValue):
                        continue
                    expr = ast.unparse(part.value)
                    escaped = isinstance(part.value, ast.Call) and getattr(part.value.func, "id", "") == "_summary_text"
                    if not escaped and expr not in ALLOWED_RAW:
                        offenders.append(f"{func.name}: {{{expr}}}")

        assert not offenders, (
            "put these through _summary_text (or allowlist them if they are built HTML): " + ", ".join(offenders)
        )

    def test_the_guard_would_catch_a_raw_hole(self):
        """The guard above is only worth having if it fails on the real mistake."""
        tree = ast.parse('def _build_section_summary(x):\n    return f"<p>{x}</p>"\n')
        func = tree.body[0]
        assert isinstance(func, ast.FunctionDef)
        holes = [
            ast.unparse(p.value)
            for node in ast.walk(func)
            if isinstance(node, ast.JoinedStr)
            for p in node.values
            if isinstance(p, ast.FormattedValue)
        ]
        assert holes == ["x"]
        assert "x" not in ALLOWED_RAW


class TestSummaryFnImplementationsHoldNoMarkup:
    """A source guard on wizard_sections.py, where the summary_fn's live.

    Nothing there builds HTML any more; a new f-string with a tag in it is the
    mistake this catches.
    """

    def test_no_summary_fn_contains_a_tag(self):
        tree = ast.parse(inspect.getsource(wizard_sections))
        offenders: list[str] = []

        for func in ast.walk(tree):
            if not isinstance(func, ast.FunctionDef) or not func.name.endswith("_summary"):
                continue
            for node in ast.walk(func):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and "<" in node.value:
                    offenders.append(f"{func.name}: {node.value!r}")
                if isinstance(node, ast.JoinedStr):
                    literal = "".join(p.value for p in node.values if isinstance(p, ast.Constant))
                    if "<" in literal:
                        offenders.append(f"{func.name}: {literal!r}")

        assert not offenders, "a summary_fn returns data, not markup: " + ", ".join(offenders)
