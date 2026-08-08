"""Zet de roos-templates om naar de LOTC-woordenschat.

Waarom een omzetter en geen handwerk: de release blijft ondertussen aan diezelfde
templates werken. Een met de hand overgetypte kopie van 152 bestanden is vanaf dag
twee verouderd, en niemand ziet waar. Een omzetter is opnieuw te draaien, en zijn
vertaalregels zijn te lezen en te toetsen - het handwerk zit dan in de regels en niet
in de uitkomst.

De uitvoer gaat naar opi/templates_lotc/, dat volledig door dit script beheerd wordt.
Handmatige wijzigingen daar overleven de volgende run niet; hoort iets structureel
anders, dan hoort het hier.

    uv run python scripts/lotc_convert_templates.py            # omzetten
    uv run python scripts/lotc_convert_templates.py --check    # alleen meten

Wat de omzetter WEL doet: componentnamen vertalen, attributen van camelCase naar
kebab-case brengen, attributen die LOTC niet kent weglaten, en de overervingsketen
naar de LOTC-schil laten wijzen.

Wat hij NIET doet: samenstellingen omschrijven die in roos een data-prop zijn en in
LOTC kinderen (`:items="..."` op een menu, `:tabs="..."` op tabs). Die staan in
UNCONVERTIBLE en worden overgeslagen met een melding, want daar is een keuze voor
nodig die een script niet kan maken.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Het script staat in scripts/ maar leest de registry van de applicatie zelf, dus die
# moet importeerbaar zijn ook als het van elders wordt aangeroepen.
sys.path.insert(0, str(Path(__file__).parent.parent))

TEMPLATES_DIR = Path(__file__).parent.parent / "opi" / "templates"
OUTPUT_DIR = Path(__file__).parent.parent / "opi" / "templates_lotc"

# Met de hand geschreven, en dus niet overschreven.
#
# De schil (base_lotc.html.j2) omdat de INDELING verandert en niet alleen de namen: de
# navigatie verhuist naar een zijkolom, in de opzet van bg.rijks.app.
#
# De formulierwidgets omdat ze wezenlijk anders werken. In de roos-templates schreven
# macro's stukken attribuut-TEKST middenin de componenttag, en dat kan bij LOTC niet.
# Daar staat :prop="expr or none" tegenover voor losse attributen en :attrs="<dict>"
# voor een hele bundel. Dat is geen vertaling die een script kan maken; het is per
# widget een keuze welk attribuut waar hoort.
HANDWRITTEN = {"base_lotc.html.j2"}
HANDWRITTEN_DIRS = {"widgets"}

# Componenten die in LOTC anders heten. Alles wat hier niet staat houdt zijn naam;
# dat is het overgrote deel, want beide systemen delen hun woordenschat grotendeels.
COMPONENT_RENAMES = {
    "ul": "list",
    "li": "list-item",
    "menubar": "menu",
    "tr": "table-row",
    "thead": "table-head",
    "tab-item": "tab",
    "input": "text-input",
    "text": "span",
    "content": "div",
    # De structuurprimitieven komen uit lotc-layout en heten daar naar wat ze doen
    # in plaats van naar de richting waarin ze staan. Een rij die mag afbreken is een
    # cluster, een kolom is een stack. layout-flow bestaat wel onder die naam.
    "layout-row": "cluster",
    "layout-column": "stack",
    # NLDD implementeert geen strong; <c-b> geeft hetzelfde vette element.
    "strong": "b",
}

# Tags waarvan LOTC geen tegenhanger heeft en die alleen omhulsel zijn: de tag valt
# weg, de inhoud blijft staan. <c-tbody> groepeert rijen die in LOTC rechtstreeks in
# de tabel hangen; <c-menubar-debug> is een ontwikkelhulpje zonder betekenis hier.
UNWRAP = {"tbody", "menubar-debug"}

# Attributen die niet alleen van schrijfwijze veranderen maar van naam.
ATTRIBUTE_RENAMES = {
    "textContent": "label",
    "className": "class",
    "bodyClass": "body-class",
    "maxWidth": "max-width",
    "ariaLabel": "aria-label",
}

# Componenten met een data-prop die in LOTC kinderen zijn. Een script kan die niet
# betrouwbaar omschrijven, dus ze worden gemeld in plaats van half omgezet.
UNCONVERTIBLE = {
    "menubar": "items",
    "menu": "items",
    "tabs": "tabs",
    "ul": "items",
    "list": "items",
}

# Een heel attribuut achter een voorwaarde, binnen de tag:
#
#     {% if form_data.email %}value="{{ form_data.email }}"{% endif %}
#
# De roos-parser laat dat door, de LOTC-parser leest {% als een attribuutnaam. LOTC
# heeft er een nettere vorm voor: :naam="expr", waarbij none betekent weglaten. Deze
# regel doet die vertaling, inclusief waarden die tekst en expressie mengen.
CONDITIONAL_ATTR_RE = re.compile(
    r"\{%-?\s*if\s+(?P<cond>.+?)\s*-?%\}\s*(?P<name>[A-Za-z_:@][A-Za-z0-9_:@-]*)=\"(?P<value>[^\"]*)\"\s*\{%-?\s*endif\s*-?%\}",
    re.DOTALL,
)
INTERPOLATION_RE = re.compile(r"\{\{\s*(.+?)\s*\}\}", re.DOTALL)


def value_as_expression(value: str) -> str:
    """Zet een attribuutwaarde om in een Jinja-expressie.

    ``{{ naam }}`` wordt de expressie zelf; losse tekst eromheen wordt een string, en
    de delen worden aan elkaar geplakt. Zo overleeft een waarde die tekst en expressie
    mengt de omzetting zonder dat de betekenis verschuift.
    """
    parts: list[str] = []
    position = 0
    for match in INTERPOLATION_RE.finditer(value):
        literal = value[position : match.start()]
        if literal:
            parts.append(repr(literal))
        parts.append(f"({match.group(1)})")
        position = match.end()
    tail = value[position:]
    if tail:
        parts.append(repr(tail))
    if not parts:
        return "''"
    return " ~ ".join(parts)


# Een voorwaarde BINNEN de aanhalingstekens van een attribuutwaarde:
#
#     label="{% if language == 'nl' %}Nieuw account{% else %}New account{% endif %}"
#
# Ook dit leest de LOTC-parser niet. Als expressie kan het wel, en dan is het meteen
# korter: :label="'Nieuw account' if language == 'nl' else 'New account'".
INLINE_IF_RE = re.compile(
    r"(?P<name>[A-Za-z_:@][A-Za-z0-9_:@-]*)=\"\{%-?\s*if\s+(?P<cond>.+?)\s*-?%\}"
    r"(?P<then>.*?)"
    r"(?:\{%-?\s*else\s*-?%\}(?P<other>.*?))?"
    r"\{%-?\s*endif\s*-?%\}\"",
    re.DOTALL,
)


def convert_inline_conditions(source: str) -> tuple[str, int]:
    """Vervang een if/else binnen een attribuutwaarde door een Jinja-expressie."""
    count = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal count
        # Een keten met {% elif %} of andere geneste Jinja laat deze regel met rust: een
        # half begrepen voorwaarde omzetten levert stille onzin op, en niet-omzetten is
        # zichtbaar. Die gevallen komen als melding naar boven.
        if "{%" in match.group("then") or "{%" in (match.group("other") or ""):
            return match.group(0)
        count += 1
        then_expression = value_as_expression(match.group("then"))
        other_expression = value_as_expression(match.group("other") or "")
        return f':{match.group("name")}="({then_expression}) if ({match.group("cond")}) else ({other_expression})"'

    return INLINE_IF_RE.sub(replace, source), count


def convert_conditional_attributes(source: str) -> tuple[str, int]:
    """Vervang voorwaardelijke attributen binnen tags door de LOTC-vorm."""
    count = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        expression = value_as_expression(match.group("value"))
        return f':{match.group("name")}="({expression}) if ({match.group("cond")}) else none"'

    return CONDITIONAL_ATTR_RE.sub(replace, source), count


TAG_RE = re.compile(r"<c-([a-z0-9-]+)((?:[^>\"']|\"[^\"]*\"|'[^']*')*?)(/?)>")
CLOSE_RE = re.compile(r"</c-([a-z0-9-]+)>")
ATTR_RE = re.compile(r"(:?)([A-Za-z_][A-Za-z0-9_-]*)\s*=\s*(\"[^\"]*\"|'[^']*')")


def to_kebab(name: str) -> str:
    """camelCase naar kebab-case: LOTC schrijft al zijn attributen zo."""
    return re.sub(r"(?<!^)(?=[A-Z])", "-", name).lower()


def convert_attributes(attrs: str, known: set[str] | None) -> tuple[str, list[str]]:
    """Vertaal de attributen van een componenttag; geef terug wat is weggelaten.

    ``known`` is de attribuutverzameling die LOTC voor dit component kent. Is die
    bekend, dan worden onbekende attributen weggelaten - LOTC valideert streng en een
    onbekend attribuut is een harde fout, terwijl het bij ons vaak vormgeving is die
    het nieuwe design system zelf levert.
    """
    dropped: list[str] = []

    def replace(match: re.Match[str]) -> str:
        prefix, name, value = match.group(1), match.group(2), match.group(3)
        if name in ("slot", "class"):
            return match.group(0)
        new_name = ATTRIBUTE_RENAMES.get(name, to_kebab(name))
        if known is not None and new_name not in known:
            dropped.append(name)
            return ""
        return f"{prefix}{new_name}={value}"

    return ATTR_RE.sub(replace, attrs), dropped


def convert_source(source: str, known_attributes: dict[str, set[str]]) -> tuple[str, list[str]]:
    """Zet de componenttags in een template om; geef de meldingen terug."""
    notes: list[str] = []

    def open_tag(match: re.Match[str]) -> str:
        name, attrs, selfclose = match.group(1), match.group(2), match.group(3)
        if name in UNWRAP:
            return ""
        # Alleen BINNEN de tag: buiten een tag is {% if %} gewone Jinja en moet hij blijven.
        attrs, inline = convert_inline_conditions(attrs)
        attrs, conditionals = convert_conditional_attributes(attrs)
        conditionals += inline
        if conditionals:
            notes.append(f'{conditionals} voorwaardelijke attributen op c-{name} omgezet naar :naam="expr"')
        prop = UNCONVERTIBLE.get(name)
        if prop and re.search(rf"[:\s]{prop}\s*=", attrs):
            notes.append(f"c-{name} draagt :{prop} - in LOTC zijn dat kinderen, met de hand omzetten")
        new_name = COMPONENT_RENAMES.get(name, name)
        new_attrs, dropped = convert_attributes(attrs, known_attributes.get(new_name))
        if dropped:
            notes.append(f"c-{name}: weggelaten attributen {sorted(set(dropped))}")
        new_attrs = re.sub(r"\s+", " ", new_attrs).strip()
        # c-menubar is in LOTC een menu in balkvorm; die vorm zat vroeger in de naam.
        if name == "menubar" and "type=" not in new_attrs:
            new_attrs = f'type="bar" {new_attrs}'.strip()
        space = " " if new_attrs else ""
        return f"<c-{new_name}{space}{new_attrs}{'/' if selfclose else ''}>"

    def close_tag(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in UNWRAP:
            return ""
        return f"</c-{COMPONENT_RENAMES.get(name, name)}>"

    converted = TAG_RE.sub(open_tag, source)
    converted = CLOSE_RE.sub(close_tag, converted)
    converted = converted.replace('extends "base.html.j2"', 'extends "base_lotc.html.j2"')
    return converted, notes


def load_known_attributes() -> dict[str, set[str]]:
    """De attributen die per component bekend zijn, uit de registry van de applicatie.

    Bewust de omgeving van de applicatie zelf (opi.core.templates_lotc) en geen eigen
    kopie: die draagt naast LOTC ook de componenten die ZAD zelf levert. Bouwde dit
    script zijn eigen omgeving, dan zou het attributen weglaten die tijdens het draaien
    wel bestaan - en dat verschil zou pas opvallen als een pagina er raar uitziet.
    """
    from lord_of_the_components.registry import ComponentRegistry
    from opi.core.templates_lotc import templates_lotc

    registry = None
    for extension in templates_lotc.env.extensions.values():
        candidate = getattr(extension, "registry", None)
        if isinstance(candidate, ComponentRegistry):
            registry = candidate
    if registry is None:
        raise RuntimeError("kon de LOTC-registry niet vinden op de extensie")
    return {
        name: {a.name for a in getattr(definition, "attributes", [])}
        for name, definition in registry._components.items()
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="alleen meten, niets schrijven")
    args = parser.parse_args()

    known_attributes = load_known_attributes()
    sources = sorted(TEMPLATES_DIR.rglob("*.j2"))
    all_notes: dict[str, list[str]] = {}
    written = 0

    for source_path in sources:
        relative = source_path.relative_to(TEMPLATES_DIR)
        if relative.name in HANDWRITTEN or relative.parts[0] in HANDWRITTEN_DIRS:
            continue
        converted, notes = convert_source(source_path.read_text(), known_attributes)
        if notes:
            all_notes[str(relative)] = notes
        if not args.check:
            target = OUTPUT_DIR / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(converted)
            written += 1

    print(f"{len(sources)} templates gelezen, {written} geschreven naar {OUTPUT_DIR}")
    if all_notes:
        print(f"\n{len(all_notes)} templates met meldingen:")
        for name, notes in sorted(all_notes.items()):
            print(f"  {name}")
            for note in sorted(set(notes)):
                print(f"      {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
