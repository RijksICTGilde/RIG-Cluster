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

from typing import Any

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
