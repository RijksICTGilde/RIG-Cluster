"""Layout elements that place a form on the screen from Python, without a custom template.

Example:
    layout = Fieldset(
        legend="project.details",
        children=[
            Row([
                Column("display_name", width=8),
                Column("cluster", width=4),
            ]),
            "description",  # Full width field
        ],
    )
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Union

if TYPE_CHECKING:
    from collections.abc import Callable


#: The named place in the component form, straight after the image field. Why a slot and not
#: an anchor on the node: see ``docs/development/services.md``.
COMPONENT_IMAGE_SLOT = "component-image"


@dataclass
class LayoutElement:
    """Base class for all layout elements."""

    css_class: str | None = None
    attributes: dict[str, str] = field(default_factory=dict)
    #: The form slot this node belongs in. Without one the node is appended where the
    #: collecting form appends its unslotted nodes.
    slot: str | None = None

    def __post_init__(self) -> None:
        if self.attributes is None:
            self.attributes = {}


LayoutChild = Union[str, "LayoutElement"]


@dataclass
class Row(LayoutElement):
    """Fields or columns side by side; ``gap`` is a size name such as "md" or "lg".

    Example:
        Row([
            Column("first_name", width=6),
            Column("last_name", width=6),
        ])
    """

    children: list[LayoutChild] = field(default_factory=list)
    gap: str = "md"


@dataclass
class Column(LayoutElement):
    """A column in a row, on a 12-column grid; ``width`` is 1 to 12.

    Example:
        Column("email", width=6)
    """

    child: LayoutChild = ""
    width: int = 12

    def __post_init__(self) -> None:
        super().__post_init__()
        if not 1 <= self.width <= 12:
            raise ValueError(f"Column width must be 1-12, got {self.width}")


@dataclass
class Fieldset(LayoutElement):
    """A group of fields behind one legend.

    ``depends_on`` and ``show_when`` work like their namesakes on ``Editable``: with both set
    the fieldset only renders when the condition holds against the value at ``depends_on``,
    and a ``[*]`` in that path is resolved with the parent sequence index at render time.

    Example:
        Fieldset(
            legend="project.services",
            description="project.services.description",
            children=["services"],
            collapsible=True,
        )
    """

    legend: str = ""
    children: list[LayoutChild] = field(default_factory=list)
    collapsible: bool = False
    collapsed: bool = False
    description: str | None = None
    depends_on: str | None = None
    show_when: dict[str, Any] | None = None


@dataclass
class Sequence(LayoutElement):
    """A repeatable field group, for a list the user can add to and remove from.

    Example:
        Sequence(
            field_name="components",
            child_layout=Row([
                Column("name", width=4),
                Column("type", width=4),
                Column("port", width=4),
            ]),
            min_items=1,
            add_label="components.add",
        )
    """

    field_name: str = ""
    child_layout: LayoutElement | list[LayoutChild] | None = None
    min_items: int = 0
    max_items: int | None = None
    add_label: str = "common.add"
    remove_label: str = "common.remove"
    item_template: str | None = None
    sortable: bool = False


@dataclass
class Div(LayoutElement):
    """A plain div wrapper, for custom styling.

    Example:
        Div(
            children=["field1", "field2"],
            css_class="custom-wrapper"
        )
    """

    children: list[LayoutChild] = field(default_factory=list)


@dataclass
class HTML(LayoutElement):
    """Raw HTML in the form. Use sparingly: a layout element is preferable.

    Example:
        HTML(content='<hr class="separator"/>')
    """

    content: str = ""


@dataclass
class TemplatePartial(LayoutElement):
    """A Jinja2 template partial in the form layout; ``template`` is relative to the
    templates directory. Preferable to ``HTML`` for a content block of any size.

    Example:
        TemplatePartial(template="wizard/partials/domain_info.html.j2")
    """

    template: str = ""
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class DisplayBlock(LayoutElement):
    """Server-rendered display area updated via HTMX.

    A source field triggers an ``hx-post`` to a generic endpoint that re-runs ``compute`` and
    swaps only the display div.

    ``display_id`` is both the endpoint lookup key and the HTML element id HTMX targets.
    ``compute`` receives ``yaml_data`` and the element's own ``context`` (which can carry
    configuration such as ``deployment_index``) and returns the context ``template`` renders.
    """

    display_id: str = ""
    compute: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] = field(default_factory=lambda: lambda d, c: {})
    template: str = ""
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class Hidden(LayoutElement):
    """A field that is submitted but not shown.

    Example:
        Hidden(field_name="csrf_token")
    """

    field_name: str = ""


@dataclass
class Submit(LayoutElement):
    """A submit button; ``kind`` is a LOTC button variant ("primary", "secondary", ...).

    Example:
        Submit(label="common.save", kind="primary")
    """

    label: str = "common.submit"
    name: str = "submit"
    kind: str = "primary"
    icon: str | None = None
    icon_position: str = "after"


@dataclass
class ButtonGroup(LayoutElement):
    """A row of action buttons, aligned "start", "center", "end" or "space-between".

    Example:
        ButtonGroup(
            buttons=[
                Submit(label="common.save", kind="primary"),
                Submit(label="common.cancel", kind="secondary"),
            ],
            alignment="end"
        )
    """

    buttons: list[Submit] = field(default_factory=list)
    alignment: str = "start"
    gap: str = "md"


def layout_field_names(layout: LayoutChild | list[LayoutChild] | None) -> set[str]:
    """Every field name *layout* places on the screen.

    A layout refers to fields by name: bare strings, ``Sequence.field_name`` and
    ``Hidden.field_name``. Containers (Row/Column/Fieldset/Div) contribute what
    their children contribute. Everything else draws no field.

    This exists so "is this editable actually drawn" is answerable without
    rendering. An editable registered on a section but absent from its layout is
    the worst of both: the flow counts it as writable, and the form never shows
    it, so every save submits it empty and wipes what was there.
    """
    if layout is None:
        return set()
    if isinstance(layout, str):
        return {layout}
    if isinstance(layout, list):
        names: set[str] = set()
        for child in layout:
            names |= layout_field_names(child)
        return names
    if isinstance(layout, Column):
        return layout_field_names(layout.child)
    if isinstance(layout, (Row, Fieldset, Div)):
        return layout_field_names(layout.children)
    if isinstance(layout, Sequence):
        return {layout.field_name} | layout_field_names(layout.child_layout)
    if isinstance(layout, Hidden):
        return {layout.field_name}
    return set()
