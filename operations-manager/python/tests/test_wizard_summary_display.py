"""What a summary screen may show.

Two things are pinned here. A field can declare that its value does not belong in
a summary (``summarizer``), and that declaration has to hold everywhere a summary
is built -- including one and two levels into a sequence, which is exactly where
it used to fall through. And whatever does get shown is escaped, because the
review page renders this HTML with ``| safe``.
"""

from __future__ import annotations

from opi.forms.editables.editable import Editable, WidgetType
from opi.forms.editables.summarizers import HiddenSummary, MaskedSummary
from opi.forms.visualizers.sections import FormSection
from opi.forms.visualizers.visualizer import EditableVisualizer
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
