"""De attribuutbundel van een formulierveld, als dict voor de LOTC-spread.

Onze widgets dragen twee bundels attributen die pas tijdens het renderen bekend zijn:
``field.htmx_attrs`` (hx-get, hx-target, hx-trigger, ...) en ``field.attributes``
(losse data-, aria- en HTML-attributen). In de roos-templates werden die uitgeschreven
door macro's die een stuk attribuut-TEKST teruggaven, midden in de componenttag:

    <c-text-input-field id="..." {{ htmx_attrs(field) }} {{ extra_attrs(field) }} />

Dat kan bij LOTC niet: die leest de haakjes als een attribuutnaam. LOTC heeft daar
``:attrs="<dict>"`` voor, dat een platte dict op het invoerveld merget. Deze module
levert die dict.

Beide bundels zijn aan onze kant al dicts, dus dit is een samenvoeging en geen
omzetting. Het enige echte werk is de overslaan-lijst: ``field.attributes`` draagt naast
HTML-attributen ook velddefinitie-instellingen (welke converter, welke validator, hoeveel
rijen) en die horen niet in de HTML terecht te komen.
"""

import re
from typing import Any

from markupsafe import Markup


def attr_escape(value: object) -> str:
    """Escape een waarde voor gebruik binnen een dubbel aangehaald HTML-attribuut.

    Anders dan het ingebouwde ``|e``-filter van Jinja2 escapet dit GEEN enkele
    aanhalingstekens. Web-componenten lezen een attribuutwaarde als platte tekst, dus
    ``&#39;`` zou letterlijk op het scherm komen; onze attributen staan altijd tussen
    dubbele aanhalingstekens, dus ``'`` escapen is niet nodig.

    Staat hier en niet bij de widgets omdat de templateomgeving hem als filter
    registreert: importeren uit ``opi.forms.widgets`` zou die omgeving via het
    ``__init__`` van dat pakket naar zichzelf laten terugwijzen.
    """
    tekst = str(value)
    return tekst.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


# Sleutels in field.attributes die de velddefinitie beschrijven en geen HTML-attribuut
# zijn. Overgenomen uit de extra_attrs-macro in templates/widgets/_macros.html.j2, die
# tot nu toe dezelfde scheiding maakte. Wijzigt die lijst, dan hoort hij hier mee te
# wijzigen - vandaar de test die beide naast elkaar legt.
NON_HTML_ATTRIBUTES = frozenset(
    {
        "rows",
        "min",
        "max",
        "step",
        "accept",
        "multiple",
        "min_items",
        "max_items",
        "add_label",
        "remove_label",
        "options_provider",
        "converter",
        "validator",
        "kv_format",
        "icon",
        "icon_color",
    }
)


def field_attrs(field: Any, handled: list[str] | None = None) -> dict[str, Any]:
    """Alle losse attributen van een veld als een platte dict voor ``:attrs``.

    Args:
        field: het veld dat gerenderd wordt.
        handled: sleutels die dit widget zelf al als expliciet attribuut zet en die
            dus niet nog eens in de bundel moeten. Standaard de niet-HTML-sleutels.

    Returns:
        Een dict van attribuutnaam naar waarde. LOTC laat een waarde die ``None`` of
        leeg is zelf weg, dus dat hoeft hier niet gefilterd te worden - en dat is maar
        goed ook, want een lege string is voor sommige aria-attributen betekenisvol.
    """
    skip = NON_HTML_ATTRIBUTES if handled is None else frozenset(handled)
    merged: dict[str, Any] = dict(getattr(field, "htmx_attrs", None) or {})
    merged.update({key: value for key, value in (getattr(field, "attributes", None) or {}).items() if key not in skip})
    return merged


#: Het ``error-message-ids``-attribuut zoals lotc-forms het op het invoerveld schrijft.
_FOUT_IDS = re.compile(r'\s+error-message-ids="[^"]*"')

#: De opening van het eerste element in een stuk HTML: ``<nldd-text-field ...>``.
_EERSTE_TAG = re.compile(r"<([a-zA-Z][-\w]*)((?:[^>\"']|\"[^\"]*\"|'[^']*')*?)(/?)>")

#: ``invalid`` als los attribuut in een tag, en niet als deel van een langere naam.
_INVALID = re.compile(r"(?:^|\s)invalid(?:\s|=|$)")


def bedraad_foutmelding(control: object, error_id: str) -> Markup:
    """Zet op het invoerveld wat ``nldd-form-field`` nodig heeft om de fout te TONEN.

    Gemeten in een browser, op de markup die lotc-forms zelf oplevert: de foutregel staat
    er wel (``<nldd-form-field-error-text ... invalid>`` met de juiste tekst) en is toch
    ``display: none`` met hoogte 0. De oorzaak zit in ``nldd-form-field`` zelf. Dat
    component synchroniseert bij elke wijziging zijn foutregels::

        const i = veld.hasAttribute("invalid")
        const o = (veld.getAttribute("error-message") ?? "").split(" ")
        for (const regel of foutregels) regel.toggleAttribute("invalid", i && o.includes(regel.id))

    Het leest dus ``error-message`` OP HET INVOERVELD, en het zet zijn eigen ``invalid``
    op elke foutregel - de ``invalid`` die het sjabloon daar schrijft wordt meteen
    overschreven. ``error-message-ids`` is de ANDERE kant op: dat is de eigenschap die
    ``nldd-form-field`` zelf op het veld ZET om ``aria-describedby`` te bedraden.
    lotc-forms schrijft daarin, en dus komt er nooit een id in de lijst die de zichtbare
    fouten bepaalt.

    Wat deze functie doet is daarom een naamsverbetering en geen truc: het
    ``error-message-ids`` dat het sjabloon schreef gaat eraf (het component vult hem
    zelf), en het veld krijgt ``invalid`` plus ``error-message="<id van de foutregel>"``.
    Gemeten resultaat: de foutregel is ``display: block`` met hoogte 18, het invoerveld
    heeft ``aria-invalid="true"`` en ``aria-describedby`` wijst naar de foutregel.

    ``aria-invalid`` gaat er ook op, als het er nog niet staat. Bij een veld dat zelf een
    invoerelement rendert zet het component die al op het element BINNEN zijn schaduwboom
    (daarom vindt een meting in de gewone DOM er nul); bij de groepsvelden - de ``<div
    role="radiogroup">`` van radio en de groep van de aankruisvakjes - staat hij nergens,
    en daar is dit het enige dat een schermlezer over de fout vertelt.

    Dit hoort in het thema thuis en niet hier; het staat als bevinding in
    ``request_for_components.md``. Zolang het daar niet gerepareerd is, is DIT de ene
    plek: elk veldsjabloon van lotc-forms loopt via de macro die deze functie aanroept.

    Args:
        control: de gerenderde HTML van het invoerveld (de ``{% call %}``-body).
        error_id: het id van het ``nldd-form-field-error-text``-element eronder.

    Returns:
        Dezelfde HTML, met het eerste element bedraad. Zonder eerste element (leeg of
        alleen tekst) komt de invoer ongewijzigd terug.
    """
    html = str(control)
    html = _FOUT_IDS.sub("", html)
    treffer = _EERSTE_TAG.search(html)
    if treffer is None:
        return Markup(html)  # noqa: S704 - al gerenderde, geescapete markup
    naam, attributen, sluiting = treffer.groups()
    extra = "" if _INVALID.search(attributen) else " invalid"
    if "aria-invalid" not in attributen:
        extra += ' aria-invalid="true"'
    bedraad = f'<{naam}{attributen}{extra} error-message="{attr_escape(error_id)}"{sluiting}>'
    return Markup(html[: treffer.start()] + bedraad + html[treffer.end() :])  # noqa: S704
