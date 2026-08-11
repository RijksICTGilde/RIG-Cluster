"""Visualizers for the invite service (project-level config).

One EditableVisualizer per editable, with Dutch labels and help text. The ``active``
sequence renders the invitation as a card whose children are the invite fields; the
realm-roles picker is a nested sequence of selects.

Two editables exist but are deliberately NOT rendered here:

* ``restrict-domain`` -- restricts by email domain, which reads like a list of allowed
  addresses and is not that. Still enforced for files that carry it.
* ``auth-methods`` is rendered conditionally: only when the keycloak template is
  ``sso-support``, because ``sso-only`` leaves nothing to choose.
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
    # No angle brackets here. Help text is passed to ROOS as the ``helperText`` ATTRIBUTE,
    # and ROOS re-emits attribute values, so anything needing escaping is escaped twice and
    # the reader sees the entities themselves ("&lt;sleutel&gt;").
    help_text=(
        "De sleutel is het laatste deel van de uitnodigingslink, achter /invite/, en de enige "
        "toegangsdrempel. Laat leeg om een veilige, willekeurige sleutel te laten genereren."
    ),
)

INVITE_REALM_ROLE_ITEM = EditableVisualizer(
    editable=INVITE_REALM_ROLE_ITEM_EDITABLE,
    widget=WidgetType.SELECT,
    label="Rol",
    help_text=(
        "De rol wordt automatisch toegekend aan de gebruiker. Is toegangsbeperking "
        "ingeschakeld, dan is de rol nodig om te kunnen inloggen."
    ),
    # Geen "Optioneel"-badge: dit is een KIEZER waar altijd iets geselecteerd staat, en de
    # lege stand heet hier "Geen rol toekennen". Dezelfde reden als bij de URI-velden van
    # keycloak; zie GEEN_OPTIONEEL_BADGE daar.
    attributes={"data-no-optional-badge": "1"},
)

#: De hulptekst staat op het veld zelf en niet hier: het is een keuze en geen lijst meer,
#: dus twee toelichtingen boven elkaar zouden alleen ruis zijn. Dat de lege keuze "geen
#: rol" betekent staat in de optie zelf ("Geen rol toekennen").
INVITE_REALM_ROLES = EditableVisualizer(
    editable=INVITE_REALM_ROLES_EDITABLE,
    widget=WidgetType.SEQUENCE,
    label="Realm-rol",
    children=[INVITE_REALM_ROLE_ITEM],
)

INVITE_CONTACT_EMAIL = EditableVisualizer(
    editable=INVITE_CONTACT_EMAIL_EDITABLE,
    widget=WidgetType.TEXT,
    label="Contact-e-mailadres",
    help_text="Getoond aan de uitgenodigde gebruiker als contact bij problemen.",
)

INVITE_APPLICATION_URL = EditableVisualizer(
    editable=INVITE_APPLICATION_URL_EDITABLE,
    widget=WidgetType.SELECT,
    label="Naar welke applicatie",
    help_text=(
        "Waar de knop op de succespagina naartoe verwijst. De keuze gaat per deployment en "
        "component; het webadres wordt daarbij gezocht. Alleen componenten die op het web "
        "gepubliceerd zijn staan ertussen."
    ),
)

# Only rendered when the keycloak template is sso-support; under sso-only there is nothing
# to choose (see the editable). The wording says "beperken" on purpose: an invite can narrow
# what the realm offers, never widen it.
INVITE_AUTH_METHODS = EditableVisualizer(
    editable=INVITE_AUTH_METHODS_EDITABLE,
    widget=WidgetType.CHECKBOX_GROUP,
    label="Toegestane aanmeldmethoden",
    help_text=(
        "Beperk waarmee de uitgenodigde gebruiker mag inloggen. Je kunt hier alleen kiezen uit "
        "wat de Keycloak-instelling van dit project toestaat. Niets aangevinkt betekent: alles "
        "wat het project toestaat."
    ),
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

# Still a sequence underneath (the schema allows several), but pinned to exactly one item,
# so it reads as a single block rather than a list of one. See INVITE_ACTIVE_EDITABLE.
INVITE_ACTIVE = EditableVisualizer(
    editable=INVITE_ACTIVE_EDITABLE,
    widget=WidgetType.SEQUENCE,
    label="Uitnodiging",
    help_text="De uitnodigingslink is de enige toegangsdrempel; deel hem zorgvuldig.",
    children=[
        INVITE_KEY,
        INVITE_REALM_ROLES,
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
