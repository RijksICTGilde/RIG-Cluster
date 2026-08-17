"""
Shared project-lifecycle helpers for sandbox E2E tests.

Creating a project through the real wizard (and discovering its technical name,
API key and first deployment) is needed by both the lifecycle suite
(test_sandbox_flows.py) and the real-life suite (test_sandbox_reallife.py), so
the logic lives here instead of in one test module.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from playwright.sync_api import expect
from tests.e2e.helpers import cluster, sandbox_api
from tests.e2e.helpers import wizard as wizard_helpers
from tests.e2e.helpers.wizard import WizardHelper

if TYPE_CHECKING:
    from playwright.sync_api import Page
    from tests.e2e.helpers.forgejo import ForgejoClient

# The E2E test workload image. The platform forces a hard non-root securityContext on
# every component pod: runAsUser: 1001, runAsNonRoot, drop ALL capabilities, and the
# generated probe is a tcpSocket on port 8080. So the image MUST run cleanly as an
# ARBITRARY UID (1001) and listen on TCP 8080, or the pod CrashLoopBackOffs and never
# becomes Healthy -- which makes create tests wait out wait_for_project_apps_healthy
# (~4 min) and the non-force UI delete drag on ArgoCD teardown.
#
# Use the purpose-built e2e-allservices workload: a tiny static Go binary built to
# exactly this contract -- distroless-static, holds no writable state, runs fine as an
# arbitrary UID (1001) and binds :8080 immediately, so the tcpSocket probe passes at
# once. Beyond starting, it round-trips every platform service the project binds it to
# and exposes the verdict at /status, which the all-services suite asserts on. It skips
# absent services, so it is a safe drop-in for the service-less lifecycle tests too. Do
# NOT switch to a stock nginx image like nginxinc/nginx-unprivileged: that one pins UID
# 101 and CrashLoopBackOffs when forced to 1001 (cannot write /var/cache/nginx). Source,
# design and the securityContext contract: images/e2e-allservices/ and
# features/e2e-allservices-image.md. Publish it with `task publish-e2e-allservices`.
RUNNABLE_IMAGE = "ghcr.io/minbzk/base-images/e2e-allservices:latest"

REVIEW_SUBMIT_SELECTOR = "button:has-text('Project aanmaken'), button:has-text('Indienen')"


@dataclass
class CreatedProject:
    name: str  # technical name, e.g. "e2e97-llv" (derived from display name, random postfix)
    display_name: str  # what was typed in the wizard, e.g. "e2e-97838-jdwm"
    api_key: str
    deployment_name: str


def on_review(page: Page) -> bool:
    """The review page is reached when the final submit button is present."""
    return page.locator(REVIEW_SUBMIT_SELECTOR).count() > 0


def walk_create_wizard(
    page: Page,
    base_url: str,
    project_name: str,
    *,
    user_email: str,
    component_name: str = "web",
    image: str = RUNNABLE_IMAGE,
) -> None:
    """Drive the create-project wizard through to the review page and submit it.

    Fills the required fields (identity, team, component) as their steps appear,
    then advances with defaults through the remaining steps (services, deployment,
    domains) until the review page is reached. The step count varies with the
    selected services, so we loop until the final submit button appears rather
    than hard-coding the number of steps.
    """
    wizard = WizardHelper(page, base_url)
    wizard.open_create_wizard()

    wizard.fill_identity(display_name=project_name, description=f"E2E lifecycle {project_name}")
    wizard.click_next()  # identity -> services
    wizard.click_next()  # services (none selected) -> team
    wizard.fill_team(email=user_email)
    wizard.click_next()  # team -> components
    wizard.fill_component(name=component_name, image=image)

    # Advance through the remaining default steps (deployment, domains, ...) until review.
    for _ in range(8):
        if on_review(page):
            break
        wizard.click_next()
    assert on_review(page), f"Did not reach the review page (stuck at {page.url})"

    body_text = page.text_content("body") or ""
    assert project_name in body_text, f"Project '{project_name}' not visible on review page"
    wizard.submit_wizard()
    assert_progress_page_is_server_rendered(page)


def assert_progress_page_is_server_rendered(page: Page) -> None:
    """The page the wizard lands on shows the task, from the server.

    This is the first thing a user sees after creating a project. It used to build its
    own HTML in JavaScript from a JSON endpoint; it now renders the shared progress
    fragment and lets htmx poll it. Skipped when the wizard did not redirect here (an
    edit flow reuses this walker), so it only measures the create landing.
    """
    if "/projects/progress/" not in page.url:
        return

    container = page.locator("#project-progress")
    container.first.wait_for(state="attached", timeout=30000)
    assert container.count() == 1, f"No server-rendered progress fragment on {page.url}"
    poll_url = container.get_attribute("hx-get") or ""
    assert poll_url.startswith("/projects/progress/"), f"Progress fragment polls elsewhere: {poll_url!r}"
    assert poll_url.endswith("/fragment"), f"Progress fragment polls elsewhere: {poll_url!r}"
    # De stapregel hoort bij de eerste paint en wordt niet later door JavaScript gevuld.
    #
    # Op de KLASSE ".edit-progress-step" zoeken kan niet meer: die hoorde bij het oude
    # fragment. De hertekende pagina (bg/_task-progress.html.j2) zet de stap in een
    # <c-paragraph> en de voortgang in een <c-progress-bar>, dus deze regel wachtte 30
    # seconden op een element dat niet meer bestaat - twintig keer in een sandboxrun, en
    # elke keer nadat het project gewoon was aangemaakt. Wat de poort moet bewaken is
    # ongewijzigd: er staat een voortgangsbalk mét een waarde, van de server.
    balk = container.locator("[data-lotc-component='progress-bar'], progress, nldd-progress-bar").first
    balk.wait_for(state="attached", timeout=30000)
    tekst = (container.text_content() or "").strip()
    assert tekst, "Progress page rendered without any progress text"


# The services a user can select in the CREATE wizard (hidden types -
# namespace-postgresql-database, namespace-redis, platform - are added via the
# edit flow instead). authorization-wall auto-pulls publish-on-web + keycloak.
ALL_CREATE_WIZARD_SERVICES = [
    "publish-on-web",
    "keycloak",
    "authorization-wall",
    "metrics-scraper",
    "persistent-storage",
    "temp-storage",
    "postgresql-database",
    "minio-storage",
    "redis",
    "attachments",
]


def _check_service_cards(page: Page, services: list[str]) -> None:
    """Tick each service card on the SERVICES step, idempotently.

    Dependency cards (publish-on-web/keycloak pulled in by authorization-wall)
    render locked+checked; those are left as-is. A card is only clicked when its
    checkbox is not already checked, so a locked-checked card is never toggled off.
    """
    for service in services:
        checkbox = page.locator(f"input[name='services[]'][value='{service}']").first
        assert checkbox.count() > 0, f"Service card '{service}' not found on the services step"
        try:
            already = checkbox.is_checked()
        except Exception:
            already = False
        if not already:
            page.locator(f"[data-service='{service}']").first.click()


def _fill_service_config_step(page: Page, *, banner: str) -> None:
    """Fill required/desired fields on whichever service-config step is showing.

    Detects the step by the presence of its fields, so it is order-independent:
    - keycloak config: enable restrict-access (auth-wall submit requires it) and
      leave the template at its default (sso-support).
    - authorization-wall config: set the banner text.
    """
    # Op het VAKJE en op het EINDE van het pad. Hier stond het oude, niet-virtuele pad
    # ("services/keycloak/config/...") en dat matcht niets meer sinds de dienstconfig onder
    # "_services-config/" wordt verstuurd, dus deze stap werd stilzwijgend overgeslagen -
    # terwijl de auth-wall-stap juist eist dat hij aanstaat. En op het vakje, want
    # is_checked() op het custom element is een harde fout.
    restrict = wizard_helpers.aanvinkvakje_eindigend_op(page, "restrict-access/enabled")
    if restrict.count() > 0:
        wizard_helpers.zet_aan(restrict.first, True)

    banner_field = wizard_helpers.veldbesturing_eindigend_op(page, "authorization-wall/config/banner")
    if banner_field.count() > 0:
        banner_field.first.fill(banner)


def _fill_component_identity(page: Page, component_name: str, image: str) -> None:
    """Fill component 0's required name + image, surviving the step's late re-render.

    The components step finishes initializing a moment AFTER htmx:afterSettle (it is
    a large services-heavy form). Filling too early gets clobbered by that init
    render, which also leaves the "Volgende" button disabled. So we wait for the
    network to settle, then fill and re-fill until both values stick, targeting the
    exact fields (the fuzzy `[name*='name']` selector also matches storage sub-fields).
    """
    # Via veldbesturing() en niet via [name='...']: onder NLDD pakt een naam-selector de
    # web-component-WIKKEL, en fill() daarop is een harde fout ("Element is not an
    # <input>") in plaats van een veld dat niet gevuld raakt.
    name_field = wizard_helpers.veldbesturing(page, "components[0]/name").first
    image_field = wizard_helpers.veldbesturing(page, "components[0]/image").first
    next_button = page.locator(
        ".wizard-step__actions button[type='submit'], .lotc-action-group button[type='submit']"
    ).first
    name_field.wait_for(state="visible", timeout=10000)
    page.wait_for_load_state("networkidle")
    # Loop until BOTH the values stick AND the Next button is enabled. The button is
    # disabled until client-side validation accepts the required fields; clicking it
    # early leaves the wizard on the components step. Each iteration waits on an EVENT
    # (the button becoming enabled) rather than a fixed sleep: a late re-render that
    # clobbers the fields also disables the button, so to_be_enabled times out and we
    # refill and retry.
    for _ in range(6):
        name_field.fill(component_name)
        image_field.fill(image)
        try:
            expect(next_button).to_be_enabled(timeout=3000)
        except AssertionError:
            continue  # re-render clobbered the values / disabled Next -> refill
        if (name_field.input_value() or "") == component_name and (image_field.input_value() or "") == image:
            return
    raise AssertionError("component name/image did not settle (values stuck / Next enabled) on the components step")


def walk_create_wizard_with_services(
    page: Page,
    base_url: str,
    project_name: str,
    *,
    user_email: str,
    services: list[str],
    component_name: str = "web",
    image: str = RUNNABLE_IMAGE,
    banner: str = "E2E all-services access banner",
) -> None:
    """Drive the create wizard with a set of services selected, through to submit.

    Extends the plain walk with service selection plus the conditional service
    config steps (keycloak, auth-wall) and the attachments step. Fills each step's
    known fields as it appears and advances until the review page, so it tolerates
    the variable step count that the selected services produce. Component-level
    service config (storage name/mount-path) keeps its pre-filled defaults.
    """
    wizard = WizardHelper(page, base_url)
    wizard.open_create_wizard()

    wizard.fill_identity(display_name=project_name, description=f"E2E all-services {project_name}")
    wizard.click_next()  # identity -> services

    _check_service_cards(page, services)
    wizard.click_next()  # services -> first config/team step

    for _ in range(12):
        if on_review(page):
            break
        _fill_service_config_step(page, banner=banner)
        # Fill the team email if this is the team step and it is still empty.
        #
        # Via wizard.field() en niet via een eigen [name='...']-locator: onder NLDD is een
        # veld een web-component (<nldd-text-field name="users[0]/email">) met het echte
        # <input> erbinnen. Een selector op alleen de naam pakt dan de WIKKEL, en
        # input_value() daarop is geen leeg veld maar een harde fout ("Node is not an
        # <input>"), waarna de wizard nooit voorbij de teamstap komt. wizard.field() zoekt
        # de besturing zelf op, en het is dezelfde locator die fill_team() gebruikt - lezen
        # en schrijven horen naar hetzelfde element te wijzen.
        email_field = wizard.field("users[0]/email")
        if email_field.count() > 0 and (email_field.first.input_value() or "") == "":
            wizard.fill_team(email=user_email)
        # The components step is a large HTMX-swapped form; its required name/image
        # fields must be filled (and verified) before advancing, else the step
        # re-renders with a "field required" error and never progresses.
        if page.locator("[name='components[0]/name']").count() > 0:
            _fill_component_identity(page, component_name, image)
        wizard.click_next()

    assert on_review(page), f"Did not reach the review page (stuck at {page.url})"
    body_text = page.text_content("body") or ""
    assert project_name in body_text, f"Project '{project_name}' not visible on review page"
    wizard.submit_wizard()


def create_project_with_services(
    page: Page,
    base_url: str,
    forgejo: ForgejoClient,
    display_name: str,
    *,
    user_email: str,
    services: list[str],
    component_name: str = "web",
    image: str = RUNNABLE_IMAGE,
    create_timeout: float = 240.0,
) -> CreatedProject:
    """Create a project through the wizard with `services` selected; resolve its identity."""
    walk_create_wizard_with_services(
        page,
        base_url,
        display_name,
        user_email=user_email,
        services=services,
        component_name=component_name,
        image=image,
    )
    # De naam komt van de voortgangspagina en niet uit een diff van de git-listing: de taak
    # weet hem, en weet ook of het gelukt is. Zie project_name_from_progress.
    name = project_name_from_progress(page, timeout=create_timeout)
    # Het BESTAND blijft een aparte controle: dat de wizard iets aanmaakte zegt nog niet dat
    # het in zad-projects staat, en dat is wat deze suite bewaakt.
    assert name in forgejo.list_project_names(), f"'{name}' staat niet in zad-projects"
    deployment_name = forgejo.get_first_deployment_name(name)
    api_key = read_api_key_with_retry(page, base_url, name)
    # The project file appears early in the create_project task, but the task then
    # keeps running (app-of-apps refresh, waiting for the app to go Healthy). Wait
    # for that to finish before handing the project to the test: otherwise a fast
    # test that adds/deletes and tears down would delete the app mid-provision,
    # leaving create_project hung on a now-missing app and jamming the worker.
    cluster.wait_for_project_apps_healthy(name, timeout=create_timeout)
    return CreatedProject(name=name, display_name=display_name, api_key=api_key, deployment_name=deployment_name)


def project_name_from_progress(page: Page, *, timeout: float = 600.0) -> str:
    """Wacht tot de aanmaaktaak KLAAR is en lees de projectnaam waar de app hem zet.

    DIT VERVANGT HET AFVISSEN VAN DE GIT-LISTING. De wizard leverde een taak op die zowel
    de UITKOMST als de NAAM kent, en dat antwoord werd weggegooid; daarna polde de oude
    helper ``ForgejoClient.wait_for_new_project`` tot 240 seconden lang de listing tot er een bestand
    opdook dat er eerst niet was, om de naam daaruit te RADEN. Dat is twee keer gokken -
    over de tijd en over de uitkomst - en het faalt op de verkeerde manier: bij RC-108
    meldde het "No new project file appeared" terwijl het project gewoon was aangemaakt,
    in 47 seconden. Een time-out werd zo een verzonnen mislukking.

    De wizard komt uit op ``/projects/progress/<task_id>``. Die pagina toont pas bij een
    afgeronde OF gefaalde taak de knop naar ``/projects/<naam>/details``
    (``_task-progress.html.j2``: ``status in ("completed", "failed")``). Daarop wachten is
    wachten op de TOESTAND: hij verschijnt precies wanneer de taak klaar is en draagt de
    naam. De ``timeout`` hier is dus een vangnet ("er is iets mis als het zo lang duurt")
    en geen wachtmechanisme.

    Faalt de taak, dan zegt deze functie DAT, in plaats van het als een uitgelopen klok te
    presenteren.
    """
    knop = page.locator("a[href*='/details'], [onclick*='/details']").first
    knop.wait_for(state="attached", timeout=timeout * 1000)
    doel = knop.get_attribute("href") or knop.get_attribute("onclick") or ""
    treffer = re.search(r"/projects/([^/'\"]+)/details", doel)
    assert treffer, f"de voortgangspagina wijst niet naar een project: {doel!r}"
    naam = treffer.group(1)
    # De uitkomst staat in een ATTRIBUUT en niet in de tekst van de pagina. Het sjabloon zet
    # bij status "completed" een success-alert en bij "failed" een error-alert, maar LOTC
    # rendert dat als <nldd-banner variant="..." text="...">: de melding staat in de
    # shadow DOM, dus ``inner_text`` levert er niets van op. Een eerste versie toetste op de
    # woorden "mislukt"/"succesvol" in de paginatekst en sloeg daardoor alarm op een
    # geslaagde aanmaak - gemeten: OPI meldde "completed successfully (50.50s)" terwijl de
    # test zei dat het faalde. Vandaar het attribuut, en dat is meteen de nauwkeurige bron:
    # een mislukking komt eruit als een MISLUKKING met zijn eigen melding, niet als een
    # uitgelopen wachttijd.
    alert = page.locator("#project-progress [data-lotc-component='alert']").first
    variant = (alert.get_attribute("variant") or "") if alert.count() else ""
    melding = (alert.get_attribute("text") or "") if alert.count() else ""
    assert variant == "success", f"het aanmaken van '{naam}' is niet geslaagd (variant={variant!r}): {melding}"
    return naam


def read_api_key_with_retry(page: Page, base_url: str, project_name: str, *, attempts: int = 20) -> str:
    """Read the API key from the details page, retrying while the project loads into memory."""
    last_error: Exception | None = None
    for _ in range(attempts):
        try:
            return sandbox_api.read_api_key(page, base_url, project_name)
        except Exception as exc:
            last_error = exc
            time.sleep(3.0)
    raise AssertionError(f"Could not read API key for '{project_name}': {last_error}")


def create_project_via_wizard(
    page: Page,
    base_url: str,
    forgejo: ForgejoClient,
    display_name: str,
    *,
    user_email: str,
    component_name: str = "web",
    image: str = RUNNABLE_IMAGE,
    create_timeout: float = 240.0,
) -> CreatedProject:
    """Create one project through the real wizard and resolve its identity.

    The technical name has a random postfix, so it is discovered by diffing the
    Forgejo repo listing before/after. Returns the project with its API key and
    first deployment name.
    """
    walk_create_wizard(page, base_url, display_name, user_email=user_email, component_name=component_name, image=image)

    # Zie project_name_from_progress: de taak weet de naam en de uitkomst, dus die vragen we
    # in plaats van de git-listing af te vissen op een klok.
    name = project_name_from_progress(page, timeout=create_timeout)
    assert name in forgejo.list_project_names(), f"'{name}' staat niet in zad-projects"

    deployment_name = forgejo.get_first_deployment_name(name)
    api_key = read_api_key_with_retry(page, base_url, name)
    # Wait out the rest of the create_project task (app-of-apps refresh, app going
    # Healthy) before handing the project over, so a fast add/delete + teardown does
    # not race the still-running create and delete the app from under it. See
    # create_project_with_services for the full rationale.
    cluster.wait_for_project_apps_healthy(name, timeout=create_timeout)
    return CreatedProject(name=name, display_name=display_name, api_key=api_key, deployment_name=deployment_name)
