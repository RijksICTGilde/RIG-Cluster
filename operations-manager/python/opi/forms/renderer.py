"""
Form renderer that orchestrates form generation.

The FormRenderer combines schema extraction, layout processing,
and widget adaptation to produce complete form HTML.
"""

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

if TYPE_CHECKING:
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


class FormRenderer:
    """
    Renders complete forms from schema + layout + data.

    The renderer:
    1. Extracts FormField instances from Pydantic schema
    2. Resolves dynamic options from providers
    3. Translates labels/descriptions
    4. Renders using the widget adapter and layout
    """

    def __init__(
        self,
        widget_adapter: WidgetAdapter,
        translator: Translator | None = None,
        options_providers: dict[str, OptionsProvider] | None = None,
    ) -> None:
        """
        Initialize the form renderer.

        Args:
            widget_adapter: Widget adapter for HTML rendering
            translator: Optional translator for i18n
            options_providers: Dict of provider name -> OptionsProvider
        """
        self.adapter = widget_adapter
        self.translator = translator or IdentityTranslator()
        self.providers = options_providers or {}
        self._edit_mode = False

    def render(
        self,
        schema: type[BaseModel],
        layout: LayoutElement | list[LayoutElement] | None = None,
        data: dict[str, Any] | None = None,
        errors: dict[str, list[str]] | None = None,
        form_id: str = "form",
        action: str = "",
        method: str = "post",
        enctype: str | None = None,
        htmx_attrs: dict[str, str] | None = None,
        edit_mode: bool = False,
    ) -> str:
        """
        Render a complete form to HTML.

        Args:
            schema: Pydantic model class defining the form structure
            layout: Optional layout definition (auto-generated if None)
            data: Current field values
            errors: Validation errors per field path
            form_id: HTML form ID
            action: Form action URL
            method: HTTP method
            enctype: Form enctype (e.g., 'multipart/form-data')
            htmx_attrs: Optional HTMX attributes for the form
            edit_mode: If True, fields with readonly_on_edit will be readonly

        Returns:
            Complete form HTML string
        """
        self._edit_mode = edit_mode
        data = data or {}
        errors = errors or {}

        # Extract fields from schema
        fields = extract_fields_from_model(schema, data, errors)

        # Build field lookup by name
        fields_by_name: dict[str, FormField] = {f.name: f for f in fields}

        # Resolve options for select fields
        self._resolve_options(fields)

        # Translate field labels/descriptions
        self._translate_fields(fields)

        # Apply edit mode constraints (readonly_on_edit -> readonly)
        if self._edit_mode:
            self._apply_edit_mode(fields)

        # Generate layout if not provided
        if layout is None:
            layout = self._generate_default_layout(fields)

        # Render form content using layout
        if isinstance(layout, list):
            content_parts = [self._render_layout_element(elem, fields_by_name) for elem in layout]
            content_html = "\n".join(content_parts)
        else:
            content_html = self._render_layout_element(layout, fields_by_name)

        # Wrap in form tags
        form_start = self.adapter.render_form_start(form_id, action, method, enctype, htmx_attrs)
        form_end = self.adapter.render_form_end()

        return f"{form_start}\n{content_html}\n{form_end}"

    def render_fields(
        self,
        schema: type[BaseModel],
        layout: LayoutElement | list[LayoutElement] | None = None,
        data: dict[str, Any] | None = None,
        errors: dict[str, list[str]] | None = None,
        edit_mode: bool = False,
    ) -> str:
        """
        Render form fields without the form wrapper.

        Useful for HTMX partial updates or embedding in existing forms.

        Args:
            schema: Pydantic model class defining the form structure
            layout: Optional layout definition
            data: Current field values
            errors: Validation errors per field path
            edit_mode: If True, fields with readonly_on_edit will be readonly

        Returns:
            Form fields HTML string (no <form> wrapper)
        """
        self._edit_mode = edit_mode
        data = data or {}
        errors = errors or {}

        # Extract fields from schema
        fields = extract_fields_from_model(schema, data, errors)

        # Build field lookup by name
        fields_by_name: dict[str, FormField] = {f.name: f for f in fields}

        # Resolve options for select fields
        self._resolve_options(fields)

        # Translate field labels/descriptions
        self._translate_fields(fields)

        # Apply edit mode constraints (readonly_on_edit -> readonly)
        if self._edit_mode:
            self._apply_edit_mode(fields)

        # Generate layout if not provided
        if layout is None:
            layout = self._generate_default_layout(fields)

        # Render using layout
        if isinstance(layout, list):
            content_parts = [self._render_layout_element(elem, fields_by_name) for elem in layout]
            return "\n".join(content_parts)
        else:
            return self._render_layout_element(layout, fields_by_name)

    def render_field(
        self,
        field: FormField,
    ) -> str:
        """
        Render a single field.

        Args:
            field: FormField to render

        Returns:
            Field HTML string
        """
        return self.adapter.render_field(field)

    def validate(
        self,
        schema: type[BaseModel],
        data: dict[str, Any],
    ) -> tuple[BaseModel | None, dict[str, list[str]]]:
        """
        Validate form data against the schema.

        Args:
            schema: Pydantic model class
            data: Form data to validate

        Returns:
            Tuple of (parsed_model or None, errors_dict)
        """
        try:
            parsed = schema.model_validate(data)
            return parsed, {}
        except ValidationError as e:
            return None, self._format_validation_errors(e)

    # ------------------------------------------------------------------
    # Editable-driven rendering
    # ------------------------------------------------------------------

    def render_from_editables(
        self,
        editables: list[EditableVisualizer],
        yaml_data: dict[str, Any],
        layout: LayoutElement | list,
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
        layout: LayoutElement | list,
        errors: dict[str, list[str]] | None = None,
        edit_mode: bool = False,
    ) -> str:
        """Render form fields without form wrapper (for HTMX partial updates)."""
        self._edit_mode = edit_mode
        fields_by_name = self._build_fields_from_editables(editables, yaml_data, errors, edit_mode)

        if isinstance(layout, list):
            content_parts = [self._render_layout_element(elem, fields_by_name, yaml_data) for elem in layout]
            inner = "\n".join(content_parts)
            return f'<c-layout-flow gap="lg">\n{inner}\n</c-layout-flow>'
        return self._render_layout_element(layout, fields_by_name, yaml_data)

    def _build_fields_from_editables(
        self,
        editables: list[EditableVisualizer],
        yaml_data: dict[str, Any],
        errors: dict[str, list[str]] | None = None,
        edit_mode: bool = False,
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

        # Inject forced values for locked_by_service fields so dependent
        # fields (depends_on) can see the forced value in yaml_data.
        for editable in editables:
            if editable.locked_by_service and _is_service_active(editable.locked_by_service, yaml_data):
                smart_set_value(yaml_data, editable.editable.yaml_path, True)

        for editable in editables:
            if not should_render_editable(editable, yaml_data):
                continue

            if editable.widget == WidgetType.GROUP:
                # Groups flatten their children into the field list
                group_fields = self._build_group_fields(editable, yaml_data, errors, edit_mode, provider_context)
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
                )
                # Pass locked services to service_cards widget
                if editable.widget == WidgetType.SERVICE_CARDS and "_locked_services" in yaml_data:
                    form_field.attributes["locked_values"] = ",".join(yaml_data["_locked_services"])
                fields_by_name[form_field.path] = form_field

        all_fields = list(fields_by_name.values())
        self._translate_fields(all_fields)
        if edit_mode:
            self._apply_edit_mode(all_fields)

        return fields_by_name

    @staticmethod
    def _build_provider_context(yaml_data: dict[str, Any]) -> dict[str, Any]:
        """Extract context from yaml_data for options providers.

        Currently extracts:
        - ``project_services``: list of enabled service names, used by
          ``FilteredServiceOptionsProvider`` to limit the component services
          checkbox group to project-enabled services.
        - ``component_names``: list of component names, used by
          ``ComponentReferenceOptionsProvider`` for deployment references.
        """
        context: dict[str, Any] = {}

        # Extract project services
        services = yaml_data.get("services", [])
        if isinstance(services, list):
            names: list[str] = []
            for svc in services:
                if isinstance(svc, str):
                    names.append(svc)
                elif isinstance(svc, dict):
                    names.extend(svc.keys())
            context["project_services"] = names

        # Extract cluster (for ClusterBaseDomainOptionsProvider)
        clusters = yaml_data.get("clusters", [])
        if isinstance(clusters, list) and clusters:
            context["cluster"] = clusters[0] if isinstance(clusters[0], str) else ""
        elif isinstance(clusters, str):
            context["cluster"] = clusters

        # Extract component names
        components = yaml_data.get("components", [])
        if isinstance(components, list):
            context["component_names"] = [
                c.get("name", "") for c in components if isinstance(c, dict) and c.get("name")
            ]

        # Extract base_domain from deployments (for DomainFormatOptionsProvider)
        # Check all deployments — in the edit flow, the edited deployment may not be [0]
        deployments = yaml_data.get("deployments", [])
        if isinstance(deployments, list):
            for dep in deployments:
                if isinstance(dep, dict):
                    base_domain = dep.get("base-domain")
                    if base_domain:
                        context["base_domain"] = base_domain
                        break

        return context

    def _build_group_fields(
        self,
        editable: EditableVisualizer,
        yaml_data: dict[str, Any],
        errors: dict[str, list[str]],
        edit_mode: bool,
        provider_context: dict[str, Any] | None = None,
    ) -> dict[str, FormField]:
        """Flatten a group editable's children into individual form fields.

        Groups are transparent containers — they don't render themselves,
        they just let their children participate directly in the field list.
        Enforcer errors from the group are attached to the parent path.
        """
        from opi.forms.visualizers.bridge import editable_to_form_field, should_render_editable

        fields: dict[str, FormField] = {}
        for child in editable.children or []:
            if not should_render_editable(child, yaml_data):
                continue
            if child.widget == WidgetType.GROUP:
                fields.update(self._build_group_fields(child, yaml_data, errors, edit_mode, provider_context))
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
        """Build a FormField for a sequence editable with item children."""
        from opi.forms.editables.service_path import smart_get_value
        from opi.forms.visualizers.bridge import editable_to_form_field, should_render_editable

        ed = editable.editable
        items = smart_get_value(yaml_data, ed.yaml_path) or []
        if not isinstance(items, list):
            items = []

        # Ensure min_items are present so the UI shows at least that many rows
        while len(items) < ed.min_items:
            items.append({})

        seq_field = FormField(
            name=ed.yaml_path,
            path=ed.yaml_path,
            schema_type=list,
            widget_type="sequence",
            label=editable.label or "",
            description=editable.description,
            min_items=ed.min_items,
            max_items=ed.max_items,
        )

        # Detect per-item reference exclusion for ComponentReferenceOptionsProvider
        ref_field_name, used_refs_by_index = self._detect_reference_exclusions(editable, items)

        children: list[FormField] = []
        for index in range(len(items)):
            # Compute per-item provider context with reference exclusions
            item_context = provider_context
            if ref_field_name and used_refs_by_index:
                other_refs = [r for i, r in used_refs_by_index.items() if i != index]
                item_context = {**(provider_context or {}), "exclude_references": other_refs}

            item_children: list[FormField] = []
            for child_editable in editable.children or []:
                if not should_render_editable(child_editable, yaml_data):
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
                    )
                    item_children.append(child_field)

            item_field = FormField(
                name=f"{ed.yaml_path}[{index}]",
                path=f"{ed.yaml_path}[{index}]",
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
        """Detect if a sequence has a ComponentReferenceOptionsProvider child.

        If found, collects the used reference values per item index so the
        provider can exclude already-selected references from other items.

        Returns:
            Tuple of (ref_field_name, {index: reference_value}).
            Both are empty/None when no reference provider is found.
        """
        ref_field_name: str | None = None
        for child in editable.children or []:
            if child.editable.values_provider == "ComponentReferenceOptionsProvider":
                # Extract the leaf field name from the yaml_path
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
        from opi.forms.editables.path import resolve_path
        from opi.forms.editables.service_path import smart_get_value
        from opi.forms.visualizers.bridge import editable_to_form_field

        ed = editable.editable
        concrete_path = resolve_path(ed.yaml_path, parent_index)
        items = smart_get_value(yaml_data, concrete_path) or []
        if not isinstance(items, list):
            items = []

        # Ensure min_items are present so the UI shows at least that many rows
        while len(items) < ed.min_items:
            items.append({})

        nested_field = FormField(
            name=concrete_path,
            path=concrete_path,
            schema_type=list,
            widget_type="sequence",
            label=editable.label or "",
            min_items=ed.min_items,
            max_items=ed.max_items,
        )

        children: list[FormField] = []
        for child_index in range(len(items)):
            item_children: list[FormField] = []
            for child_editable in editable.children or []:
                # Pre-resolve the parent [*] so editable_to_form_field
                # only needs to resolve the child [*].
                resolved_editable = copy.copy(child_editable)
                resolved_ed = copy.copy(child_editable.editable)
                resolved_ed.yaml_path = resolve_path(child_editable.editable.yaml_path, parent_index)
                resolved_editable.editable = resolved_ed
                child_field = editable_to_form_field(
                    resolved_editable,
                    yaml_data,
                    errors,
                    index=child_index,
                    edit_mode=edit_mode,
                    provider_context=provider_context,
                )
                item_children.append(child_field)

            item_field = FormField(
                name=f"{concrete_path}[{child_index}]",
                path=f"{concrete_path}[{child_index}]",
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
            # Check if field has an options_provider attribute
            provider_name = field.attributes.get("options_provider")
            if provider_name and provider_name in self.providers:
                field.options = self.providers[provider_name].get_options()

            # Recursively process children
            if field.children:
                self._resolve_options(field.children)

    def _translate_fields(self, fields: list[FormField]) -> None:
        """Translate field labels and descriptions."""
        for field in fields:
            # Translate label if it looks like an i18n key
            if field.label and "." in field.label:
                field.label = self.translator(field.label)

            # Translate description
            if field.description and "." in field.description:
                field.description = self.translator(field.description)

            # Translate errors
            field.errors = [self.translator(e) for e in field.errors]

            # Recursively translate children
            if field.children:
                self._translate_fields(field.children)

    def _apply_edit_mode(self, fields: list[FormField]) -> None:
        """Apply edit mode constraints to fields.

        Sets readonly=True for fields marked with readonly_on_edit=True.
        Sets edit_mode=True on all fields so widgets can adapt.
        """
        for field in fields:
            field.edit_mode = True
            if field.readonly_on_edit:
                field.readonly = True

            # Recursively apply to children
            if field.children:
                self._apply_edit_mode(field.children)

    def _generate_default_layout(self, fields: list[FormField]) -> list[LayoutElement | str]:
        """Generate a simple default layout from fields."""
        # Just return field names for simple vertical layout
        return [f.name for f in fields]

    def _render_layout_element(
        self,
        element: LayoutElement | str,
        fields: dict[str, FormField],
        yaml_data: dict[str, Any] | None = None,
    ) -> str:
        """
        Render a layout element or field reference.

        Args:
            element: Layout element or field name string
            fields: Field lookup by name
            yaml_data: Current YAML data (needed by DisplayBlock)

        Returns:
            Rendered HTML
        """
        # String = field name reference
        if isinstance(element, str):
            field = fields.get(element)
            if field:
                return self.adapter.render_field(field)
            return ""

        # Row layout
        if isinstance(element, Row):
            children_html = [self._render_layout_element(child, fields, yaml_data) for child in element.children]
            return self.adapter.render_row(element, children_html)

        # Column layout
        if isinstance(element, Column):
            child_html = self._render_layout_element(element.child, fields, yaml_data)
            return self.adapter.render_column(element, child_html)

        # Fieldset layout
        if isinstance(element, Fieldset):
            children_html = [self._render_layout_element(child, fields, yaml_data) for child in element.children]
            return self.adapter.render_fieldset(element, children_html)

        # Div wrapper
        if isinstance(element, Div):
            children_html = [self._render_layout_element(child, fields, yaml_data) for child in element.children]
            return self.adapter.render_div(element, children_html)

        # Sequence (repeatable fields)
        if isinstance(element, Sequence):
            field = fields.get(element.field_name)
            if field and field.children:
                items_html = []
                for i, child_field in enumerate(field.children):
                    if element.child_layout:
                        # Build relative fields dict for this item
                        item_prefix = f"{element.field_name}[{i}]/"
                        relative_fields: dict[str, FormField] = {}
                        for cf in child_field.children:
                            rel_key = cf.path.removeprefix(item_prefix)
                            relative_fields[rel_key] = cf

                        # Render using child_layout
                        if isinstance(element.child_layout, list):
                            parts = [self._render_layout_element(el, relative_fields) for el in element.child_layout]
                            item_html = "\n".join(parts)
                        else:
                            item_html = self._render_layout_element(element.child_layout, relative_fields)
                    else:
                        # Default: render all children flat
                        child_fields_html = [self.adapter.render_field(cf) for cf in child_field.children]
                        item_html = "\n".join(child_fields_html)
                    items_html.append(self.adapter.render_sequence_item(field, i, item_html))
                return self.adapter.render_sequence(field, items_html)
            elif field:
                return self.adapter.render_sequence(field, [])
            return f"<!-- Unknown sequence field: {element.field_name} -->"

        # Hidden field
        if isinstance(element, Hidden):
            field = fields.get(element.field_name)
            if field:
                return self.adapter.render_hidden(field)
            return f"<!-- Unknown hidden field: {element.field_name} -->"

        # Raw HTML
        if isinstance(element, HTML):
            return element.content

        # Template partial
        if isinstance(element, TemplatePartial):
            from opi.core.templates import get_templates

            tmpl = get_templates().get_template(element.template)
            ctx = {**(yaml_data or {}), **element.context}
            return tmpl.render(ctx)

        # Display block (server-rendered via HTMX)
        if isinstance(element, DisplayBlock):
            from opi.core.templates import get_templates

            context = element.compute(yaml_data or {})
            tmpl = get_templates().get_template(element.template)
            inner = tmpl.render(context)
            return f'<div id="display-{element.display_id}">{inner}</div>'

        # Submit button
        if isinstance(element, Submit):
            return self.adapter.render_submit(element)

        # Button group
        if isinstance(element, ButtonGroup):
            buttons_html = [self._render_layout_element(btn, fields) for btn in element.buttons]
            return self.adapter.render_button_group(element, buttons_html)

        # Unknown element type
        return f"<!-- Unknown layout element: {type(element).__name__} -->"

    def _format_validation_errors(self, error: ValidationError) -> dict[str, list[str]]:
        """Format Pydantic validation errors to field-keyed dict."""
        errors: dict[str, list[str]] = {}

        for err in error.errors():
            # Build field path from location
            loc = err.get("loc", ())
            path = ".".join(str(part) for part in loc)

            # Get error message
            msg = err.get("msg", "Validation error")

            if path not in errors:
                errors[path] = []
            errors[path].append(msg)

        return errors
