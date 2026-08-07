"""What a summary screen may show.

Whatever a summary shows is escaped: the review page renders this HTML with
``| safe``, so a value someone typed into a form is markup unless it is escaped
here.
"""

from __future__ import annotations

from opi.forms.editables.editable import Editable, WidgetType
from opi.forms.visualizers.sections import FormSection
from opi.forms.visualizers.visualizer import EditableVisualizer
from opi.web.router_wizard import _build_section_summary

XSS = "<img src=x onerror=alert(1)>"


def _field(path: str, label: str, **editable_kwargs) -> EditableVisualizer:
    return EditableVisualizer(
        editable=Editable(yaml_path=path, **editable_kwargs),
        widget=WidgetType.TEXT,
        label=label,
    )


def _section(*editables: EditableVisualizer) -> FormSection:
    return FormSection(section_id="test", title="Test", editables=list(editables))


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
