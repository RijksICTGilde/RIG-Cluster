"""
Layout system for form rendering.

This module provides layout elements inspired by Django Crispy Forms,
allowing programmatic control over form layout without custom templates.

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


@dataclass
class LayoutElement:
    """
    Base class for all layout elements.

    Attributes:
        css_class: Additional CSS classes
        attributes: Extra HTML attributes
    """

    css_class: str | None = None
    attributes: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Initialize default attributes dict if None."""
        if self.attributes is None:
            self.attributes = {}


# Type alias for layout children (can be field name string or another element)
LayoutChild = Union[str, "LayoutElement"]


@dataclass
class Row(LayoutElement):
    """
    Horizontal row of fields/columns.

    Used to place multiple fields side-by-side.

    Attributes:
        children: List of field names or Column elements
        gap: Gap between columns (e.g., "md", "lg")

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
    """
    Column within a row.

    Uses Bootstrap-style 12-column grid system.

    Attributes:
        child: Field name or nested layout element
        width: Column width (1-12, default 12 = full width)

    Example:
        Column("email", width=6)
    """

    child: LayoutChild = ""
    width: int = 12

    def __post_init__(self) -> None:
        """Validate width is in valid range."""
        super().__post_init__()
        if not 1 <= self.width <= 12:
            raise ValueError(f"Column width must be 1-12, got {self.width}")


@dataclass
class Fieldset(LayoutElement):
    """
    Group of fields with a legend.

    Used to visually group related fields together.

    Attributes:
        legend: Legend text (i18n key)
        children: List of field names or layout elements
        collapsible: Whether the fieldset can be collapsed
        collapsed: Initial collapsed state (only if collapsible)
        description: Optional description text below legend
        depends_on: YAML path of a field this fieldset depends on.
            When set, the fieldset is only rendered if the condition
            specified by ``show_when`` is met. Uses the same semantics
            as ``Editable.depends_on``. Paths containing ``[*]`` are
            resolved using the parent sequence index at render time.
        show_when: Condition dict evaluated against the ``depends_on``
            value. Supports the same operators as ``Editable.show_when``
            (e.g. ``{"contains": "metrics-scraper"}``).

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
    """
    Repeatable/cloneable field group.

    Used for list fields where users can add/remove items.

    Attributes:
        field_name: Name of the sequence field in the schema
        child_layout: Layout for each sequence item
        min_items: Minimum number of items (0 for optional)
        max_items: Maximum number of items (None for unlimited)
        add_label: Label for add button (i18n key)
        remove_label: Label for remove button (i18n key)
        item_template: Optional template name for rendering items
        sortable: Whether items can be reordered

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
    """
    Generic div wrapper for custom styling.

    Attributes:
        children: List of field names or layout elements

    Example:
        Div(
            children=["field1", "field2"],
            css_class="custom-wrapper"
        )
    """

    children: list[LayoutChild] = field(default_factory=list)


@dataclass
class HTML(LayoutElement):
    """
    Raw HTML insertion into the form.

    Use sparingly - prefer layout elements when possible.

    Attributes:
        content: Raw HTML content

    Example:
        HTML(content='<hr class="separator"/>')
    """

    content: str = ""


@dataclass
class TemplatePartial(LayoutElement):
    """
    Render a Jinja2 template partial into the form layout.

    Use this instead of ``HTML`` for non-trivial content blocks that are
    easier to maintain as template files.

    Attributes:
        template: Path relative to the templates directory
            (e.g. ``wizard/partials/domain_info.html.j2``)
        context: Optional extra template context variables

    Example:
        TemplatePartial(template="wizard/partials/domain_info.html.j2")
    """

    template: str = ""
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class DisplayBlock(LayoutElement):
    """Server-rendered display area updated via HTMX.

    Renders a small HTML fragment computed from form data. Source fields
    trigger ``hx-post`` to a generic endpoint that re-runs ``compute``
    and swaps only the display div.

    Attributes:
        display_id: Unique identifier, used as both the endpoint lookup
            key and the HTML element id for HTMX targeting.
        compute: Callable that receives ``yaml_data`` (dict) and a
            ``context`` dict, and returns a template context dict.
            The context dict comes from the DisplayBlock's inherited
            ``context`` field and can carry configuration such as
            ``deployment_index``.
        template: Jinja2 template path (relative to templates dir) that
            renders the computed context into an HTML fragment.
    """

    display_id: str = ""
    compute: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] = field(default_factory=lambda: lambda d, c: {})
    template: str = ""
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class Hidden(LayoutElement):
    """
    Hidden field that doesn't render visually.

    Attributes:
        field_name: Name of the hidden field

    Example:
        Hidden(field_name="csrf_token")
    """

    field_name: str = ""


@dataclass
class Submit(LayoutElement):
    """
    Submit button.

    Attributes:
        label: Button label (i18n key)
        name: Form field name for the button
        kind: Button style ("primary", "secondary", "danger")
        size: Button size ("sm", "md", "lg")
        icon: Optional icon name
        icon_position: Icon position ("before", "after")

    Example:
        Submit(label="common.save", kind="primary")
    """

    label: str = "common.submit"
    name: str = "submit"
    kind: str = "primary"
    size: str = "md"
    icon: str | None = None
    icon_position: str = "after"


@dataclass
class ButtonGroup(LayoutElement):
    """
    Group of action buttons.

    Attributes:
        buttons: List of Submit or Button elements
        alignment: Alignment ("start", "center", "end", "space-between")
        gap: Gap between buttons ("sm", "md", "lg")

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
