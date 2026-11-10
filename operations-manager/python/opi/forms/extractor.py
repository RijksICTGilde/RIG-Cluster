"""Extraction of ``FormField`` instances from Pydantic models annotated with ``FormMeta``."""

from typing import TYPE_CHECKING, Any, cast, get_args, get_origin

from pydantic import BaseModel

from opi.forms.field import FormField
from opi.forms.schema import get_form_meta, infer_widget_type

if TYPE_CHECKING:
    from pydantic.fields import FieldInfo


def extract_fields_from_model(
    model: type[BaseModel],
    data: dict[str, Any] | None = None,
    errors: dict[str, list[str]] | None = None,
    path_prefix: str = "",
) -> list[FormField]:
    data = data or {}
    errors = errors or {}
    fields: list[FormField] = []

    for field_name, field_info in model.model_fields.items():
        full_path = f"{path_prefix}{field_name}" if path_prefix else field_name
        form_field = extract_single_field(
            name=field_name,
            path=full_path,
            field_info=field_info,
            value=data.get(field_name),
            field_errors=errors.get(full_path, []),
        )
        fields.append(form_field)

    return fields


def extract_single_field(
    name: str,
    path: str,
    field_info: FieldInfo,
    value: Any = None,
    field_errors: list[str] | None = None,
) -> FormField:
    field_errors = field_errors or []

    # pydantic 2.14 types FieldInfo.annotation as TypeForm, wider than the type | None the
    # form layer works with. The value itself is still a type or None.
    python_type = cast("type | None", field_info.annotation)
    origin_type = get_origin(python_type)

    form_meta = get_form_meta(field_info)
    widget_type = infer_widget_type(python_type, form_meta)
    is_required = field_info.is_required()

    form_field = FormField(
        name=name,
        path=path,
        schema_type=python_type,
        widget_type=widget_type,
        label=(form_meta.label if form_meta else None) or name.replace("_", " ").title(),
        required=is_required,
        description=form_meta.description if form_meta else None,
        placeholder=form_meta.placeholder if form_meta else None,
        value=value,
        errors=field_errors,
        css_class=form_meta.css_class if form_meta else None,
        readonly=form_meta.readonly if form_meta else False,
        readonly_on_edit=form_meta.readonly_on_edit if form_meta else False,
        min_items=form_meta.min_items if form_meta else 0,
        max_items=form_meta.max_items if form_meta else None,
    )

    if form_meta:
        if form_meta.htmx_trigger:
            form_field.htmx_attrs["hx-trigger"] = form_meta.htmx_trigger
        if form_meta.htmx_target:
            form_field.htmx_attrs["hx-target"] = form_meta.htmx_target
        if form_meta.htmx_swap:
            form_field.htmx_attrs["hx-swap"] = form_meta.htmx_swap

        if form_meta.options_provider:
            form_field.attributes["options_provider"] = form_meta.options_provider

        if form_meta.converter:
            form_field.attributes["converter"] = form_meta.converter

        if form_meta.attributes:
            form_field.attributes.update(form_meta.attributes)

    if origin_type is list and widget_type == "sequence":
        args = get_args(python_type)
        if args and len(args) > 0:
            item_type = args[0]
            if isinstance(item_type, type) and issubclass(item_type, BaseModel):
                form_field.children = _extract_sequence_children(
                    item_type=item_type, values=value or [], path=path, errors={}
                )

    if widget_type == "nested":
        # Strip the Optional/Union wrapper: the first non-None argument is the model.
        inner_type = python_type
        if origin_type is not None:
            args = get_args(python_type)
            for arg in args:
                if arg is not type(None):
                    inner_type = arg
                    break

        if isinstance(inner_type, type) and issubclass(inner_type, BaseModel):
            nested_data = value if isinstance(value, dict) else {}
            form_field.children = extract_fields_from_model(
                model=inner_type,
                data=nested_data,
                errors={},
                path_prefix=f"{path}.",
            )

    return form_field


def _extract_sequence_children(
    item_type: type[BaseModel],
    values: list[Any],
    path: str,
    errors: dict[str, list[str]],
) -> list[FormField]:
    """One ``FormField`` per sequence item, each with the item's own fields as children."""
    children: list[FormField] = []

    for index, item_value in enumerate(values):
        item_path = f"{path}[{index}]"

        if isinstance(item_value, BaseModel):
            item_data = item_value.model_dump()
        elif isinstance(item_value, dict):
            item_data = item_value
        else:
            item_data = {}

        item_fields = extract_fields_from_model(
            model=item_type,
            data=item_data,
            errors=errors,
            path_prefix=f"{item_path}.",
        )

        item_field = FormField(
            name=f"{path}[{index}]",
            path=item_path,
            schema_type=item_type,
            widget_type="sequence_item",
            label=f"Item {index + 1}",
            required=False,
            children=item_fields,
        )
        children.append(item_field)

    return children


def group_fields_by_section(fields: list[FormField], model: type[BaseModel]) -> dict[str | None, list[FormField]]:
    """Group the fields by the ``section`` of their ``FormMeta``; *model* carries that meta."""
    sections: dict[str | None, list[FormField]] = {}

    for field in fields:
        field_info = model.model_fields.get(field.name)
        form_meta = get_form_meta(field_info) if field_info else None
        section = form_meta.section if form_meta else None

        if section not in sections:
            sections[section] = []
        sections[section].append(field)

    return sections
