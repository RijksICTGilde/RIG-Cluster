"""Guard: templates blijven verplaatsbare blokken, geen lappen tekst met vormgeving erin.

De vormgeving en de indeling van het portaal gaan veranderen. Wat je dan wilt verplaatsen
zijn blokken. Deze test houdt drie dingen tegen die dat onmogelijk maken:

1. een nieuw ``style=``-attribuut in de markup - vormgeving hoort in de CSS-bestanden of
   in het component, niet in de pagina;
2. een nieuw ``<style>``-blok in een template - zelfde reden, maar dan groter;
3. een ``content``-blok boven de grens - een pagina hoort een samenstelling van benoemde
   deeltemplates te zijn, geen enkel blok van honderden regels.

De uitzonderingen staan hieronder met hun reden erbij, zodat een uitzondering zichtbaar is
en niet stilzwijgend. De getallen zijn bovengrenzen die alleen omlaag mogen: staat er een
hoger getal dan de werkelijkheid, dan faalt de test met het verzoek het te verlagen.
"""

import re

from opi.core.template_helpers import STATIC_DIR, TEMPLATES_DIR

INLINE_STYLE = re.compile(r'\bstyle="')
STYLE_BLOCK = re.compile(r"<style[\s>]")
BLOCK_START = re.compile(r"{%-?\s*block\s+(\w+)")
BLOCK_END = re.compile(r"{%-?\s*endblock")
JINJA_SYNTAX = re.compile(r"{{|{%")
HTML_TAG = re.compile(r"<[a-zA-Z][^>]*>", re.DOTALL)
CLASS_ATTRIBUTE = re.compile(r'\bclass="')
# Een Jinja-blok mag alles bevatten, ook een ``>`` (``{% if a|length > 1 %}``). Zolang die
# er in staat, kapt HTML_TAG de tag daar af en ziet de guard de rest van de attributen niet
# meer. Haal de Jinja-blokken er dus eerst uit; wat overblijft is de markup zelf.
JINJA_BLOCK = re.compile(r"{[{%#].*?[}%#]}", re.DOTALL)

# Reden voor een uitzondering die er niet hoort te zijn maar er nog wel is. Elke regel met
# deze reden is werk dat nog moet gebeuren; de test houdt alleen tegen dat er iets bij komt.
WERKLIJST = "werklijst: nog op te schonen, mag alleen korter worden"

# Een content-blok boven deze grens is geen samenstelling meer. De grens is een richtlijn
# voor de mens en een harde regel voor de test: het gaat erom dat de eenheden betekenis
# hebben, niet dat ze klein zijn.
MAX_CONTENT_BLOCK_LINES = 50

# Templates die hun content-blok niet opdelen, met de reden.
#
# Deze lijst is met RC-67 opnieuw opgebouwd: de guard las opi/templates/, en die map is er
# niet meer. Hij meet nu opi/templates_lotc/, en dat is een tweede generatie sjablonen die
# nooit langs deze eis is gekomen. De hertekende bg-paginas staan er daarom als WERKLIJST
# in - ze zijn niet goedgekeurd, ze zijn geteld, en de eis is dat het er niet meer worden.
CONTENT_BLOCK_EXCEPTIONS: dict[str, str] = {
    "architecture-overview.html.j2": (
        "1.500 regels handgeschreven markup in een enkel blok. Dit is geen pagina om op te "
        "delen maar een om apart te beoordelen, en misschien te vervangen in plaats van te "
        "verbouwen. Bewust buiten het opdeelwerk gehouden."
    ),
    "bg/project-tabs.html.j2": WERKLIJST,
    "bg/projects.html.j2": WERKLIJST,
    "bg/feedback.html.j2": WERKLIJST,
    "bg/project-details.html.j2": WERKLIJST,
    "bg/admin-approvals.html.j2": WERKLIJST,
    "bg/dashboard.html.j2": WERKLIJST,
    "bg/wizard-start.html.j2": WERKLIJST,
    "bg/admin-users.html.j2": WERKLIJST,
    "bg/metrics-explorer.html.j2": WERKLIJST,
    "bg/permission-denied.html.j2": WERKLIJST,
    "bg/admin-usage.html.j2": WERKLIJST,
    "bg/actions.html.j2": WERKLIJST,
    "bg/cli.html.j2": WERKLIJST,
}

# Templates die een eigen <style>-blok mogen houden, met de reden.
STYLE_BLOCK_EXCEPTIONS: dict[str, str] = {
    "architecture-overview.html.j2": "Zie CONTENT_BLOCK_EXCEPTIONS: deze pagina wordt in zijn geheel apart beoordeeld.",
    "bg/admin-usage.html.j2": WERKLIJST,
}

# Toegestane ``style=``-attributen per template, met de reden. Alles wat hier niet staat,
# mag er nul hebben. Wat hier staat is vormgeving die niet in een CSS-bestand kan: een
# waarde die de template zelf uitrekent, of een component dat een klasse anders behandelt
# dan een style.
INLINE_STYLE_BUDGET: dict[str, tuple[int, str]] = {
    "architecture-overview.html.j2": (
        85,
        "Zie CONTENT_BLOCK_EXCEPTIONS: deze pagina wordt in zijn geheel apart beoordeeld.",
    ),
    "widgets/_macros.html.j2": (
        1,
        "De breedte van de voortgangsbalk is de waarde zelf. Staat in de macro progress_bar, "
        "zodat die uitzondering op een plek blijft.",
    ),
    "project-details/_resource-usage.html.j2": (
        2,
        "De breedte van de CPU- en van de geheugenbalk is de waarde zelf.",
    ),
    "bg/_task-progress.html.j2": (
        1,
        "De breedte van de voortgangsbalk is de waarde zelf; zelfde uitzondering als in widgets/_macros.html.j2.",
    ),
    "widgets/form_start.html.j2": (
        1,
        "De tussenruimte van de stapel wordt als custom property gezet omdat het "
        "componentensysteem er geen klasse voor kent.",
    ),
    "wizard/wizard_steps_indicator.html.j2": (
        1,
        "De breedte van de voortgangsbalk is het percentage voltooide stappen.",
    ),
    "widgets/button_group.html.j2": (
        1,
        "De tussenruimte tussen de knoppen is een instelling van het veld en komt uit de "
        "formulierdefinitie, niet uit een vaste lijst.",
    ),
    "wizard/partials/backup_select_deployment.html.j2": (
        1,
        "De kleur van het resourcetype-label komt uit de servicedefinitie; er is geen vaste "
        "lijst kleuren om klassen voor te maken.",
    ),
}


def _templates() -> list[tuple[str, str]]:
    """(pad ten opzichte van TEMPLATES_DIR, inhoud) voor elke template."""
    return [(str(p.relative_to(TEMPLATES_DIR)), p.read_text()) for p in sorted(TEMPLATES_DIR.rglob("*.j2"))]


def _content_block_lines(text: str) -> int:
    """Grootste ``content``-blok in regels, of 0 als de template er geen heeft."""
    largest = 0
    stack: list[tuple[str, int]] = []
    for number, line in enumerate(text.splitlines()):
        start = BLOCK_START.search(line)
        if start:
            stack.append((start.group(1), number))
        if BLOCK_END.search(line) and stack:
            name, first = stack.pop()
            if name == "content":
                largest = max(largest, number - first + 1)
    return largest


def test_no_inline_style_attributes() -> None:
    """Vormgeving hoort in de CSS-bestanden of in het component, niet in een attribuut."""
    offenders: list[str] = []
    for name, text in _templates():
        found = len(INLINE_STYLE.findall(text))
        allowed = INLINE_STYLE_BUDGET.get(name, (0, ""))[0]
        if found > allowed:
            offenders.append(f"{name}: {found} style=-attributen, toegestaan zijn er {allowed}")

    assert not offenders, "Zet de vormgeving in static/css/ of laat hem aan het component over:\n" + "\n".join(
        offenders
    )


def test_inline_style_budget_is_not_stale() -> None:
    """Een bovengrens die boven de werkelijkheid ligt, geeft ruimte terug die net gewonnen is."""
    counts = {name: len(INLINE_STYLE.findall(text)) for name, text in _templates()}
    stale = [
        f"{name}: bovengrens {allowed}, werkelijk {counts.get(name, 0)} - verlaag de bovengrens"
        for name, (allowed, _) in INLINE_STYLE_BUDGET.items()
        if allowed > counts.get(name, 0)
    ]

    assert not stale, "INLINE_STYLE_BUDGET loopt achter op de werkelijkheid:\n" + "\n".join(stale)


def test_no_style_blocks_in_templates() -> None:
    """Een <style>-blok in een pagina is vormgeving die niet mee kan verhuizen."""
    offenders = [
        f"{name}: {len(STYLE_BLOCK.findall(text))} <style>-blok(ken)"
        for name, text in _templates()
        if STYLE_BLOCK.search(text) and name not in STYLE_BLOCK_EXCEPTIONS
    ]

    assert not offenders, "Verplaats deze CSS naar een bestand onder static/css/:\n" + "\n".join(offenders)


def test_style_block_exceptions_are_not_stale() -> None:
    """Een uitzondering voor een template zonder <style>-blok verbergt de regel weer."""
    texts = dict(_templates())
    stale = [name for name in STYLE_BLOCK_EXCEPTIONS if name not in texts or not STYLE_BLOCK.search(texts[name])]

    assert not stale, "Deze uitzonderingen zijn niet meer nodig, haal ze weg:\n" + "\n".join(stale)


def test_content_blocks_are_compositions() -> None:
    """Een pagina stelt deeltemplates samen; een blok van honderden regels doet dat niet."""
    offenders = [
        f"{name}: content-blok van {_content_block_lines(text)} regels, grens is {MAX_CONTENT_BLOCK_LINES}"
        for name, text in _templates()
        if name not in CONTENT_BLOCK_EXCEPTIONS and _content_block_lines(text) > MAX_CONTENT_BLOCK_LINES
    ]

    assert not offenders, "Deel dit op in benoemde deeltemplates die de pagina samenstelt:\n" + "\n".join(offenders)


def test_content_block_exceptions_are_not_stale() -> None:
    """Een uitzondering voor een pagina die inmiddels binnen de grens valt, hoort weg."""
    texts = dict(_templates())
    stale = [
        name
        for name in CONTENT_BLOCK_EXCEPTIONS
        if name not in texts or _content_block_lines(texts[name]) <= MAX_CONTENT_BLOCK_LINES
    ]

    assert not stale, "Deze uitzonderingen zijn niet meer nodig, haal ze weg:\n" + "\n".join(stale)


def test_stylesheets_contain_no_jinja() -> None:
    """CSS die uit een template komt, mag geen Jinja meer bevatten.

    In een <style>-blok werd ``width: 30%`` soms als ``width: 30{{ '%' }}`` geschreven.
    Dat rendert daar naar het goede, maar een bestand onder static/ komt nooit langs
    Jinja: daar blijft de accolade letterlijk staan en valt de regel stil weg. Precies
    het soort verlies dat je bij het verplaatsen niet ziet.
    """
    offenders: list[str] = []
    for stylesheet in sorted((STATIC_DIR / "css").glob("*.css")):
        for number, line in enumerate(stylesheet.read_text().splitlines(), start=1):
            if JINJA_SYNTAX.search(line):
                offenders.append(f"{stylesheet.name}:{number}: {line.strip()}")

    assert not offenders, "Een CSS-bestand wordt niet door Jinja gerenderd:\n" + "\n".join(offenders)


def test_no_element_carries_two_class_attributes() -> None:
    """Een element met twee class-attributen verliest het tweede zonder melding.

    Bij het omzetten van style= naar class= is dit de val: een element had al een
    class en kreeg er een voorwaardelijke bij als los attribuut. Als style= stond het
    naast de class en werkte het; als tweede class= houdt de browser alleen de eerste
    aan en gebeurt er niets meer.

    De Jinja-blokken gaan er eerst uit: staat er een ``>`` in een voorwaarde, dan kapt
    HTML_TAG de tag daar af en blijft er precies een class over. Zie JINJA_BLOCK.
    """
    offenders = [
        f"{name}: {' '.join(tag.split())[:120]}"
        for name, text in _templates()
        for tag in HTML_TAG.findall(JINJA_BLOCK.sub("", text))
        if len(CLASS_ATTRIBUTE.findall(tag)) > 1
    ]

    assert not offenders, "Voeg de klassen samen tot een class-attribuut:\n" + "\n".join(offenders)
