"""Editable definitions for user admin CRUD forms."""

from opi.forms.editables.editable import Editable, WidgetType
from opi.forms.editables.validators import EmailValidator, MinMaxLengthValidator
from opi.forms.layout import Column, Fieldset, Row
from opi.forms.visualizers.sections import FormSection
from opi.forms.visualizers.visualizer import EditableVisualizer

# --- Editables (data logic) ---

USER_EMAIL_EDITABLE = Editable(
    yaml_path="email",
    required=True,
    validator=EmailValidator(),
)

USER_FULL_NAME_EDITABLE = Editable(
    yaml_path="full_name",
    required=True,
    validator=MinMaxLengthValidator(min_length=2, max_length=200),
)

# --- Visualizers (UI) ---

USER_EMAIL_VISUALIZER = EditableVisualizer(
    editable=USER_EMAIL_EDITABLE,
    widget=WidgetType.TEXT,
    label="E-mailadres",
    placeholder="gebruiker@example.nl",
)

USER_FULL_NAME_VISUALIZER = EditableVisualizer(
    editable=USER_FULL_NAME_EDITABLE,
    widget=WidgetType.TEXT,
    label="Volledige naam",
    placeholder="Jan de Vries",
)

# --- Section ---

USER_SECTION = FormSection(
    section_id="user-details",
    title="Gebruiker",
    editables=[USER_EMAIL_VISUALIZER, USER_FULL_NAME_VISUALIZER],
    layout=Fieldset(
        legend="Gebruiker",
        children=[
            Row(
                children=[
                    Column("email", width=6),
                    Column("full_name", width=6),
                ]
            ),
        ],
    ),
)
