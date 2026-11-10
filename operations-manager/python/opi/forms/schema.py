"""``FormMeta``: form metadata attached to a Pydantic field through ``Annotated``.

Example:
    class ProjectForm(BaseModel):
        display_name: Annotated[str, FormMeta(
            label="project.display_name",
            description="project.display_name.help",
            widget="text"
        )] = Field(min_length=3, max_length=100)
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FormMeta:
    """Everything needed to render one form field: widget, labels, conditions and behaviour."""

    widget: str | None = None
    label: str | None = None
    description: str | None = None
    placeholder: str | None = None
    options_provider: str | None = None
    converter: str | None = None
    validator: str | None = None
    depends_on: str | None = None
    show_when: dict[str, Any] | None = None
    css_class: str | None = None
    attributes: dict[str, str] | None = field(default_factory=dict)
    order: int = 0
    section: str | None = None
    readonly: bool = False
    readonly_on_edit: bool = False
    htmx_trigger: str | None = None
    htmx_target: str | None = None
    htmx_swap: str | None = None
    min_items: int = 0
    max_items: int | None = None

    def __post_init__(self) -> None:
        if self.attributes is None:
            self.attributes = {}


def get_form_meta(field_info: Any) -> FormMeta | None:
    """The ``FormMeta`` in a Pydantic field's metadata, or None when it carries none."""
    if not hasattr(field_info, "metadata"):
        return None

    for meta in field_info.metadata:
        if isinstance(meta, FormMeta):
            return meta
    return None


def infer_widget_type(python_type: type | None, form_meta: FormMeta | None) -> str:
    """The widget type for a field: what ``form_meta`` declares, else inferred from the type.

    ``python_type`` is None when the model declares no annotation, which pydantic allows.
    """
    if form_meta and form_meta.widget:
        return form_meta.widget

    type_name = getattr(python_type, "__name__", str(python_type))

    origin = getattr(python_type, "__origin__", None)
    if origin is list:
        return "sequence"
    if origin is dict:
        return "mapping"

    type_widget_map = {
        "str": "text",
        "int": "number",
        "float": "number",
        "bool": "checkbox",
        "date": "date",
        "datetime": "datetime",
        "time": "time",
    }

    return type_widget_map.get(type_name, "text")
