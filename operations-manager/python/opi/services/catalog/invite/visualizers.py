"""Visualizers for the invite service (project-level config).

One EditableVisualizer per editable, with Dutch labels and help text. The ``active``
sequence renders each invitation as a card whose children are the invite fields; the
realm-roles picker is a nested sequence of selects.
"""

from __future__ import annotations

from opi.forms.editables.editable import WidgetType
from opi.forms.visualizers.visualizer import EditableVisualizer
from opi.services.catalog.invite.editables import (
    INVITE_ACTIVE_EDITABLE,
    INVITE_APPLICATION_URL_EDITABLE,
    INVITE_AUTH_METHODS_EDITABLE,
    INVITE_CONTACT_EMAIL_EDITABLE,
    INVITE_DEFAULT_LANGUAGE_EDITABLE,
    INVITE_KEY_EDITABLE,
    INVITE_MESSAGE_EN_EDITABLE,
    INVITE_MESSAGE_NL_EDITABLE,
    INVITE_REALM_ROLE_ITEM_EDITABLE,
    INVITE_REALM_ROLES_EDITABLE,
    INVITE_RESTRICT_DOMAIN_EDITABLE,
    INVITE_SUCCESS_BUTTON_EN_EDITABLE,
    INVITE_SUCCESS_BUTTON_NL_EDITABLE,
    INVITE_SUCCESS_TITLE_EN_EDITABLE,
    INVITE_SUCCESS_TITLE_NL_EDITABLE,
)

INVITE_DEFAULT_LANGUAGE = EditableVisualizer(
    editable=INVITE_DEFAULT_LANGUAGE_EDITABLE,
    widget=WidgetType.SELECT,
    label="Standaardtaal",
    help_text="De taal van de uitnodigingspagina's wanneer de browser geen voorkeur doorgeeft.",
)

INVITE_KEY = EditableVisualizer(
    editable=INVITE_KEY_EDITABLE,
    widget=WidgetType.TEXT,
    label="Sleutel",
    help_text=(
        "De sleutel is onderdeel van de uitnodigingslink (/invite/<sleutel>) en de enige "
        "toegangsdrempel. Laat leeg om een veilige, willekeurige sleutel te laten genereren."
    ),
)

INVITE_REALM_ROLE_ITEM = EditableVisualizer(
    editable=INVITE_REALM_ROLE_ITEM_EDITABLE,
    widget=WidgetType.SELECT,
    label="Rol",
)

INVITE_REALM_ROLES = EditableVisualizer(
    editable=INVITE_REALM_ROLES_EDITABLE,
    widget=WidgetType.SEQUENCE,
    label="Realm-rollen",
    help_text=(
        "De Keycloak realm-rollen die de uitgenodigde gebruiker krijgt. Laat leeg om alleen een "
        "account aan te maken zonder extra rechten."
    ),
    children=[INVITE_REALM_ROLE_ITEM],
)

INVITE_RESTRICT_DOMAIN = EditableVisualizer(
    editable=INVITE_RESTRICT_DOMAIN_EDITABLE,
    widget=WidgetType.TEXT,
    label="Domeinbeperking",
    help_text="Beperk de uitnodiging tot e-mailadressen van dit domein (bijv. rijksoverheid.nl). Leeg = geen beperking.",
)

INVITE_CONTACT_EMAIL = EditableVisualizer(
    editable=INVITE_CONTACT_EMAIL_EDITABLE,
    widget=WidgetType.TEXT,
    label="Contact-e-mailadres",
    help_text="Getoond aan de uitgenodigde gebruiker als contact bij problemen.",
)

INVITE_APPLICATION_URL = EditableVisualizer(
    editable=INVITE_APPLICATION_URL_EDITABLE,
    widget=WidgetType.TEXT,
    label="Applicatie-URL",
    help_text="Waar de knop op de succespagina naartoe verwijst.",
)

INVITE_AUTH_METHODS = EditableVisualizer(
    editable=INVITE_AUTH_METHODS_EDITABLE,
    widget=WidgetType.CHECKBOX_GROUP,
    label="Toegestane aanmeldmethoden",
    help_text="Beperk hoe de gebruiker binnenkomt. Niets aangevinkt: val terug op de projectinstellingen (beide).",
)

INVITE_MESSAGE_NL = EditableVisualizer(
    editable=INVITE_MESSAGE_NL_EDITABLE,
    widget=WidgetType.TEXTAREA,
    label="Welkomstbericht (Nederlands)",
)
INVITE_MESSAGE_EN = EditableVisualizer(
    editable=INVITE_MESSAGE_EN_EDITABLE,
    widget=WidgetType.TEXTAREA,
    label="Welkomstbericht (Engels)",
)
INVITE_SUCCESS_TITLE_NL = EditableVisualizer(
    editable=INVITE_SUCCESS_TITLE_NL_EDITABLE,
    widget=WidgetType.TEXT,
    label="Succestitel (Nederlands)",
)
INVITE_SUCCESS_TITLE_EN = EditableVisualizer(
    editable=INVITE_SUCCESS_TITLE_EN_EDITABLE,
    widget=WidgetType.TEXT,
    label="Succestitel (Engels)",
)
INVITE_SUCCESS_BUTTON_NL = EditableVisualizer(
    editable=INVITE_SUCCESS_BUTTON_NL_EDITABLE,
    widget=WidgetType.TEXT,
    label="Knoptekst (Nederlands)",
)
INVITE_SUCCESS_BUTTON_EN = EditableVisualizer(
    editable=INVITE_SUCCESS_BUTTON_EN_EDITABLE,
    widget=WidgetType.TEXT,
    label="Knoptekst (Engels)",
)

INVITE_ACTIVE = EditableVisualizer(
    editable=INVITE_ACTIVE_EDITABLE,
    widget=WidgetType.SEQUENCE,
    label="Actieve uitnodigingen",
    help_text="Elke uitnodiging heeft een eigen link. De link is de enige toegangsdrempel; deel hem zorgvuldig.",
    children=[
        INVITE_KEY,
        INVITE_REALM_ROLES,
        INVITE_RESTRICT_DOMAIN,
        INVITE_CONTACT_EMAIL,
        INVITE_APPLICATION_URL,
        INVITE_AUTH_METHODS,
        INVITE_MESSAGE_NL,
        INVITE_MESSAGE_EN,
        INVITE_SUCCESS_TITLE_NL,
        INVITE_SUCCESS_TITLE_EN,
        INVITE_SUCCESS_BUTTON_NL,
        INVITE_SUCCESS_BUTTON_EN,
    ],
)

INVITE_VISUALIZERS = [INVITE_DEFAULT_LANGUAGE, INVITE_ACTIVE]
