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

from opi.core.templates import TEMPLATES_DIR

INLINE_STYLE = re.compile(r'\bstyle="')
STYLE_BLOCK = re.compile(r"<style[\s>]")
BLOCK_START = re.compile(r"{%-?\s*block\s+(\w+)")
BLOCK_END = re.compile(r"{%-?\s*endblock")

# Reden voor een uitzondering die er niet hoort te zijn maar er nog wel is. Elke regel met
# deze reden is werk dat nog moet gebeuren; de test houdt alleen tegen dat er iets bij komt.
WERKLIJST = "werklijst: nog op te schonen, mag alleen korter worden"

# Een content-blok boven deze grens is geen samenstelling meer. De grens is een richtlijn
# voor de mens en een harde regel voor de test: het gaat erom dat de eenheden betekenis
# hebben, niet dat ze klein zijn.
MAX_CONTENT_BLOCK_LINES = 50

# Templates die hun content-blok niet opdelen, met de reden.
CONTENT_BLOCK_EXCEPTIONS: dict[str, str] = {
    "architecture-overview.html.j2": (
        "1.500 regels handgeschreven markup in een enkel blok. Dit is geen pagina om op te "
        "delen maar een om apart te beoordelen, en misschien te vervangen in plaats van te "
        "verbouwen. Bewust buiten het opdeelwerk gehouden."
    ),
    "about.html.j2": WERKLIJST,
    "admin/approvals.html.j2": WERKLIJST,
    "admin/usage.html.j2": WERKLIJST,
    "admin/users.html.j2": WERKLIJST,
    "dashboard.html.j2": WERKLIJST,
    "metrics-explorer.html.j2": WERKLIJST,
    "permission-denied.html.j2": WERKLIJST,
    "project-details.html.j2": WERKLIJST,
    "project-form-demo.html.j2": WERKLIJST,
    "projects-overview.html.j2": WERKLIJST,
    "services-overview.html.j2": WERKLIJST,
    "wizard/wizard_start.html.j2": WERKLIJST,
}

# Templates die een eigen <style>-blok mogen houden, met de reden.
STYLE_BLOCK_EXCEPTIONS: dict[str, str] = {
    "base.html.j2": (
        "Globale reparatie van de ROOS c-alert-layout, met een TODO erboven om hem te "
        "verwijderen zodra jinja-roos-components is gerepareerd. Hoort bij de hack, niet "
        "bij een pagina."
    ),
    "architecture-overview.html.j2": "Zie CONTENT_BLOCK_EXCEPTIONS: deze pagina wordt in zijn geheel apart beoordeeld.",
    "admin/approvals.html.j2": WERKLIJST,
    "admin/users.html.j2": WERKLIJST,
    "dashboard.html.j2": WERKLIJST,
    "metrics-explorer.html.j2": WERKLIJST,
    "project-form-demo.html.j2": WERKLIJST,
    "projects-overview.html.j2": WERKLIJST,
    "tools.html.j2": WERKLIJST,
    "wizard/modal_wizard_review.html.j2": WERKLIJST,
    "wizard/modal_wizard_step.html.j2": WERKLIJST,
    "wizard/partials/approval_items.html.j2": WERKLIJST,
    "wizard/partials/attachments_upload.html.j2": WERKLIJST,
    "wizard/wizard_review.html.j2": WERKLIJST,
    "wizard/wizard_start.html.j2": WERKLIJST,
    "wizard/wizard_step.html.j2": WERKLIJST,
}

# Toegestane ``style=``-attributen per template, met de reden. Alles wat hier niet staat,
# mag er nul hebben.
INLINE_STYLE_BUDGET: dict[str, tuple[int, str]] = {
    "architecture-overview.html.j2": (
        85,
        "Zie CONTENT_BLOCK_EXCEPTIONS: deze pagina wordt in zijn geheel apart beoordeeld.",
    ),
    "admin/approvals.html.j2": (4, WERKLIJST),
    "admin/usage.html.j2": (1, WERKLIJST),
    "admin/user-form.html.j2": (1, WERKLIJST),
    "admin/users.html.j2": (6, WERKLIJST),
    "dashboard.html.j2": (2, WERKLIJST),
    "metrics-explorer.html.j2": (7, WERKLIJST),
    "partials/deployment_metrics.html.j2": (4, WERKLIJST),
    "widgets/_macros.html.j2": (
        1,
        "De breedte van de voortgangsbalk is de waarde zelf en kan niet in een CSS-bestand. "
        "Staat in de macro progress_bar, zodat die uitzondering op een plek blijft.",
    ),
    "project-details/_argocd-status-skeleton.html.j2": (7, WERKLIJST),
    "project-details/_resource-usage.html.j2": (4, WERKLIJST),
    "project-details/action-confirm.html.j2": (1, WERKLIJST),
    "project-details/modals.html.j2": (1, WERKLIJST),
    "project-details/section-actions.html.j2": (1, WERKLIJST),
    "project-details/section-components.html.j2": (1, WERKLIJST),
    "project-details/section-config.html.j2": (1, WERKLIJST),
    "project-details/section-danger-zone.html.j2": (9, WERKLIJST),
    "project-details/section-deployment-actions.html.j2": (2, WERKLIJST),
    "project-details/section-deployment-argocd.html.j2": (1, WERKLIJST),
    "project-details/section-deployment-state.html.j2": (1, WERKLIJST),
    "project-details/section-deployment-status.html.j2": (1, WERKLIJST),
    "project-details/section-deployments.html.j2": (10, WERKLIJST),
    "project-details/section-env-vars.html.j2": (1, WERKLIJST),
    "project-details/section-header.html.j2": (2, WERKLIJST),
    "project-details/section-helm-charts.html.j2": (4, WERKLIJST),
    "project-details/section-helmfile.html.j2": (4, WERKLIJST),
    "project-details/section-repositories.html.j2": (1, WERKLIJST),
    "project-details/section-resource-usage.html.j2": (3, WERKLIJST),
    "project-details/section-tasks.html.j2": (3, WERKLIJST),
    "project-details/section-team.html.j2": (3, WERKLIJST),
    "project-details.html.j2": (3, WERKLIJST),
    "project-form-demo.html.j2": (1, WERKLIJST),
    "projects-overview.html.j2": (6, WERKLIJST),
    "roos-form-improved.html.j2": (1, WERKLIJST),
    "roos-form.html.j2": (2, WERKLIJST),
    "tools.html.j2": (4, WERKLIJST),
    "widgets/button_group.html.j2": (1, WERKLIJST),
    "widgets/text.html.j2": (1, WERKLIJST),
    "wizard/modal_wizard_progress.html.j2": (1, WERKLIJST),
    "wizard/modal_wizard_success.html.j2": (1, WERKLIJST),
    "wizard/partials/attachments_list.html.j2": (3, WERKLIJST),
    "wizard/partials/attachments_upload.html.j2": (2, WERKLIJST),
    "wizard/partials/backup_select_deployment.html.j2": (6, WERKLIJST),
    "wizard/partials/restore_select_backup.html.j2": (12, WERKLIJST),
    "wizard/partials/restore_select_target.html.j2": (11, WERKLIJST),
    "wizard/partials/url_preview.html.j2": (1, WERKLIJST),
    "wizard/wizard_steps_indicator.html.j2": (1, WERKLIJST),
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

    assert not offenders, "Zet de vormgeving in static/css/ of laat hem aan het ROOS-component over:\n" + "\n".join(
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
