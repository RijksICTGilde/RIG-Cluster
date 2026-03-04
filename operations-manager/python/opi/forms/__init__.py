"""
Dynamic form generation system for OPI.

This module provides a schema-driven approach to form generation,
inspired by TAD's Editable pattern and Django Crispy Forms layout system.

Key components:
- FormMeta: Pydantic annotation for form field metadata
- FormField: Resolved field ready for rendering
- Layout elements: Row, Column, Fieldset, Sequence
- WidgetAdapter: Abstract UI rendering layer
- FormRenderer: Orchestrates form rendering

Example usage:
    from pydantic import BaseModel, Field
    from typing import Annotated
    from opi.forms import FormMeta, Row, Column, Fieldset, FormRenderer
    from opi.forms.widgets import ROOSWidgetAdapter

    class MyForm(BaseModel):
        name: Annotated[str, FormMeta(
            label="form.name",
            widget="text"
        )] = Field(min_length=1)

    layout = Fieldset(
        legend="form.details",
        children=[
            Row([
                Column("name", width=6),
                Column("email", width=6),
            ])
        ]
    )

    renderer = FormRenderer(ROOSWidgetAdapter())
    html = renderer.render(MyForm, layout)
"""

from opi.forms.converters import (
    AGEEncryptedConverter,
    BooleanConverter,
    IdentityConverter,
    IntegerConverter,
    KeyValueConverter,
    ListConverter,
    StringConverter,
    YAMLConverter,
    get_converter,
)
from opi.forms.extractor import (
    extract_fields_from_model,
    extract_single_field,
    group_fields_by_section,
)
from opi.forms.field import Converter, FormField, Validator
from opi.forms.hooks import (
    DependencyEnforcerHook,
    EnforcerHook,
    FormHook,
    FormProcessor,
    FormState,
    PatternValidatorHook,
    RangeValidatorHook,
    RequiredValidatorHook,
    SlugEnforcerHook,
    UniqueValidatorHook,
    ValidatorHook,
)
from opi.forms.i18n import (
    DictTranslator,
    FormTranslator,
    IdentityTranslator,
    create_translator,
    get_default_nl_translator,
)
from opi.forms.layout import (
    HTML,
    ButtonGroup,
    Column,
    Div,
    Fieldset,
    Hidden,
    LayoutElement,
    Row,
    Sequence,
    Submit,
)

# Models (OPI-specific form schemas)
from opi.forms.models import (
    ComponentFormModel,
    ProjectFormModel,
    UserFormModel,
    get_project_form_layout,
)
from opi.forms.renderer import FormRenderer, OptionsProvider, Translator
from opi.forms.schema import FormMeta, get_form_meta, infer_widget_type
from opi.forms.visualizers.providers import (
    ClusterOptionsProvider,
    ComponentTypeOptionsProvider,
    CpuLimitOptionsProvider,
    CpuRequestOptionsProvider,
    DomainModeOptionsProvider,
    MemoryLimitOptionsProvider,
    MemoryRequestOptionsProvider,
    ServiceOptionsProvider,
    UserRoleOptionsProvider,
    get_all_providers,
    get_provider,
)
from opi.forms.widgets import ROOSWidgetAdapter, WidgetAdapter

__all__ = [
    "HTML",
    "AGEEncryptedConverter",
    "BooleanConverter",
    "ButtonGroup",
    # Providers
    "ClusterOptionsProvider",
    "Column",
    "ComponentFormModel",
    "ComponentTypeOptionsProvider",
    "Converter",
    "CpuLimitOptionsProvider",
    "CpuRequestOptionsProvider",
    "DependencyEnforcerHook",
    "DictTranslator",
    "Div",
    "DomainModeOptionsProvider",
    "EnforcerHook",
    "Fieldset",
    # Field
    "FormField",
    "FormHook",
    # Schema
    "FormMeta",
    "FormProcessor",
    # Renderer
    "FormRenderer",
    # Hooks
    "FormState",
    "FormTranslator",
    "Hidden",
    # Converters
    "IdentityConverter",
    # I18n
    "IdentityTranslator",
    "IntegerConverter",
    "KeyValueConverter",
    "LayoutElement",
    "ListConverter",
    "MemoryLimitOptionsProvider",
    "MemoryRequestOptionsProvider",
    "OptionsProvider",
    "PatternValidatorHook",
    # Models
    "ProjectFormModel",
    "ROOSWidgetAdapter",
    "RangeValidatorHook",
    "RequiredValidatorHook",
    # Layout
    "Row",
    "Sequence",
    "ServiceOptionsProvider",
    "SlugEnforcerHook",
    "StringConverter",
    "Submit",
    "Translator",
    "UniqueValidatorHook",
    "UserFormModel",
    "UserRoleOptionsProvider",
    "Validator",
    "ValidatorHook",
    # Widgets
    "WidgetAdapter",
    "YAMLConverter",
    "create_translator",
    # Extractor
    "extract_fields_from_model",
    "extract_single_field",
    "get_all_providers",
    "get_converter",
    "get_default_nl_translator",
    "get_form_meta",
    "get_project_form_layout",
    "get_provider",
    "group_fields_by_section",
    "infer_widget_type",
]
