"""The whole chain, driven by hand through the browser: wizard -> project file -> NetworkPolicy.

``tests/test_cross_domain_chain.py`` proves the same chain from the form processor down. It
does not prove the one thing this service kept failing on: that a human can actually fill the
step in. Three required fields hung on selects that were empty in the create wizard, so the
step could not be completed at all -- and every downstream test still passed, because none of
them started at the page.

This test starts at the page. It clicks through the real create wizard, adds a rule, picks the
peer from the cascading selects (the peer deployment and component lists only exist AFTER a
project is chosen, so a test that can fill them is also the proof that the cascade re-renders),
submits, and takes the project YAML the wizard hands to the create task. That YAML then goes
through ``contribute_deployment_manifests`` and the real Jinja template, and what comes out has
to be a NetworkPolicy naming the peer.

One project is created here; the rule points at a second project by name. That second project
does not have to exist for the policy to be generated -- see the last test -- because naming a
peer grants it nothing: the receiving side decides with its own policy what it lets in.

Run: uv run pytest tests/e2e/test_wizard_cross_domain_policy.py -m "e2e and not sandbox" -q
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import pytest
import yaml
from opi.services.catalog.base import DeploymentManifestContext
from opi.services.registry import get_service
from opi.services.services_enums import ServiceType
from playwright.sync_api import Error as PlaywrightError
from tests.e2e.helpers.htmx import wait_for_htmx_quiet
from tests.e2e.helpers.tekst import veld
from tests.e2e.helpers.wizard import WizardHelper, unique_project_name

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = [pytest.mark.e2e]

SERVICE = ServiceType.CROSS_DOMAIN_ACCESS.value
STEP = f"{SERVICE}-config"
FIELD = f"_services-config/{SERVICE}/config/inbound[0]"

#: A seeded fixture project (tests/e2e/fixtures/projects/test-project.yaml): one component
#: ``frontend``, one deployment ``default`` on the managed cluster.
PEER_PROJECT = "test-project"
PEER_DEPLOYMENT = "default"
PEER_COMPONENT = "frontend"

OWN_COMPONENT = "web"
OWN_PORT = "8080"

_MAX_WIZARD_STEPS = 15

#: Vangnet voor het wachten op een cascadeveld, geen wachtmechanisme: elke wachtregel
#: hieronder keert terug zodra de voorwaarde waar is. Elke pick is een VOLLEDIGE
#: heen-en-weer met de server (de stap wordt server-side opnieuw gerenderd), en op een
#: bezette machine duurt die ronde seconden in plaats van milliseconden. Op tien seconden
#: liep dat vangnet af terwijl er niets stuk was -- dat is precies het rood dat deze suite
#: wisselvallig maakte. Dertig seconden kost niets zolang het groen is.
_SELECT_TIMEOUT_MS = 30_000


@pytest.fixture
def captured_yaml(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The project YAML the wizard hands to the create task -- the artifact, not the page."""
    captured: list[str] = []

    import opi.core.task_helpers as task_helpers

    async def _fake_create_async_task(**kwargs: Any) -> dict[str, Any]:
        captured.append(kwargs["payload"]["yaml_content"])
        return {"task_id": "00000000-0000-0000-0000-000000000000"}

    monkeypatch.setattr(task_helpers, "create_async_task", _fake_create_async_task)
    return captured


def _step(page: Page) -> str:
    return page.url.rsplit("/step/", 1)[-1]


def _walk(wizard: WizardHelper, page: Page, until: str | None) -> None:
    """Click through the wizard until *until* (or the review page), filling what blocks it.

    Never a fixed number of clicks: every selected service inserts its own config step, so
    counting them breaks silently the moment the step list changes.
    """
    for _ in range(_MAX_WIZARD_STEPS):
        if until is None and page.locator("#wizard-review-submit").count() > 0:
            return
        if until is not None and _step(page) == until:
            return
        if _step(page) == "team":
            wizard.fill_team(email="test@example.com")
        elif _step(page) == "components":
            wizard.fill_component(name=OWN_COMPONENT, image="nginx:1.25")
        wizard.click_next()
        page.wait_for_load_state("networkidle")
    raise AssertionError(f"{until or 'review'} not reached within {_MAX_WIZARD_STEPS} steps; stuck at {page.url}")


def _inbound_field_names(page: Page) -> list[str]:
    """Every form control on the page that belongs to an inbound rule of this step."""
    return page.evaluate(
        "() => [...document.querySelectorAll('[name]')]"
        ".map(e => e.getAttribute('name'))"
        ".filter(n => n && n.includes('/config/inbound['))"
    )


def _fill_inbound_rule(page: Page, name: str) -> None:
    """Add one inbound rule and fill every field of it, cascading select by select."""
    # De isolatiepoort. Deze test schrijft alles onder ``inbound[0]``, dus als een vorige
    # test hier een regel had achtergelaten, zou "Item toevoegen" ``inbound[1]`` opleveren
    # en zouden alle asserties hieronder op de VERKEERDE regel kijken. Dat was de gestelde
    # (en niet gemeten) oorzaak van de xfail die hier stond. Nu zegt het rood het zelf, in
    # plaats van als een raadselachtige waardefout te verschijnen.
    achtergebleven = _inbound_field_names(page)
    assert not achtergebleven, (
        f"de stap begint niet leeg -- er staat al een regel van een vorige wizardgang: {achtergebleven}"
    )

    page.locator("button:has-text('Item toevoegen')").first.click()
    # Wait for the row that the click adds, not for a fixed 800 ms: filling a field that
    # is not there yet is the failure this used to produce on a busy machine.
    veld(page, f"{FIELD}/name").wait_for(state="visible", timeout=_SELECT_TIMEOUT_MS)

    veld(page, f"{FIELD}/name").fill(name)
    # Choosing the peer project is what makes the next two lists exist at all. Every one of
    # these selects re-renders the row server-side, so each pick has to wait for that render
    # to land before the next one -- picking into a select that is about to be replaced loses
    # the value and leaves the step with an empty required field.
    _select_when_offered(page, f"{FIELD}/from/project", PEER_PROJECT)
    _select_when_offered(page, f"{FIELD}/from/deployment", PEER_DEPLOYMENT)
    _select_when_offered(page, f"{FIELD}/from/component", PEER_COMPONENT)
    _select_when_offered(page, f"{FIELD}/to/component", OWN_COMPONENT)
    _select_when_offered(page, f"{FIELD}/to/port", OWN_PORT)

    filled = {
        field: veld(page, f"{FIELD}/{field}").input_value()
        for field in ("name", "from/project", "from/deployment", "from/component", "to/component", "to/port")
    }
    assert all(filled.values()), f"the rule is not completely filled in: {filled}"


def _wacht(page: Page, expressie: str, field: str, value: str, waarop: str) -> None:
    """``wait_for_function`` met een melding die zegt WAAR op gewacht werd.

    Een verlopen ``wait_for_function`` levert kaal "Timeout 30000ms exceeded" met de
    JS-bron eronder: geen veld, geen waarde, geen richting. Precies die melding stond
    onder de wisselvallige runs van deze suite, en daardoor was er niet uit af te lezen
    of de lijst leeg bleef of de waarde weer verdween.
    """
    try:
        page.wait_for_function(expressie, arg=[field, value], timeout=_SELECT_TIMEOUT_MS)
    except PlaywrightError as exc:
        opties = page.evaluate(
            "(name) => { const el = document.querySelector(`select[name='${name}']`);"
            " return el ? [...el.options].map(o => o.value) : null; }",
            field,
        )
        raise AssertionError(
            f"{waarop} niet gebeurd binnen {_SELECT_TIMEOUT_MS} ms voor {field}='{value}'; "
            f"de keuzelijst biedt nu {opties}"
        ) from exc


def _select_when_offered(page: Page, field: str, value: str) -> None:
    """Wait until a dependent select actually offers *value*, pick it, and let the row settle.

    The cascade is server-side (the row's own values decide the list), so the option appears
    only after the re-render triggered by the field before it.
    """
    # De ECHTE <select> zoeken en niet getElementsByName()[0]: onder het nieuwe thema is
    # het eerste element met die naam de WIKKEL (<nldd-select-field>), en die heeft geen
    # .options. Het wachten liep dan af op iets dat nooit waar kon worden.
    _wacht(
        page,
        "([name, value]) => { const el = document.querySelector(`select[name='${name}']`);"
        " return el && [...el.options].some(o => o.value === value); }",
        field,
        value,
        waarop="de keuzelijst bood de waarde aan",
    )
    veld(page, field).select_option(value)
    # Waiting for the field to hold the value again is the signal that the swap landed;
    # without it the next pick can go into a select that is about to be replaced, and
    # both values are lost. Only shows up when the machine is busy (a loaded CI runner),
    # never when the server answers in a few milliseconds.
    #
    # A ``page.wait_for_load_state("networkidle")`` used to sit here as well. It said only
    # that the XHR was done, not that the re-rendered row was in the DOM -- and the row
    # that comes back is what carries the value -- so it added a wait without adding a
    # guarantee. The condition below is the real signal, and ``wait_for_htmx_quiet``
    # further down covers the swap itself.
    _wacht(
        page,
        "([name, value]) => { const el = document.querySelector(`select[name='${name}']`);"
        " return el && el.value === value && !el.closest('form')?.classList.contains('htmx-request'); }",
        field,
        value,
        waarop="de rij droeg de gekozen waarde weer",
    )
    # Hier stond ``page.wait_for_timeout(600)``. Gemeten (RC-125): elke cascaderende pick
    # levert precies TWEE ``htmx:afterSettle``-gebeurtenissen op, en de laatste landt
    # 9-52 ms VOOR de voorwaarde hierboven waar wordt -- ook op een machine die op een
    # kern staat te stampen. Die 600 ms dekte dus nooit iets af (wel 3 s per test), en
    # voor een swap die er langer over doet is het een gok. ``wait_for_htmx_quiet``
    # wacht op de gebeurtenis zelf en keert terug zodra het echt stil is.
    wait_for_htmx_quiet(page)


def _project_from_wizard(app_server: str, page: Page, captured: list[str], rule_name: str) -> dict[str, Any]:
    """Run the create wizard with one cross-domain rule and return the project it produced."""
    wizard = WizardHelper(page, app_server)
    wizard.open_create_wizard()
    wizard.fill_identity(display_name=unique_project_name("cda"), description="cross-domain chain")
    wizard.click_next()
    wizard.fill_services([SERVICE])
    _walk(wizard, page, until=STEP)

    _fill_inbound_rule(page, rule_name)
    wizard.click_next()
    page.wait_for_load_state("networkidle")

    _walk(wizard, page, until=None)
    wizard.submit_wizard()

    # Wait for the captured project file rather than for two seconds: the submit runs
    # server-side, and how long that takes is a property of the machine, not of the
    # behaviour under test. A submit that never produces a file still fails below.
    deadline = time.monotonic() + 30
    while not captured and time.monotonic() < deadline:
        page.wait_for_timeout(100)

    assert captured, f"the wizard created no project (stuck at {page.url})"
    project = yaml.safe_load(captured[-1])
    assert isinstance(project, dict)
    return project


def _policies(project: dict[str, Any]) -> list[dict[str, Any]]:
    """Everything the service contributes for this project's deployment, rendered as YAML."""
    from opi.generation.manifests import render_template

    deployment = project["deployments"][0]
    context = DeploymentManifestContext(
        project_name=project["name"],
        project_data=project,
        deployment=deployment,
        cluster=deployment["cluster"],
        namespace=f"rig-{deployment['namespace']}",
    )
    return [
        yaml.safe_load(render_template(spec.template_path, spec.values))
        for spec in get_service(ServiceType.CROSS_DOMAIN_ACCESS).contribute_deployment_manifests(context)
    ]


def _ingress_peers(policy: dict[str, Any]) -> list[tuple[str, dict[str, str], list[int]]]:
    return [
        (
            entry["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"],
            entry["podSelector"]["matchLabels"],
            [p["port"] for p in rule["ports"]],
        )
        for rule in policy["spec"]["ingress"]
        for entry in rule["from"]
    ]


class TestTheWizardProducesANetworkPolicy:
    def test_a_rule_filled_in_the_browser_becomes_a_networkpolicy(
        self, app_server: str, auth_page: Page, captured_yaml: list[str]
    ) -> None:
        project = _project_from_wizard(app_server, auth_page, captured_yaml, "van-test-project")

        # 1. the rule is in the project file the wizard submits, in the stored shape
        entry = next(e for e in project["services"] if isinstance(e, dict) and e.get("name") == SERVICE)
        assert entry["config"]["inbound"] == [
            {
                "name": "van-test-project",
                "from": {"project": PEER_PROJECT, "deployment": PEER_DEPLOYMENT, "component": PEER_COMPONENT},
                "to": {"component": OWN_COMPONENT, "port": int(OWN_PORT)},
            }
        ]

        # 2. + 3. and it comes out of the generator as a real NetworkPolicy for that peer
        [policy] = _policies(project)
        assert policy["kind"] == "NetworkPolicy"
        assert policy["spec"]["podSelector"]["matchLabels"]["app"].endswith(f"-{OWN_COMPONENT}")
        assert _ingress_peers(policy) == [
            (
                f"rig-{PEER_PROJECT}",
                {"app": f"{PEER_DEPLOYMENT}-{PEER_COMPONENT}", "project": PEER_PROJECT},
                [int(OWN_PORT)],
            )
        ]

    def test_a_peer_that_does_not_exist_here_still_yields_the_policy(
        self, app_server: str, auth_page: Page, captured_yaml: list[str]
    ) -> None:
        """The two-project case, from the one project that can be created here.

        The select only offers peers this instance knows, so the rule is filled in the browser
        as before and then re-pointed at a project name that does not exist -- which is the
        normal state while the other side is still being set up, or when it is managed in
        another domain. The policy must still be generated: it says which pods MAY talk, and
        the receiver decides with its own policy whether it lets them in.
        """
        project = _project_from_wizard(app_server, auth_page, captured_yaml, "van-de-buren")

        entry = next(e for e in project["services"] if isinstance(e, dict) and e.get("name") == SERVICE)
        entry["config"]["inbound"][0]["from"]["project"] = "nog-niet-aangemaakt"

        [policy] = _policies(project)
        assert _ingress_peers(policy) == [
            (
                "rig-nog-niet-aangemaakt",  # by convention: a project's namespace is its name
                {"app": f"{PEER_DEPLOYMENT}-{PEER_COMPONENT}", "project": "nog-niet-aangemaakt"},
                [int(OWN_PORT)],
            )
        ]


def test_a_second_wizard_in_the_same_browser_session_starts_without_a_rule(
    app_server: str, auth_page: Page, captured_yaml: list[str]
) -> None:
    """De isolatiebewering van dit bestand, in een test in plaats van in een xfail-reden.

    De twee tests hierboven stonden onder de aanname dat ze elkaar besmetten: "ze delen
    een browsersessie, en de eerste vult er een regel in waar de tweede mee begint".
    Gemeten (RC-125) klopt dat niet, en dit is de meting:

    Deze test doet de besmetting BEWUST -- twee volledige wizardgangen achter elkaar op
    DEZELFDE pagina, dezelfde context, dezelfde koekjespot, dus een strengere opzet dan
    de twee tests hierboven ooit hebben (die krijgen elk een verse context, want
    ``auth_page`` en ``authenticated_context`` staan per test). En de tweede gang begint
    toch met een lege regellijst.

    Dat komt niet vanzelf: de wizardstaat staat server-side onder een token in het
    sessiekoekje, en ``open_create_wizard`` haalt eerst ``/forms/wizard/restart`` op, wat
    die staat wist. Dat is de weg waarlangs de isolatie loopt, en die weg is nergens
    getoetst -- vandaar deze test: gaat de restart eruit, dan valt hij om, en niet een
    van de twee tests hierboven op een raadselachtige waardefout.
    """
    _project_from_wizard(app_server, auth_page, captured_yaml, "eerste-gang")

    wizard = WizardHelper(auth_page, app_server)
    wizard.open_create_wizard()
    wizard.fill_identity(display_name=unique_project_name("cda"), description="tweede gang")
    wizard.click_next()
    wizard.fill_services([SERVICE])
    _walk(wizard, auth_page, until=STEP)

    assert _inbound_field_names(auth_page) == [], (
        "de tweede wizardgang begint met de regel van de eerste; de wizardstaat wordt niet gewist"
    )
