"""The ``FormRenderer``: schema extraction, layout and widget adaptation into form HTML."""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel, ValidationError

from opi.forms.editables.editable import WidgetType
from opi.forms.extractor import extract_fields_from_model
from opi.forms.field import FormField
from opi.forms.layout import (
    HTML,
    ButtonGroup,
    Column,
    DisplayBlock,
    Div,
    Fieldset,
    Hidden,
    LayoutElement,
    Row,
    Sequence,
    Submit,
    TemplatePartial,
)
from opi.services.catalog.publish_on_web.domain_config import DomainSetting, get_domain_setting
from opi.services.services import service_entry_name

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from opi.forms.visualizers.visualizer import EditableVisualizer
    from opi.forms.widgets.base import WidgetAdapter


class Translator(Protocol):
    """Protocol for translation functions."""

    def __call__(self, key: str) -> str:
        """Translate a key to the current locale."""
        ...


class OptionsProvider(Protocol):
    """Protocol for dynamic options providers."""

    def get_options(self) -> list[dict[str, Any]]:
        """Get options for a select/radio field."""
        ...


class IdentityTranslator:
    """Default translator that returns keys unchanged."""

    def __call__(self, key: str) -> str:
        return key


#: The translator leaves an unknown key alone, so a probe comes out unchanged.
_PROBE = "zad-probe-veld-"


def _all_fields(fields: Iterable[FormField]) -> Iterator[FormField]:
    """Every field in the tree, the rows of a sequence included."""
    for field in fields:
        yield field
        if field.children:
            yield from _all_fields(field.children)


class FormRenderer:
    """Renders a complete form from a schema, a layout and the data."""

    def __init__(
        self,
        widget_adapter: WidgetAdapter,
        translator: Translator | None = None,
        options_providers: dict[str, OptionsProvider] | None = None,
    ) -> None:
        self.adapter = widget_adapter
        self.translator = translator or IdentityTranslator()
        self.providers = options_providers or {}
        self._edit_mode = False
        # The wizard this form sits in. A TemplatePartial with endpoints of its own
        # (attachments) needs it to build the right URL; with a fixed flow in the template,
        # the attachments of a modal-edit wizard write to the create wizard's session.
        self._flow_id: str | None = None

    def render(
        self,
        schema: type[BaseModel],
        layout: LayoutElement | list[LayoutElement | str] | None = None,
        data: dict[str, Any] | None = None,
        errors: dict[str, list[str]] | None = None,
        form_id: str = "form",
        action: str = "",
        method: str = "post",
        enctype: str | None = None,
        htmx_attrs: dict[str, str] | None = None,
        edit_mode: bool = False,
    ) -> str:
        """Render a complete form to HTML; without a *layout* a default one is generated.

        In ``edit_mode`` a field marked ``readonly_on_edit`` renders read-only.
        """
        self._edit_mode = edit_mode
        data = data or {}
        errors = errors or {}

        fields = extract_fields_from_model(schema, data, errors)
        fields_by_name: dict[str, FormField] = {f.name: f for f in fields}
        self._resolve_options(fields)
        self._translate_fields(fields)
        if self._edit_mode:
            self._apply_edit_mode(fields)

        if layout is None:
            layout = self._generate_default_layout(fields)

        if isinstance(layout, list):
            content_parts = [self._render_layout_element(elem, fields_by_name) for elem in layout]
            content_html = "\n".join(content_parts)
        else:
            content_html = self._render_layout_element(layout, fields_by_name)

        form_start = self.adapter.render_form_start(form_id, action, method, enctype, htmx_attrs)
        form_end = self.adapter.render_form_end()

        return f"{form_start}\n{content_html}\n{form_end}"

    def render_field(
        self,
        field: FormField,
    ) -> str:
        """Render a single field to HTML."""
        return self.adapter.render_field(field)

    def validate(
        self,
        schema: type[BaseModel],
        data: dict[str, Any],
    ) -> tuple[BaseModel | None, dict[str, list[str]]]:
        """Validate *data* against *schema*, as ``(parsed model or None, errors per field)``."""
        try:
            parsed = schema.model_validate(data)
            return parsed, {}
        except ValidationError as e:
            return None, self._format_validation_errors(e)

    def render_from_editables(
        self,
        editables: list[EditableVisualizer],
        yaml_data: dict[str, Any],
        layout: LayoutElement | list[LayoutElement | str],
        errors: dict[str, list[str]] | None = None,
        edit_mode: bool = False,
        form_id: str = "form",
        action: str = "",
        method: str = "post",
        enctype: str | None = None,
        htmx_attrs: dict[str, str] | None = None,
    ) -> str:
        """Render a complete form from editable definitions + YAML data."""
        self._edit_mode = edit_mode
        fields_by_name = self._build_fields_from_editables(editables, yaml_data, errors, edit_mode)

        if isinstance(layout, list):
            content_parts = [self._render_layout_element(elem, fields_by_name, yaml_data) for elem in layout]
            content_html = "\n".join(content_parts)
        else:
            content_html = self._render_layout_element(layout, fields_by_name, yaml_data)

        form_start = self.adapter.render_form_start(form_id, action, method, enctype, htmx_attrs)
        form_end = self.adapter.render_form_end()
        return f"{form_start}\n{content_html}\n{form_end}"

    def render_fields_from_editables(
        self,
        editables: list[EditableVisualizer],
        yaml_data: dict[str, Any],
        layout: LayoutElement | list[LayoutElement | str] | None = None,
        errors: dict[str, list[str]] | None = None,
        edit_mode: bool = False,
        warnings: dict[str, list[str]] | None = None,
        flow_id: str | None = None,
    ) -> str:
        """Render form fields without form wrapper (for HTMX partial updates).

        ``layout`` may be None, since a FormSection is allowed not to declare one, and then
        every field is rendered in order: the same default the schema-driven renderers build.
        """
        self._edit_mode = edit_mode
        self._flow_id = flow_id
        fields_by_name = self._build_fields_from_editables(editables, yaml_data, errors, edit_mode, warnings=warnings)
        if layout is None:
            layout = list(fields_by_name)

        if isinstance(layout, list):
            content_parts = [self._render_layout_element(elem, fields_by_name, yaml_data) for elem in layout]
            return self.adapter.render_flow(content_parts)
        return self._render_layout_element(layout, fields_by_name, yaml_data)

    def take_unrendered_errors(
        self,
        editables: list[EditableVisualizer],
        yaml_data: dict[str, Any],
        errors: dict[str, list[str]],
        edit_mode: bool = False,
    ) -> list[str]:
        """Remove the errors no field on this screen carries, and return their messages.

        An error keyed to a path this form does not draw has nowhere to appear, so the step
        refuses to advance and says nothing (RIG-Cluster#179).

        Which paths are drawn is MEASURED, not derived from the paths: each one is offered
        to the renderer as a unique probe message, and whatever comes back on a field was
        drawn. Comparing paths cannot answer it: a virtualized field is keyed by its virtual
        spelling while its errors are read under the real one, and a sequence keys only the
        sequence itself while every row child carries its own.
        """
        paths = list(errors)
        probes = {path: [f"{_PROBE}{index}"] for index, path in enumerate(paths)}
        drawn = {
            message
            for field in _all_fields(
                self._build_fields_from_editables(editables, yaml_data, probes, edit_mode).values()
            )
            for message in field.errors
        }
        unrendered: list[str] = []
        for index, path in enumerate(paths):
            if f"{_PROBE}{index}" not in drawn:
                unrendered.extend(errors.pop(path))
        return unrendered

    def _build_fields_from_editables(
        self,
        editables: list[EditableVisualizer],
        yaml_data: dict[str, Any],
        errors: dict[str, list[str]] | None = None,
        edit_mode: bool = False,
        warnings: dict[str, list[str]] | None = None,
    ) -> dict[str, FormField]:
        """Convert editables to FormField dict for the layout pipeline."""
        from opi.forms.editables.service_path import smart_set_value
        from opi.forms.visualizers.bridge import (
            _is_service_active,
            editable_to_form_field,
            should_render_editable,
        )

        errors = errors or {}
        fields_by_name: dict[str, FormField] = {}
        provider_context = self._build_provider_context(yaml_data)

        # The forced value of a locked_by_service field goes into yaml_data, so a field
        # that depends_on it sees it.
        for editable in editables:
            if editable.locked_by_service and _is_service_active(editable.locked_by_service, yaml_data):
                smart_set_value(yaml_data, editable.editable.yaml_path, True)

        self._resolve_checkbox_group_defaults(editables, yaml_data, provider_context)

        for editable in editables:
            if not should_render_editable(editable, yaml_data, siblings=editables, edit_mode=edit_mode):
                continue

            if editable.widget == WidgetType.GROUP:
                group_fields = self._build_group_fields(
                    editable, yaml_data, errors, edit_mode, provider_context, warnings=warnings
                )
                fields_by_name.update(group_fields)
            elif editable.widget == WidgetType.SEQUENCE:
                seq_field = self._build_sequence_field(editable, yaml_data, errors, edit_mode, provider_context)
                fields_by_name[editable.editable.yaml_path] = seq_field
            else:
                form_field = editable_to_form_field(
                    editable,
                    yaml_data,
                    errors,
                    edit_mode=edit_mode,
                    provider_context=provider_context,
                    warnings=warnings,
                )
                if editable.widget == WidgetType.SERVICE_CARDS and "_locked_services" in yaml_data:
                    form_field.attributes["locked_values"] = ",".join(yaml_data["_locked_services"])
                fields_by_name[form_field.path] = form_field

                # A reverse-virtualized alias, so a layout reference by the real YAML path
                # finds the virtual FormField. The Sequence item rendering does the same.
                if form_field.virtualize:
                    from opi.forms.editables.editable import reverse_virtualize

                    real_path = reverse_virtualize(form_field.path, form_field.virtualize)
                    if real_path != form_field.path:
                        fields_by_name[real_path] = form_field

        all_fields = list(fields_by_name.values())
        self._translate_fields(all_fields)
        if edit_mode:
            self._apply_edit_mode(all_fields)

        return fields_by_name

    def _resolve_checkbox_group_defaults(
        self,
        editables: list[EditableVisualizer],
        yaml_data: dict[str, Any],
        provider_context: dict[str, Any] | None = None,
        index: int | None = None,
    ) -> None:
        """Fill an unset checkbox_group with every value its ``values_provider`` offers.

        Only when the value is None, so a selection the user already made is kept. It is what
        lets a dependent field (``depends_on`` / ``show_when``) see real values.
        """
        import logging as _logging

        from opi.forms.editables.editable import WidgetType
        from opi.forms.editables.path import resolve_path
        from opi.forms.editables.service_path import smart_get_value, smart_set_value
        from opi.forms.visualizers.bridge import _resolve_options

        _logger = _logging.getLogger(__name__)
        for editable in editables:
            if editable.widget != WidgetType.CHECKBOX_GROUP:
                continue
            ed = editable.editable
            if not ed.values_provider:
                continue

            concrete_path = resolve_path(ed.yaml_path, index)
            current_value = smart_get_value(yaml_data, concrete_path)
            _logger.info(
                "[resolve_defaults] path=%s, current_value=%r, is_none=%s",
                concrete_path,
                current_value,
                current_value is None,
            )
            if current_value is not None:
                continue

            options = _resolve_options(ed.values_provider, provider_context)
            if options:
                all_values = [str(o.get("value", "")) for o in options]
                _logger.info("[resolve_defaults] INJECTING all values: %r", all_values)
                smart_set_value(yaml_data, concrete_path, all_values)

    @staticmethod
    def _build_provider_context(yaml_data: dict[str, Any]) -> dict[str, Any]:
        """The context an options provider needs, read out of *yaml_data*."""
        import logging

        logger = logging.getLogger(__name__)

        context: dict[str, Any] = {}

        # Read the identity through service_entry_name, so every entry form resolves: a bare
        # string, the uniform record ({name, config}) and the legacy single-key dict. A
        # record's raw keys are "name" and "config", so without this every service carrying
        # config vanishes from the component services picker.
        services = yaml_data.get("services", [])
        if isinstance(services, list):
            names = [name for svc in services if (name := service_entry_name(svc)) is not None]
            context["project_services"] = names

        clusters = yaml_data.get("clusters", [])
        if isinstance(clusters, list) and clusters:
            context["cluster"] = clusters[0] if isinstance(clusters[0], str) else ""
        elif isinstance(clusters, str):
            context["cluster"] = clusters

        components = yaml_data.get("components", [])
        if isinstance(components, list):
            context["component_names"] = [
                c.get("name", "") for c in components if isinstance(c, dict) and c.get("name")
            ]

        # _original_deployment_names comes first: the add-deployment flow sets it to leave the
        # new slot out.
        deployments = yaml_data.get("deployments", [])
        original_names = yaml_data.get("_original_deployment_names")
        if isinstance(original_names, list):
            context["deployment_names"] = original_names
        elif isinstance(deployments, list):
            context["deployment_names"] = [
                d.get("name", "") for d in deployments if isinstance(d, dict) and d.get("name")
            ]

        # Every deployment is checked: in the edit flow the edited one need not be [0].
        if isinstance(deployments, list):
            for dep in deployments:
                if isinstance(dep, dict):
                    base_domain = get_domain_setting(dep, DomainSetting.BASE_DOMAIN)
                    custom_domain = dep.get("base-domain:custom")
                    if base_domain:
                        context["base_domain"] = base_domain
                        logger.debug(
                            f"_build_provider_context: base_domain={base_domain!r}, "
                            f"base_domain:custom={custom_domain!r}"
                        )
                        break

        return context

    def _build_group_fields(
        self,
        editable: EditableVisualizer,
        yaml_data: dict[str, Any],
        errors: dict[str, list[str]],
        edit_mode: bool,
        provider_context: dict[str, Any] | None = None,
        warnings: dict[str, list[str]] | None = None,
    ) -> dict[str, FormField]:
        """Flatten a group editable's children into individual form fields.

        A group is a transparent container: it does not render itself, its children take part
        in the field list directly. An enforcer error from the group lands on the parent path.
        """
        from opi.forms.visualizers.bridge import editable_to_form_field, should_render_editable

        fields: dict[str, FormField] = {}
        group_children = editable.children or []
        for child in group_children:
            if not should_render_editable(child, yaml_data, siblings=group_children, edit_mode=edit_mode):
                continue
            if child.widget == WidgetType.GROUP:
                fields.update(
                    self._build_group_fields(child, yaml_data, errors, edit_mode, provider_context, warnings=warnings)
                )
            elif child.widget == WidgetType.SEQUENCE:
                seq_field = self._build_sequence_field(child, yaml_data, errors, edit_mode, provider_context)
                fields[child.editable.yaml_path] = seq_field
            else:
                form_field = editable_to_form_field(
                    child,
                    yaml_data,
                    errors,
                    edit_mode=edit_mode,
                    provider_context=provider_context,
                    warnings=warnings,
                )
                fields[form_field.path] = form_field
        return fields

    def _build_sequence_field(
        self,
        editable: EditableVisualizer,
        yaml_data: dict[str, Any],
        errors: dict[str, list[str]],
        edit_mode: bool,
        provider_context: dict[str, Any] | None = None,
    ) -> FormField:
        """Build a FormField for a sequence editable with item children.

        When the sequence carries a ``virtualize`` mapping, the form path is
        rewritten to the virtual segment (e.g. ``services`` → ``_services-config``)
        and the same mapping travels to each non-sequence child through ``parent_virtualize``,
        the way ``_build_nested_sequence_field`` does it, so the modal-edit-component flow,
        which flattens nested sequences to the top level, does not collide with the sibling
        service-selection list.
        """
        from opi.forms.editables.editable import apply_virtualize
        from opi.forms.editables.service_path import smart_get_value
        from opi.forms.visualizers.bridge import editable_to_form_field, should_render_editable

        ed = editable.editable
        virt = ed.virtualize
        # The items always come from the REAL path; only the form-side name is virtual.
        items = smart_get_value(yaml_data, ed.yaml_path) or []
        if not isinstance(items, list):
            items = []

        # Padded to min_items, so the UI shows at least that many rows.
        while len(items) < ed.min_items:
            items.append({})

        form_path = apply_virtualize(ed.yaml_path, virt) if virt else ed.yaml_path
        seq_field = FormField(
            name=form_path,
            path=form_path,
            schema_type=list,
            widget_type="sequence",
            label=editable.label or "",
            description=editable.description,
            min_items=ed.min_items,
            max_items=ed.max_items,
            add_remove=ed.add_remove,
            virtualize=virt,
        )

        ref_field_name, used_refs_by_index = self._detect_reference_exclusions(editable, items)

        children: list[FormField] = []
        for index in range(len(items)):
            # Per-item provider context. ``row_data`` holds this row's own stored values, so
            # a provider can answer about the row it renders in ("which deployments does the
            # project chosen in THIS row have"), and ``exclude_references`` the narrower case
            # of what the other rows already took.
            row = items[index] if isinstance(items[index], dict) else {}
            item_context: dict[str, Any] = {**(provider_context or {}), "row_data": row}
            if ref_field_name and used_refs_by_index:
                item_context["exclude_references"] = [r for i, r in used_refs_by_index.items() if i != index]

            item_children: list[FormField] = []
            seq_children = editable.children or []

            self._resolve_checkbox_group_defaults(seq_children, yaml_data, item_context, index=index)

            for child_editable in seq_children:
                if not should_render_editable(
                    child_editable, yaml_data, index=index, siblings=seq_children, edit_mode=edit_mode
                ):
                    continue
                if child_editable.widget == WidgetType.SEQUENCE:
                    nested_seq = self._build_nested_sequence_field(
                        child_editable,
                        yaml_data,
                        errors,
                        edit_mode,
                        parent_index=index,
                        provider_context=item_context,
                    )
                    item_children.append(nested_seq)
                else:
                    child_field = editable_to_form_field(
                        child_editable,
                        yaml_data,
                        errors,
                        index=index,
                        edit_mode=edit_mode,
                        provider_context=item_context,
                        parent_virtualize=virt,
                    )
                    item_children.append(child_field)

            item_field = FormField(
                name=f"{form_path}[{index}]",
                path=f"{form_path}[{index}]",
                schema_type=dict,
                widget_type="sequence_item",
                label=f"Item {index + 1}",
                required=False,
                children=item_children,
            )
            children.append(item_field)

        seq_field.children = children
        return seq_field

    @staticmethod
    def _detect_reference_exclusions(
        editable: EditableVisualizer,
        items: list[Any],
    ) -> tuple[str | None, dict[int, str]]:
        """The reference field of a sequence and the value each row took, or None and empty.

        Only for a sequence with a ``ComponentReferenceOptionsProvider`` child, so that
        provider can leave the references other rows already took out of its options.
        """
        ref_field_name: str | None = None
        for child in editable.children or []:
            if child.editable.values_provider == "ComponentReferenceOptionsProvider":
                ref_field_name = child.editable.yaml_path.rsplit("/", 1)[-1].split("[")[0]
                break

        used_refs: dict[int, str] = {}
        if ref_field_name:
            for idx, item in enumerate(items):
                if isinstance(item, dict):
                    ref = item.get(ref_field_name, "")
                    if ref:
                        used_refs[idx] = ref

        return ref_field_name, used_refs

    def _build_nested_sequence_field(
        self,
        editable: EditableVisualizer,
        yaml_data: dict[str, Any],
        errors: dict[str, list[str]],
        edit_mode: bool,
        parent_index: int,
        provider_context: dict[str, Any] | None = None,
    ) -> FormField:
        """Build a nested sequence (e.g., deployments[0]/components)."""
        from opi.forms.editables.editable import apply_virtualize
        from opi.forms.editables.path import resolve_path
        from opi.forms.editables.service_path import smart_get_value
        from opi.forms.visualizers.bridge import editable_to_form_field, should_render_editable

        ed = editable.editable
        real_path = resolve_path(ed.yaml_path, parent_index)
        virt = ed.virtualize
        form_path = apply_virtualize(real_path, virt) if virt else real_path

        items = smart_get_value(yaml_data, real_path) or []
        if not isinstance(items, list):
            items = []

        # Padded to min_items, so the UI shows at least that many rows.
        while len(items) < ed.min_items:
            items.append({})

        nested_field = FormField(
            name=form_path,
            path=form_path,
            schema_type=list,
            widget_type="sequence",
            label=editable.label or "",
            min_items=ed.min_items,
            max_items=ed.max_items,
            add_remove=ed.add_remove,
            virtualize=virt,
        )

        children: list[FormField] = []
        for child_index in range(len(items)):
            item_children: list[FormField] = []
            for child_editable in editable.children or []:
                # The parent [*] is resolved here, so editable_to_form_field and show_when
                # only have the child [*], the nested item index, left to resolve.
                resolved_editable = copy.copy(child_editable)
                resolved_ed = copy.copy(child_editable.editable)
                resolved_ed.yaml_path = resolve_path(child_editable.editable.yaml_path, parent_index)
                if resolved_ed.depends_on:
                    resolved_ed.depends_on = resolve_path(resolved_ed.depends_on, parent_index)
                resolved_editable.editable = resolved_ed
                # ``show_when`` applies inside a nested sequence too, so a field can be
                # shown or hidden per row on a sibling's value.
                if not should_render_editable(
                    resolved_editable, yaml_data, index=child_index, siblings=editable.children, edit_mode=edit_mode
                ):
                    continue
                child_field = editable_to_form_field(
                    resolved_editable,
                    yaml_data,
                    errors,
                    index=child_index,
                    edit_mode=edit_mode,
                    provider_context=provider_context,
                    parent_virtualize=virt,
                )
                item_children.append(child_field)

            item_field = FormField(
                name=f"{form_path}[{child_index}]",
                path=f"{form_path}[{child_index}]",
                schema_type=dict,
                widget_type="sequence_item",
                label=f"Item {child_index + 1}",
                required=False,
                children=item_children,
            )
            children.append(item_field)

        nested_field.children = children
        return nested_field

    def _resolve_options(self, fields: list[FormField]) -> None:
        """Resolve dynamic options for select/radio fields."""
        for field in fields:
            provider_name = field.attributes.get("options_provider")
            if provider_name and provider_name in self.providers:
                field.options = self.providers[provider_name].get_options()

            if field.children:
                self._resolve_options(field.children)

    def _translate_fields(self, fields: list[FormField]) -> None:
        """Translate field labels and descriptions."""
        for field in fields:
            if field.label and "." in field.label:
                field.label = self.translator(field.label)

            if field.description and "." in field.description:
                field.description = self.translator(field.description)

            field.errors = [self.translator(e) for e in field.errors]

            if field.children:
                self._translate_fields(field.children)

    def _apply_edit_mode(self, fields: list[FormField]) -> None:
        """Apply edit mode constraints to fields.

        A field marked ``readonly_on_edit`` becomes readonly, and every field gets
        ``edit_mode`` so a widget can adapt to it.
        """
        for field in fields:
            field.edit_mode = True
            if field.readonly_on_edit:
                field.readonly = True

            if field.children:
                self._apply_edit_mode(field.children)

    def _generate_default_layout(self, fields: list[FormField]) -> list[LayoutElement | str]:
        """A simple vertical layout: the field names in order."""
        return [f.name for f in fields]

    def _render_layout_element(
        self,
        element: LayoutElement | str,
        fields: dict[str, FormField],
        yaml_data: dict[str, Any] | None = None,
    ) -> str:
        """Render one layout element, or the field a bare string names.

        ``yaml_data`` is what a ``DisplayBlock`` computes from.
        """
        if isinstance(element, str):
            field = fields.get(element)
            if field:
                return self.adapter.render_field(field)
            return ""

        if isinstance(element, Row):
            children_html = [self._render_layout_element(child, fields, yaml_data) for child in element.children]
            return self.adapter.render_row(element, children_html)

        if isinstance(element, Column):
            child_html = self._render_layout_element(element.child, fields, yaml_data)
            return self.adapter.render_column(element, child_html)

        if isinstance(element, Fieldset):
            if element.depends_on:
                dep_field = fields.get(element.depends_on)
                dep_value = dep_field.value if dep_field else None
                from opi.forms.visualizers.bridge import evaluate_show_when

                if not evaluate_show_when(dep_value, element.show_when):
                    return ""
            children_html = [self._render_layout_element(child, fields, yaml_data) for child in element.children]
            return self.adapter.render_fieldset(element, children_html)

        if isinstance(element, Div):
            children_html = [self._render_layout_element(child, fields, yaml_data) for child in element.children]
            return self.adapter.render_div(element, children_html)

        if isinstance(element, Sequence):
            field = fields.get(element.field_name)
            if field and field.children:
                items_html = []
                for i, child_field in enumerate(field.children):
                    if element.child_layout:
                        item_prefix = f"{element.field_name}[{i}]/"
                        relative_fields: dict[str, FormField] = {}
                        for cf in child_field.children:
                            rel_key = cf.path.removeprefix(item_prefix)
                            relative_fields[rel_key] = cf

                        # A reverse-virtualized alias, so a layout reference by the real
                        # YAML path finds the virtual FormField.
                        from opi.forms.editables.editable import reverse_virtualize

                        for rel_key, cf in list(relative_fields.items()):
                            if cf.virtualize:
                                real_key = reverse_virtualize(rel_key, cf.virtualize)
                                if real_key != rel_key:
                                    relative_fields[real_key] = cf

                        if isinstance(element.child_layout, list):
                            parts = [self._render_layout_element(el, relative_fields) for el in element.child_layout]
                            item_html = "\n".join(parts)
                        else:
                            item_html = self._render_layout_element(element.child_layout, relative_fields)
                    else:
                        child_fields_html = [self.adapter.render_field(cf) for cf in child_field.children]
                        item_html = "\n".join(child_fields_html)
                    items_html.append(self.adapter.render_sequence_item(field, i, item_html))
                return self.adapter.render_sequence(field, items_html)
            elif field:
                return self.adapter.render_sequence(field, [])
            return f"<!-- Unknown sequence field: {element.field_name} -->"

        if isinstance(element, Hidden):
            field = fields.get(element.field_name)
            if field:
                return self.adapter.render_hidden(field)
            return f"<!-- Unknown hidden field: {element.field_name} -->"

        if isinstance(element, HTML):
            return element.content

        if isinstance(element, TemplatePartial):
            from opi.http.render.templates_lotc import templates_lotc

            tmpl = templates_lotc.env.get_template(element.template)
            # The flow_id goes along: a partial with endpoints of its own has to know WHICH
            # wizard it sits in. element.context wins, so a partial can override it.
            ctx = {"flow_id": self._flow_id, **(yaml_data or {}), **element.context}
            return tmpl.render(ctx)

        if isinstance(element, DisplayBlock):
            from opi.http.render.templates_lotc import templates_lotc

            context = element.compute(yaml_data or {}, element.context)
            tmpl = templates_lotc.env.get_template(element.template)
            inner = tmpl.render(context)
            return f'<div id="display-{element.display_id}">{inner}</div>'

        if isinstance(element, Submit):
            return self.adapter.render_submit(element)

        if isinstance(element, ButtonGroup):
            buttons_html = [self._render_layout_element(btn, fields) for btn in element.buttons]
            return self.adapter.render_button_group(element, buttons_html)

        return f"<!-- Unknown layout element: {type(element).__name__} -->"

    def _format_validation_errors(self, error: ValidationError) -> dict[str, list[str]]:
        """Format Pydantic validation errors to field-keyed dict."""
        errors: dict[str, list[str]] = {}

        for err in error.errors():
            loc = err.get("loc", ())
            path = ".".join(str(part) for part in loc)

            msg = err.get("msg", "Validation error")

            if path not in errors:
                errors[path] = []
            errors[path].append(msg)

        return errors
