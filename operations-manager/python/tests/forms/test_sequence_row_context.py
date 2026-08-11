"""A provider rendered inside a sequence row sees that row's own values (RC-42).

The sequence renderer already built a per-row ``item_context`` for one narrow case
(``exclude_references``: which component references the OTHER rows took). ``row_data``
generalises it to "this row's stored values", which is what a dependent select needs: the
cross-domain peer-deployment list is a function of the peer project chosen in the SAME row.

Locked here because it is the assumption the whole cross-domain cascade rests on -- the
providers module said for a long time that the framework could not do this.
"""

from typing import Any

from opi.forms.editables.editable import Editable, WidgetType
from opi.forms.renderer import FormRenderer
from opi.forms.visualizers.providers import PROVIDER_REGISTRY
from opi.forms.visualizers.visualizer import EditableVisualizer
from opi.forms.widgets.lotc import LOTCWidgetAdapter

_SEEN: list[dict[str, Any] | None] = []


class _RecordingProvider:
    """Records the ``row_data`` it was constructed with, once per rendered row."""

    def __init__(self, row_data: dict[str, Any] | None = None, current_value: str | None = None) -> None:
        _SEEN.append(row_data)
        self.row_data = row_data or {}

    def get_options(self) -> list[dict[str, Any]]:
        return [{"value": self.row_data.get("project", ""), "label": "x"}]


def _sequence() -> EditableVisualizer:
    child = EditableVisualizer(
        editable=Editable(yaml_path="rules[*]/peer", values_provider="_RecordingProvider"),
        widget=WidgetType.SELECT,
        label="Peer",
    )
    return EditableVisualizer(
        editable=Editable(yaml_path="rules", children=[child.editable]),
        widget=WidgetType.SEQUENCE,
        label="Regels",
        children=[child],
    )


def _render(items: list[dict[str, Any]]) -> None:
    _SEEN.clear()
    PROVIDER_REGISTRY["_RecordingProvider"] = _RecordingProvider
    try:
        renderer = FormRenderer(widget_adapter=LOTCWidgetAdapter())
        renderer._build_sequence_field(_sequence(), {"rules": items}, {}, edit_mode=False)
    finally:
        del PROVIDER_REGISTRY["_RecordingProvider"]


def test_each_row_sees_its_own_values() -> None:
    _render([{"peer": "a", "project": "alpha"}, {"peer": "b", "project": "beta"}])
    assert [seen["project"] for seen in _SEEN if seen] == ["alpha", "beta"]


def test_empty_row_gives_an_empty_mapping_not_none() -> None:
    _render([{}])
    assert _SEEN == [{}]


def test_non_mapping_row_does_not_break_the_render() -> None:
    # Sequence items are dicts in practice, but a hand-edited project file can hold anything.
    _render(["losse-tekst"])  # type: ignore[list-item]
    assert _SEEN == [{}]
