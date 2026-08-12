"""
Page object for the project creation wizard.

Encapsulates wizard navigation, form filling, and submission. Works against
both local app server and sandbox cluster URLs.

The wizard is HTMX-driven - step transitions happen via POST/GET without full
page reloads. We use Playwright's wait_for_load_state and network idle
detection to handle HTMX responses.
"""

from __future__ import annotations

import secrets
import string
import time
from typing import TYPE_CHECKING

from tests.e2e.helpers.htmx import wait_for_htmx_quiet

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Locator, Page


# Hoe vaak een veld opnieuw gevuld mag worden als een late render het weer leegmaakt.
_FILL_ATTEMPTS = 5

#: De tags waarin een echte formulierbesturing zit.
FIELD_TAGS = ("input", "textarea", "select")


def veldbesturing(page: Page, naam: str) -> Locator:
    """De echte formulierbesturing van het veld met dit pad, ongeacht de vormgeving.

    Op het NAME-attribuut zoeken werkt hier niet. In de LOTC-weergave draagt het custom
    element (``<nldd-text-field>``) een name en de ``<input>`` in zijn shadow root ook, dus
    ``[name='x']`` vindt er twee en Playwright weigert dat; bij een meerregelig veld draagt
    juist alleen het custom element een name en de ``<textarea>`` geen.

    Het ID is in beide vormgevingen wel precies een element, en wel de besturing zelf: de
    roos-widget zet ``id=<pad>`` op de input, en LOTC geeft datzelfde pad als ``input-id``
    door aan de input in de shadow root. Ids bevatten hier ``/`` en ``[]``, dus ze staan in
    een attribuutselector en niet achter een ``#``.

    Als losse functie en niet alleen als methode op WizardHelper, omdat ``lifecycle.py``
    zijn eigen ``[name='...']``-locators had en daar precies op deze val liep: de wizard
    kwam niet voorbij de team- en componentenstap, met "Node is not an <input>" in plaats
    van een leeg veld.
    """
    return page.locator(", ".join(f"{tag}[id='{naam}']" for tag in FIELD_TAGS))


def aanvinkvakje(page: Page, pad: str) -> Locator:
    """Het aanvinkvakje met dit pad: precies een element, met een bruikbare ``.checked``.

    Een aanvinkvakje is geen ``<input>`` maar het custom element zelf: dat draagt de stand
    (``.checked``) en levert zijn waarde als form-associated element. ``veldbesturing()``
    zoekt daarom naar het verkeerde ding.

    Op het NAME zoeken kan hier niet. Playwright kijkt door schaduwbomen heen, en het
    element geeft zijn name door aan zijn schaduwelementen; in de browser gemeten levert
    ``[name='<pad>']`` er dan drie op:

        NLDD-CHECKBOX-FIELD  (licht)
        NLDD-CHECKBOX        (schaduw van NLDD-CHECKBOX-FIELD)
        INPUT                (schaduw van NLDD-CHECKBOX)

    De id staat op het besturingselement en nergens anders: het component zet hem daar
    sinds LOTC 762e570, en ons sjabloon geeft hem alleen nog als prop mee. Daarvoor stond
    hij ook op de omhullende div en leverde ``[id='<pad>']`` er twee op. Nu is het er
    precies een - en wel het vakje zelf, dat ``.checked`` draagt.

    Voor een groep is dat ``<pad>-<waarde>`` per keuze; gebruik daarvoor
    :func:`aanvinkvakjes`, want op ``<pad>-`` matchen vindt ook de hulptekst
    (``<pad>-help``).
    """
    return page.locator(f"[id='{pad}']")


def aanvinkvakjes(page: Page, pad: str) -> Locator:
    """Alle vakjes van de GROEP met dit pad, in de volgorde waarin ze staan.

    Twee dingen worden hier bewust uitgesloten. De hulptekst en de foutmelding van het
    veld dragen ``<pad>-help`` en ``<pad>-error``, dus een prefixselector op de id pikt
    die mee (en telt dan een vakje te veel). De schaduwelementen dragen wel de name maar
    geen id. Naam EN id samen laat precies de vakjes over.
    """
    return page.locator(f"[name='{pad}[]'][id]")


def _unique_project_name(prefix: str = "e2e") -> str:
    """Generate a unique project name: e2e-{timestamp}-{random}."""
    ts = int(time.time()) % 100000
    suffix = "".join(secrets.choice(string.ascii_lowercase) for _ in range(4))
    return f"{prefix}-{ts}-{suffix}"


class WizardHelper:
    """Page object for the create-project wizard flow."""

    FLOW_ID = "create-project"

    def __init__(self, page: Page, base_url: str) -> None:
        self.page = page
        self.base_url = base_url.rstrip("/")
        self.project_name: str | None = None

    @property
    def wizard_url(self) -> str:
        return f"{self.base_url}/forms/wizard/{self.FLOW_ID}"

    def start(self) -> None:
        """Navigate to the wizard start page and begin the flow."""
        self.page.goto(f"{self.base_url}/forms/wizard/start")
        self.page.wait_for_load_state("networkidle")

    # Stepper label of the first step (identity). Used to assert we start clean.
    STEP_IDENTITY = "Projectgegevens"

    #: Het label van de actieve stap, in beide vormgevingen.
    #:
    #: De wizard bestaat tijdens de omzetting in twee weergaven: de bestaande
    #: roos-markup (een <li> met eigen klassen) en de LOTC-weergave (een
    #: <nldd-step-indicator-item>, waar het label een attribuut is en geen tekst).
    #: Deze helper draait tegen allebei, zodat de gedragstests over GEDRAG blijven gaan
    #: en niet over welk componentensysteem de pagina toevallig rendert.
    ROOS_ACTIVE_STEP_LABEL = "li[aria-current='step'] .wizard-steps__label"
    LOTC_ACTIVE_STEP_ITEM = "nldd-step-indicator-item[status='current']"
    ACTIVE_STEP_LABEL = f"{ROOS_ACTIVE_STEP_LABEL}, {LOTC_ACTIVE_STEP_ITEM}"

    #: De knop die de stap indient. In de LOTC-weergave is dat een <nldd-button> en geen
    #: <button>, dus de selector mag niet op de tagnaam hangen. Hij levert daar TWEE
    #: treffers - het custom element en de <button> in zijn shadow root - vandaar .first
    #: bij het klikken: dat is het element in de gewone DOM, en het dient het formulier
    #: netjes in (nldd-button is form-associated en roept requestSubmit() aan).
    # De knoppenbalk heet in de bestaande wizard .wizard-step__actions en in de nieuwe
    # .lotc-action-group (c-action-group). Beide vormen staan erin, zodat een test niet
    # hoeft te weten welke weergave hij meet.
    SUBMIT_BUTTON = (
        "#wizard-step-form [type='submit'], "
        ".wizard-step__actions button[type='submit'], "
        ".lotc-action-group button[type='submit']"
    )

    #: Foutmeldingen bij een veld, in beide vormgevingen. De LOTC-weergave zet ze in een
    #: <nldd-form-field-error-text>; de roos-weergave in een eigen klasse.
    FIELD_ERRORS = ".rvo-form-field__error, .field-error, [role='alert'], nldd-form-field-error-text"

    #: Zie :func:`veldbesturing` voor waarom dit op het id gaat en niet op de naam.
    FIELD_TAGS = FIELD_TAGS

    def field(self, name: str) -> Locator:
        """De invoerbesturing van het veld met dit pad."""
        return veldbesturing(self.page, name)

    def open_create_wizard(self) -> None:
        """Navigate to a FRESH create-project wizard, guaranteed to start on step 1.

        The wizard keeps server-side state keyed by the session (see
        ``wizard_page`` in ``opi/web/router_wizard.py``: it *resumes* an existing
        flow at its current step). Because the E2E browser context is shared across
        the whole test session, a prior test that left the wizard part-way (e.g. on
        the Components step) makes ``GET /forms/wizard/create-project`` resume there
        instead of at step 1 -- so the identity step's ``display-name`` field is
        absent and ``fill_identity`` burns a blind 15s timeout. Hitting ``/restart``
        first clears that state (``wizard_restart`` -> ``clear_wizard_state``), so
        every create starts clean regardless of what ran before.
        """
        self.page.goto(f"{self.base_url}/forms/wizard/restart")
        self.page.wait_for_load_state("networkidle")
        self.page.goto(self.wizard_url)
        self.page.wait_for_load_state("networkidle")
        # Fail fast with a useful message if we did not land on step 1, instead of
        # letting fill_identity time out on a field that lives on another step.
        self.assert_on_step(self.STEP_IDENTITY)

    def fill_identity(
        self,
        display_name: str | None = None,
        description: str = "E2E test project",
        cluster: str | None = None,
    ) -> None:
        """Fill the identity step fields.

        Targets the specific fields by name and waits for them to render, rather than
        falling back to broad ``input.first`` / ``textarea.first`` locators. Those
        fallbacks were dangerous: under load the identity form renders a moment late, so
        the specific locator was momentarily empty and the fallback grabbed the FIRST
        textarea on the page -- which, on a partially-rendered/wrong step, is the hidden
        CodeMirror-backed ``components[0]/aliases`` textarea (``display:none``). Playwright
        then waited 30s for that hidden element to become visible before failing, turning
        a timing blip into a minutes-long, failing create. Waiting for the real field is
        both correct and event-based.
        """
        if display_name is None:
            display_name = _unique_project_name()
        self.project_name = display_name

        # Verify we are actually on the identity step before filling. A clear
        # "on step X, expected Projectgegevens" beats a blind 15s timeout on a
        # display-name field that lives on step 1 when the wizard resumed elsewhere.
        self.assert_on_step(self.STEP_IDENTITY)

        name_input = self.field("display-name")
        name_input.wait_for(state="visible", timeout=15000)
        name_input.fill(display_name)

        desc_input = self.field("description")
        desc_input.wait_for(state="visible", timeout=15000)
        desc_input.fill(description)

        # Select cluster if provided and a select exists
        if cluster:
            cluster_select = self.field("clusters")
            if cluster_select.count() > 0:
                cluster_select.select_option(value=cluster)

    def fill_team(self, email: str = "admin@sandbox.rijksapp.dev", role: str = "admin") -> None:
        """Fill the team step with a single user."""
        # Field name uses slash notation: users[0]/email
        email_input = self.field("users[0]/email")
        if email_input.count() > 0:
            email_input.fill(email)
        else:
            # Fallback to partial match
            email_input = self.page.locator("input[name*='email']").first
            if email_input.count() > 0:
                email_input.fill(email)

        # Role is typically a hidden input with a default value - no interaction needed.
        # Only attempt to set it if it's a visible select element.
        role_select = self.page.locator("select[name='users[0]/role']")
        if role_select.count() > 0 and role_select.is_visible():
            role_select.select_option(value=role)

    def fill_component(
        self,
        name: str = "web",
        image: str = "nginx:latest",
    ) -> None:
        """Fill a minimal component definition.

        Targets the exact component fields by name and waits for them, instead of the
        fuzzy ``[name*='name']`` / ``[name*='image']`` selectors, which also match storage
        sub-fields and, under a late render, the wrong element.
        """
        comp_name = self.field("components[0]/name")
        comp_name.wait_for(state="visible", timeout=15000)

        # Typen en dan controleren dat het bleef staan, want een zichtbaar veld is nog
        # geen veld dat klaar is. De componentenstap kan een htmx-swap onderweg hebben
        # die de rij vervangt; landt die na het typen, dan is het veld weer leeg, gaat
        # de stap zonder naam de deur uit en komt hij terug met 'Dit veld is verplicht'.
        # Dat leest als een productfout en is een wedloop: gemeten wist de swap de zojuist
        # getypte 'web' binnen 400ms weer uit.
        # Eerst laten uitrazen, en daarna nog controleren dat het bleef staan: het wachten
        # dekt de swap die al hing, de controle dekt wat er alsnog achteraan komt.
        for _ in range(_FILL_ATTEMPTS):
            self.wait_for_htmx_quiet()
            comp_name.fill(name)
            if comp_name.input_value() == name:
                break
        else:
            raise AssertionError(
                f"component name did not stick after {_FILL_ATTEMPTS} attempts; "
                "something keeps re-rendering the components step"
            )

        comp_image = self.field("components[0]/image").first
        if comp_image.count() > 0:
            comp_image.fill(image)

    def click_next(self) -> None:
        """Click the Next/submit button to advance to the next step."""
        next_btn = self.page.locator(self.SUBMIT_BUTTON).first
        self._click_and_wait_for_step_change(next_btn)

    def click_previous(self) -> None:
        """Click the Previous button to go back a step."""
        prev_btn = self.page.locator("button:has-text('Vorige'), button:has-text('Previous')")
        if prev_btn.count() > 0:
            self._click_and_wait_for_step_change(prev_btn.first)

    def click_review(self) -> None:
        """Click the review/check button on the last step."""
        review_btn = self.page.locator("button:has-text('Controleren'), button[type='submit']").last
        self._click_and_wait_for_step_change(review_btn)

    #: De markering die de laatste wizardstap krijgt VOOR de verzendknop wordt ingedrukt,
    #: zodat "de stap is vervangen" te onderscheiden is van "de stap staat er nog".
    VOOR_VERZENDEN = "zadVoorVerzenden"

    def submit_wizard(self, timeout: float = 30000) -> None:
        """Dien de wizard in en wacht tot hij ergens geland is.

        WAAROM DIT NIET OP ``networkidle`` WACHT. Dat deed het wel, plus wachten tot er
        weer een ``#wizard-step-inner`` in de pagina stond, en daar strandden op de
        sandbox 25 tests in hun OPZET (generale repetitie, bevinding 7). Twee dingen
        gingen daar mis, en allebei zijn ze eigen aan juist deze klik:

        1. De laatste verzendknop verlaat de wizardpagina: hij landt op
           ``/projects/progress/<id>``. Daar IS geen ``#wizard-step-inner``, dus die
           voorwaarde kon per definitie niet meer waar worden.
        2. Die voortgangspagina peilt zichzelf met htmx. Een pagina die blijft peilen
           wordt nooit ``networkidle``, dus ook die wachtvoorwaarde loopt af.

        Het bewijs dat het de TOETS was en niet de wizard: de projecten die deze
        fixtures aanmaakten stonden na afloop gewoon in ``zad-projects``, met een
        ArgoCD-applicatie op ``Synced``/``Healthy``. De wizard doorliep de keten; de
        test gaf te vroeg op.

        Er zijn drie landingen mogelijk en alle drie zijn ze een afloop:

        - de voortgangspagina (aanmaken geaccepteerd);
        - een ingeswapte voortgangsweergave, als de flow in de pagina blijft;
        - een NIEUWE wizardstap, als de server iets afkeurde - dat is een echte
          uitkomst en geen wachtfout, en de aanroeper ziet hem aan de foutmelding.

        Om de derde te kunnen zien wordt de huidige stap eerst gemarkeerd: zonder dat is
        "de stap staat er nog" niet te onderscheiden van "de stap is vervangen", en dan
        keert de wachtvoorwaarde meteen terug.
        """
        self.page.evaluate(
            "(sleutel) => { const el = document.querySelector('#wizard-step-inner');"
            " if (el) el.dataset[sleutel] = '1'; }",
            self.VOOR_VERZENDEN,
        )
        submit_btn = self.page.locator("button:has-text('Project aanmaken'), button:has-text('Indienen')")
        if submit_btn.count() > 0:
            submit_btn.first.click()
        else:
            # Fallback: any primary submit button
            self.page.locator("button[type='submit']").last.click()
        self.page.wait_for_function(
            """(sleutel) => {
                if (location.pathname.startsWith('/projects/progress/')) return true;
                if (document.querySelector('#project-progress')) return true;
                const el = document.querySelector('#wizard-step-inner');
                return el !== null && !el.dataset[sleutel];
            }""",
            arg=self.VOOR_VERZENDEN,
            timeout=timeout,
        )

    def wait_for_step(self, step_title: str, timeout: float = 10000) -> None:
        """Wait for a specific step to be loaded by checking the page content."""
        self.page.wait_for_selector(f"text={step_title}", timeout=timeout)

    def wait_for_review(self, timeout: float = 10000) -> None:
        """Wait for the review page to load."""
        self.page.wait_for_selector(
            "text=Controleren, text=Samenvatting, text=Review",
            timeout=timeout,
        )

    def wait_for_progress_complete(self, timeout: float = 120000) -> None:
        """Wait for the deployment progress to finish."""
        # Wait for success indicator or project list redirect
        self.page.wait_for_selector(
            "[data-status='completed'], .progress-complete, text=voltooid",
            timeout=timeout,
        )

    def get_current_step_title(self) -> str | None:
        """Return the title (stepper label) of the currently-active wizard step."""
        label = self.page.locator(self.ACTIVE_STEP_LABEL)
        if label.count() == 0:
            return None
        # In de LOTC-weergave staat het label in het text-attribuut van het
        # custom element; de tekstinhoud zit in zijn shadow root en is leeg.
        return ((label.first.get_attribute("text") or label.first.text_content()) or "").strip()

    def assert_on_step(self, expected_title: str, timeout: float = 10000) -> None:
        """Assert the wizard is showing the expected step; fail fast if not.

        Reading the active step indicator is far more useful than a blind
        ``wait_for`` on a field: if the wizard resumed on the wrong step the field
        we want is simply absent, and we would wait out the full timeout with no
        clue why. This says which step we actually got, and points at the usual
        cause (leaked wizard state from a prior test in the shared context).
        """
        label = self.page.locator(self.ACTIVE_STEP_LABEL)
        label.first.wait_for(state="visible", timeout=timeout)
        current = self.get_current_step_title() or ""
        if expected_title.lower() not in current.lower():
            raise AssertionError(
                f"Wizard is on step '{current}', expected '{expected_title}' "
                f"(url={self.page.url}). Likely leaked wizard state from a prior test."
            )

    def has_validation_errors(self) -> bool:
        """Check if the current step shows validation errors."""
        errors = self.page.locator(self.FIELD_ERRORS)
        return errors.count() > 0

    def get_validation_error_texts(self) -> list[str]:
        """Return all visible validation error messages."""
        errors = self.page.locator(self.FIELD_ERRORS)
        return [errors.nth(i).text_content() or "" for i in range(errors.count())]

    def fill_services(self, services: list[str] | None = None) -> None:
        """Select service cards on the services step.

        Args:
            services: List of service names to select (e.g., ["keycloak", "postgresql"]).
                      If None or empty, no services are selected (just advance).
        """
        if not services:
            return

        for service in services:
            # Try clicking service cards by data attribute, label text, or checkbox
            card = self.page.locator(
                f"[data-service='{service}'], label:has-text('{service}'), input[value='{service}']"
            ).first
            if card.count() > 0:
                card.click()

    def fill_deployment(
        self,
        name: str = "default",
        image: str = "nginx:latest",
    ) -> None:
        """Fill deployment step fields."""
        name_input = self.page.locator("input[name*='deployment'] input[name*='name'], input[name*='name']").first
        if name_input.count() > 0:
            name_input.fill(name)

        image_input = self.page.locator("input[name*='image']").first
        if image_input.count() > 0:
            image_input.fill(image)

    def fill_domain(self) -> None:
        """Accept defaults on the domain step (just advance without changes)."""
        # Domain step typically has defaults; nothing to fill for basic flow.

    def upload_attachment(self, attachment_id: str, file_path: str, timeout: float = 10000) -> None:
        """On the Bijlagen (attachments) step: stage a file via the out-of-band upload control.

        Waits until the staged-list fragment shows the uploaded id.
        """
        self.page.locator("#wiz-att-id").fill(attachment_id)
        self.page.locator("#wiz-att-file").set_input_files(file_path)
        self.page.locator("button:has-text('Uploaden')").first.click()
        self.page.wait_for_selector(f"#wiz-att-list:has-text('{attachment_id}')", timeout=timeout)

    def screenshot(self, name: str, directory: Path) -> Path:
        """Take a full-page screenshot and save it.

        Args:
            name: Screenshot filename (without extension).
            directory: Directory to save the screenshot in.

        Returns:
            Path to the saved screenshot file.
        """
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{name}.png"
        self.page.screenshot(path=str(path), full_page=True)
        return path

    #: De stappen in de stappenbalk, in beide vormgevingen.
    #:
    #: Hier stond ook ``nav li``, en dat was in de LOTC-schil de ZIJKOLOM: de uitkomst was
    #: "Dashboard, Projecten, Beheer..." in plaats van de stappen van de wizard, en dan
    #: faalt een test op iets dat niets met de wizard te maken heeft. Alleen de balk zelf
    #: dus, in beide vormen.
    STEP_ITEMS = "#wizard-steps li, nldd-step-indicator-item"

    def get_visible_step_titles(self) -> list[str]:
        """Return all visible step titles from the step indicator."""
        steps = self.page.locator(self.STEP_ITEMS)
        titles = []
        for i in range(steps.count()):
            # In de LOTC-weergave staat het label in het text-attribuut; de tekstinhoud
            # zit in de shadow root en is leeg.
            text = steps.nth(i).get_attribute("text") or steps.nth(i).text_content()
            if text:
                titles.append(text.strip())
        return titles

    def wait_for_htmx_quiet(self, quiet_ms: int = 400) -> None:
        """Wacht tot er geen htmx-swap meer landt; zie helpers/htmx.py."""
        wait_for_htmx_quiet(self.page, quiet_ms)

    def _click_and_wait_for_step_change(self, locator, timeout: float = 10000) -> None:
        """Click a button and wait for the HTMX swap to complete.

        Uses HTMX request tracking to detect when the swap is done, rather than
        DOM mutation detection which can race with other page updates.
        """
        # Eerst laten uitrazen wat er al hangt, anders vervangt die swap de knop
        # onder onze klik vandaan en vertrekt er niets om op te wachten.
        self.wait_for_htmx_quiet()

        # Install a one-shot listener that sets a flag when HTMX finishes settling
        self.page.evaluate("""() => {
            window.__htmxSettled = false;
            document.addEventListener('htmx:afterSettle', function handler() {
                window.__htmxSettled = true;
                document.removeEventListener('htmx:afterSettle', handler);
            });
        }""")

        locator.click()

        # Wait for HTMX to settle (swap complete)
        self.page.wait_for_function(
            "() => window.__htmxSettled === true",
            timeout=timeout,
        )
