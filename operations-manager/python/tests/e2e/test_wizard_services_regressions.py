"""Wizard regressions on the project-level services list (local test server).

Both tests capture the YAML the wizard would commit by stubbing the async-task
creation, so they assert on the real artifact instead of on the page.

1. A preset applied on a service-config step must stick: its card renders selected
   and its values reach the project file. The config lived on a ``{name, config}``
   record that the devirtualize step in ``get_merged_data`` did not recognise, so
   every value was dropped on the next merge.
2. Selecting a service that locks a dependency and then deselecting it again must
   not post the dependency twice. The locked card used to render a hidden carrier
   input next to its disabled checkbox; unlocking re-enabled the checkbox client-side
   while the carrier stayed, so the name was submitted twice and reached the project
   file as a config record plus a stray bare string.
3. A component added later starts with every project service ticked, independent of
   what an earlier component has. The picker is filtered on the project's service
   names, which were read off a record's raw dict keys, so every config-carrying
   service dropped out and only the config-less half came up ticked.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
import yaml
from playwright.sync_api import expect
from tests.e2e.helpers.wizard import WizardHelper, unique_project_name

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = [pytest.mark.e2e]

_PRESET_CARD = "[hx-post$='/preset/keycloak-config/toegangsbeperking']"


@pytest.fixture
def captured_yaml(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Capture the project YAML the wizard hands to the create task."""
    captured: list[str] = []

    import opi.core.task_helpers as task_helpers

    async def _fake_create_async_task(**kwargs: Any) -> dict[str, Any]:
        captured.append(kwargs["payload"]["yaml_content"])
        return {"task_id": "00000000-0000-0000-0000-000000000000"}

    monkeypatch.setattr(task_helpers, "create_async_task", _fake_create_async_task)
    return captured


_MAX_WIZARD_STEPS = 15


def _current_step(page: Page) -> str:
    """The step id from the wizard URL, e.g. ``components``."""
    return page.url.rsplit("/step/", 1)[-1]


def _advance_to_step(wizard: WizardHelper, page: Page, step: str) -> None:
    """Click through the wizard until *step* is the current step.

    Which config steps sit in between follows the selected services, so this
    cannot be a fixed number of clicks: adding a service - or giving an existing
    one a config step - silently shifted every later step by one, and the test
    then filled the wrong page.
    """
    for _ in range(_MAX_WIZARD_STEPS):
        if _current_step(page) == step:
            return
        if _current_step(page) == "team":
            wizard.fill_team(email="test@example.com")
        wizard.click_next()
        page.wait_for_load_state("networkidle")
    raise AssertionError(f"step {step!r} not reached within {_MAX_WIZARD_STEPS} steps; stuck at {page.url}")


def _finish_wizard(wizard: WizardHelper, page: Page) -> None:
    """Walk the remaining steps and submit, however many of them there are.

    The step count is not fixed: every selected service inserts its own config
    step (``redis-config``, ``keycloak-config``, ...). Counting clicks left the
    walk one step short of the review page, where ``submit_wizard`` fell back to
    the step's own Next button, so no project was ever created and the test
    failed on an empty capture instead of on the thing it means to assert.
    """
    for _ in range(_MAX_WIZARD_STEPS):
        if page.locator("#wizard-review-submit").count() > 0:
            break
        step = _current_step(page)
        if step == "team":
            wizard.fill_team(email="test@example.com")
        elif step == "components":
            wizard.fill_component(name="web", image="nginx:latest")
        wizard.click_next()
        page.wait_for_load_state("networkidle")
    else:
        raise AssertionError(f"review page not reached within {_MAX_WIZARD_STEPS} steps; stuck at {page.url}")

    wizard.submit_wizard()
    page.wait_for_timeout(2000)


def _services_block(yaml_text: str) -> list[str]:
    """The raw lines of the top-level ``services:`` block."""
    lines = yaml_text.splitlines()
    start = lines.index("services:")
    block: list[str] = []
    for line in lines[start + 1 :]:
        if line and not line.startswith(" "):
            break
        block.append(line)
    return block


def test_preset_stays_applied(app_server: str, auth_page: Page, captured_yaml: list[str]) -> None:
    """Clicking a preset selects its card and its values reach the project file."""
    wizard = WizardHelper(auth_page, app_server)
    wizard.open_create_wizard()

    wizard.fill_identity(display_name=unique_project_name(), description="preset regression")
    wizard.click_next()

    wizard.fill_services(["keycloak"])
    wizard.click_next()

    # Keycloak config step: apply the "Toegangsbeperking" preset
    auth_page.wait_for_load_state("networkidle")
    assert auth_page.locator(f"{_PRESET_CARD}.service-card--selected").count() == 0, "preset should start unapplied"
    auth_page.locator(_PRESET_CARD).first.click()
    auth_page.wait_for_load_state("networkidle")

    # Wait for the swapped-in card instead of for a fixed 500ms: applying a preset
    # re-renders the step server-side, and on a busy machine that takes longer than
    # any number picked here. A fixed pause turns a slow render into a failing test.
    expect(auth_page.locator(f"{_PRESET_CARD}.service-card--selected")).to_have_count(1, timeout=10000)

    wizard.click_next()
    _finish_wizard(wizard, auth_page)

    assert captured_yaml, "no project YAML captured"
    yaml_text = captured_yaml[0]
    assert "restrict-access:" in yaml_text, f"preset values missing from project file:\n{yaml_text}"
    assert "realm-role: allowed-user" in yaml_text


def test_unlocking_a_dependency_does_not_duplicate_it(
    app_server: str, auth_page: Page, captured_yaml: list[str]
) -> None:
    """A service locked and then unlocked again lands in the project file exactly once."""
    wizard = WizardHelper(auth_page, app_server)
    wizard.open_create_wizard()

    wizard.fill_identity(display_name=unique_project_name(), description="duplication regression")
    wizard.click_next()

    # authorization-wall requires keycloak, so keycloak renders locked (disabled)
    wizard.fill_services(["authorization-wall", "redis"])
    wizard.click_next()

    # Come back so the step is re-rendered server-side with keycloak locked, then
    # release the lock by deselecting authorization-wall.
    wizard.click_previous()
    auth_page.wait_for_load_state("networkidle")
    auth_page.locator("[data-service='authorization-wall']").first.click()
    auth_page.wait_for_timeout(300)

    wizard.click_next()
    auth_page.wait_for_load_state("networkidle")
    wizard.click_next()  # keycloak config defaults
    _finish_wizard(wizard, auth_page)

    assert captured_yaml, "no project YAML captured"
    block = _services_block(captured_yaml[0])
    keycloak_entries = [line for line in block if line.strip() in ("- keycloak", "- name: keycloak")]
    assert len(keycloak_entries) == 1, f"keycloak listed {len(keycloak_entries)}x:\n" + "\n".join(block)


def test_second_component_offers_every_project_service(app_server: str, auth_page: Page) -> None:
    """A component added later starts with all project services ticked."""
    wizard = WizardHelper(auth_page, app_server)
    wizard.open_create_wizard()

    wizard.fill_identity(display_name=unique_project_name(), description="component services regression")
    wizard.click_next()

    # keycloak and publish-on-web carry config, redis and postgresql-database do not
    selected = ["publish-on-web", "keycloak", "redis", "postgresql-database"]
    wizard.fill_services(selected)
    wizard.click_next()

    _advance_to_step(wizard, auth_page, "components")

    # First component: untick everything but publish-on-web
    wizard.fill_component(name="web", image="nginx:latest")
    for service in ("keycloak", "redis", "postgresql-database"):
        box = auth_page.locator(f"input[name='components[0]/services[]'][value='{service}']").first
        if box.count() > 0 and box.is_checked():
            box.uncheck()
    auth_page.wait_for_timeout(200)

    # Add a second component. Wait for its checkboxes to exist rather than for a fixed
    # delay: under a full-suite run the htmx swap outlasts any timeout worth hardcoding,
    # and reading too early sees the pre-swap DOM and asserts on nothing.
    auth_page.locator("button:has-text('Toevoegen'), button:has-text('Component toevoegen')").last.click()
    auth_page.wait_for_selector("input[name='components[1]/services[]']", timeout=15000)

    ticked = auth_page.eval_on_selector_all(
        "input[name='components[1]/services[]']",
        "els => els.filter(e => e.checked).map(e => e.value)",
    )
    assert sorted(ticked) == sorted(selected), f"second component came up with {ticked}, expected {selected}"


def test_een_voorinstelling_meldt_niets(app_server: str, auth_page: Page) -> None:
    """Een voorinstelling aanklikken mag geen melding opleveren.

    Gemeld op de keycloak-configuratiestap: klikken op "Snelstart: kies een scenario" gaf
    een systeemvenster met "undefined is vereist en kan niet worden uitgezet".

    De oorzaak: de voorinstellingen gebruiken dezelfde klassen als de dienstkaarten, want ze
    zien er hetzelfde uit, maar ze dragen geen ``data-service`` - een voorinstelling is geen
    dienst. Ze liepen daardoor toch door de dienstenlogica heen, en die vroeg naar de naam
    van een dienst die er niet is. Dat was jarenlang onzichtbaar omdat de melding uit een
    regel IN de kaart kwam die er niet stond; zodra de melding zijn eigen tekst opbouwde,
    kwam hij wel in beeld.
    """
    meldingen: list[str] = []
    auth_page.on("dialog", lambda dialoog: (meldingen.append(dialoog.message), dialoog.accept()))

    wizard = WizardHelper(auth_page, app_server)
    wizard.open_create_wizard()
    wizard.fill_identity(display_name=unique_project_name(), description="voorinstelling zonder melding")
    wizard.click_next()
    # authorization-wall dwingt de waarden van de voorinstelling "toegangsbeperking" af, en
    # daardoor staat die voorinstelling VERGRENDELD op de configuratiestap. Dat is de stand
    # waarin de melding verscheen; een losse voorinstelling raakt hem nooit.
    wizard.fill_services(["keycloak", "authorization-wall"])
    wizard.click_next()
    auth_page.wait_for_load_state("networkidle")

    # Een VERGRENDELDE voorinstelling draagt geen hx-post (die staat er alleen als je hem
    # nog mag toepassen), dus hij is niet met _PRESET_CARD te vinden. Op de klasse dus.
    kaart = auth_page.locator(".service-card--locked-checked")
    kaart.first.wait_for(state="visible", timeout=10000)
    kaart.first.click()
    auth_page.wait_for_timeout(500)

    assert not meldingen, f"een voorinstelling aanklikken gaf een systeemvenster: {meldingen}"
    assert auth_page.locator("nldd-modal-dialog:visible").count() == 0, (
        "er hoort ook geen dialoog te verschijnen; een voorinstelling heeft geen slot"
    )


def test_een_uitnodiging_zonder_eigen_sleutel_wordt_opgeslagen(
    app_server: str, auth_page: Page, captured_yaml: list[str]
) -> None:
    """De wizard mag geen projectbestand opleveren dat de validatie afkeurt.

    Gemeld: aan het eind van de wizard kwam "Verwerking mislukt - Failed Git operations:
    configuratie van service 'invite' op projectniveau is ongeldig: Field required", en het
    projectbestand was niet opgeslagen, dus al het werk was weg.

    Wat er misging: het sleutelveld van een uitnodiging mag leeg blijven, dan wordt er een
    willekeurige sleutel gegenereerd. Dat gebeurt in de ``post_merge`` van de invite-sectie.
    Maar de wizard bewaarde de stapgegevens VOORDAT die hook draaide en bewaarde daarna
    alleen ``components`` opnieuw, dus de gegenereerde sleutel haalde de wizardstand nooit.
    Bij het opslaan stond er een uitnodiging zonder ``key``, en ``InviteEntry.key`` is
    verplicht.
    """
    wizard = WizardHelper(auth_page, app_server)
    wizard.open_create_wizard()
    wizard.fill_identity(display_name=unique_project_name(), description="uitnodiging zonder sleutel")
    wizard.click_next()

    # invite vereist keycloak; die komt automatisch mee.
    wizard.fill_services(["keycloak", "invite"])
    wizard.click_next()

    # De invite-stap gewoon doorlopen zonder een sleutel in te vullen.
    _finish_wizard(wizard, auth_page)

    assert captured_yaml, "de wizard heeft geen projectbestand opgeleverd; het opslaan is mislukt"

    # Op de GEPARSTE inhoud meten en niet op de tekst: "key:" komt ook voor in
    # age-public-key en age-private-key, dus een zoekopdracht op die tekst slaagt altijd en
    # meet niets. Dat is deze test een keer overkomen.
    project = yaml.safe_load(captured_yaml[-1])
    invite = next(
        (
            entry["config"]
            for entry in project.get("services", [])
            if isinstance(entry, dict) and entry.get("name") == "invite"
        ),
        None,
    )
    assert invite is not None, "de dienst invite hoort in het projectbestand te staan"

    uitnodigingen = invite.get("active") or []
    assert uitnodigingen, "er hoort een uitnodiging in te staan"
    for nummer, uitnodiging in enumerate(uitnodigingen):
        assert uitnodiging.get("key"), (
            f"uitnodiging {nummer} heeft geen sleutel. Een lege sleutel hoort bij het opslaan een "
            f"gegenereerde te worden; zonder sleutel keurt InviteEntry het projectbestand af en "
            f"gaat de hele wizardsessie verloren. Gekregen: {sorted(uitnodiging)}"
        )
