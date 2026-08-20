"""Live sandbox E2E for the ``vlam`` service (RC-142).

The unit tests measure the contribution; this measures the road from a user's click to a
running pod. Three assertions, in the order a consumer would notice them failing:

1. The card is on the services step at all -- it only appears where the cluster
   configuration knows a VLAM endpoint, so on a cluster without one this whole service is
   silently unreachable and no unit test would say so.
2. The project file records the service, and the generated Deployment carries
   ``VLAM_API_URL``.
3. The RUNNING pod received that variable. A manifest is not a pod: the value is injected
   once at container start, so reading it out of ``/proc/1/environ`` is the only proof
   that the deployed container actually has it.
4. The address ANSWERS from inside a consumer pod (RC-144). Everything above measures
   wiring; this measures the chain -- selected service, injected address, egress policy on
   the consumer, inbound policy on the proxy, an answer coming back.
5. And the counter-measurement without which the one above proves nothing: a pod in a
   project WITHOUT the service does not get through to the same address. The tenant
   baseline only opens 80/443 outbound, so this is what the service actually adds.

The sandbox has no VLAM upstream and no RON: its cluster configuration carries a
placeholder endpoint. Since RC-144 the suite puts a STUB on exactly those coordinates
(``tests/e2e/helpers/vlam_stub.py``) so 4 and 5 have something to answer them; reaching
the real VLAM is only testable on production, and that is the acceptance test in the plan,
not this one.

Skips when E2E_BASE_URL is unset. Requires YOUR build on the sandbox.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest
from opi.services.catalog.vlam.endpoint import vlam_endpoint
from opi.services.services_enums import ServiceType
from tests.e2e.conftest import FORGEJO_VERIFY_SSL, SANDBOX_TEST_USER
from tests.e2e.helpers import cluster, sandbox_api, vlam_stub
from tests.e2e.helpers.lifecycle import CreatedProject, create_project_via_wizard, create_project_with_services
from tests.e2e.helpers.wizard import WizardHelper, unique_project_name

if TYPE_CHECKING:
    from collections.abc import Generator

    from opi.services.catalog.vlam.endpoint import VlamEndpoint
    from playwright.sync_api import BrowserContext
    from tests.e2e.helpers.forgejo import ForgejoClient

logger = logging.getLogger(__name__)

pytestmark = [pytest.mark.e2e, pytest.mark.sandbox]

_VERIFY_SSL = FORGEJO_VERIFY_SSL
_SERVICE = ServiceType.VLAM.value
#: The sandbox is what an OPI on the sandbox manages, so it is the cluster whose
#: configuration decides what this suite may expect.
_CLUSTER = "sandboxed-local"


@pytest.fixture(scope="module")
def vlam_project(
    sandbox_context: BrowserContext,
    sandbox_url: str,
    forgejo: ForgejoClient,
) -> Generator[CreatedProject]:
    """A project with only the vlam service, cleaned up afterwards."""
    page = sandbox_context.new_page()
    created: CreatedProject | None = None
    try:
        created = create_project_with_services(
            page,
            sandbox_url,
            forgejo,
            unique_project_name(prefix="vlam"),
            user_email=SANDBOX_TEST_USER["email"],
            services=[_SERVICE],
        )
        logger.info("Created vlam project: %s", created.name)
        yield created
    finally:
        page.close()
        if created is not None:
            sandbox_api.delete_project_via_api(sandbox_url, created.name, created.api_key, verify_ssl=_VERIFY_SSL)


@pytest.mark.timeout(300)
def test_the_card_is_on_the_services_step(sandbox_context: BrowserContext, sandbox_url: str) -> None:
    """Without a card there is no road for a user, however complete the code is."""
    page = sandbox_context.new_page()
    try:
        wizard = WizardHelper(page, sandbox_url)
        wizard.open_create_wizard()
        wizard.fill_identity(display_name=unique_project_name(prefix="vlamcard"), description="vlam card e2e")
        wizard.click_next()
        assert page.locator(f"input[name='services[]'][value='{_SERVICE}']").count() > 0, (
            f"de kaart voor '{_SERVICE}' staat niet op de dienstenstap"
        )
    finally:
        page.close()


@pytest.mark.timeout(600)
def test_the_project_file_records_the_service(vlam_project: CreatedProject, forgejo: ForgejoClient) -> None:
    data = forgejo.get_project_yaml(vlam_project.name)
    assert data is not None, f"projectbestand voor '{vlam_project.name}' ontbreekt in zad-projects"
    names = [
        entry if isinstance(entry, str) else (entry.get("name") or entry.get("reference"))
        for entry in data.get("services") or []
    ]
    assert _SERVICE in names, f"'{_SERVICE}' staat niet in het projectbestand: {names}"


@pytest.mark.timeout(600)
def test_the_running_pod_received_the_address(vlam_project: CreatedProject) -> None:
    """The measurement that counts: the variable as the running process received it."""
    if not cluster.kubectl_available():
        pytest.skip("kubectl niet beschikbaar; deze meting hoort op de sandboxmachine")
    endpoint = vlam_endpoint(_CLUSTER)
    assert endpoint is not None, f"cluster '{_CLUSTER}' kent geen VLAM-endpoint"

    namespaces = cluster._project_namespaces(vlam_project.name)
    assert namespaces, f"geen namespace voor project '{vlam_project.name}'"
    namespace = namespaces[0]

    assert cluster.wait_for(
        lambda: bool(cluster.running_pod_names(namespace, vlam_project.deployment_name)),
        timeout=420,
        interval=5,
    ), f"geen draaiende pod in {namespace}"
    pod = cluster.running_pod_names(namespace, vlam_project.deployment_name)[0]

    value = cluster.env_in_pod(namespace, pod, "VLAM_API_URL", probe="rc142")
    assert value == endpoint.api_url, f"pod kreeg VLAM_API_URL={value!r}, verwacht {endpoint.api_url!r}"


@pytest.mark.timeout(600)
def test_the_egress_policy_opens_only_the_proxy(vlam_project: CreatedProject) -> None:
    """One policy for the deployment, egress only, pinned to the proxy pod."""
    if not cluster.kubectl_available():
        pytest.skip("kubectl niet beschikbaar; deze meting hoort op de sandboxmachine")
    endpoint = vlam_endpoint(_CLUSTER)
    assert endpoint is not None

    namespaces = cluster._project_namespaces(vlam_project.name)
    assert namespaces, f"geen namespace voor project '{vlam_project.name}'"
    policies = [
        item
        for item in cluster.get_json("networkpolicies", namespaces[0]).get("items", [])
        if _SERVICE in item["metadata"]["name"]
    ]
    assert len(policies) == 1, f"verwacht een vlam-netwerkregel, gevonden: {[p['metadata']['name'] for p in policies]}"

    spec = policies[0]["spec"]
    assert spec["policyTypes"] == ["Egress"]
    assert spec["podSelector"]["matchLabels"]["deployment"] == vlam_project.deployment_name
    peer = spec["egress"][0]["to"][0]
    assert peer["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"] == endpoint.namespace
    assert peer["podSelector"]["matchLabels"] == endpoint.pod_labels
    assert [port["port"] for port in spec["egress"][0]["ports"]] == [endpoint.port]


@pytest.fixture(scope="module")
def stub_endpoint() -> Generator[VlamEndpoint]:
    """The placeholder coordinates, made real by a stub for the length of this module."""
    if not cluster.kubectl_available():
        pytest.skip("kubectl niet beschikbaar; deze meting hoort op de sandboxmachine")
    endpoint = vlam_endpoint(_CLUSTER)
    assert endpoint is not None, f"cluster '{_CLUSTER}' kent geen VLAM-endpoint"
    vlam_stub.ensure(_CLUSTER, endpoint)
    try:
        yield endpoint
    finally:
        vlam_stub.remove(endpoint)


@pytest.fixture(scope="module")
def project_without_vlam(
    sandbox_context: BrowserContext,
    sandbox_url: str,
    forgejo: ForgejoClient,
) -> Generator[CreatedProject]:
    """A project with no services at all: the counter-example for the egress measurement."""
    page = sandbox_context.new_page()
    created: CreatedProject | None = None
    try:
        created = create_project_via_wizard(
            page,
            sandbox_url,
            forgejo,
            unique_project_name(prefix="zvlam"),
            user_email=SANDBOX_TEST_USER["email"],
        )
        logger.info("Created project without vlam: %s", created.name)
        yield created
    finally:
        page.close()
        if created is not None:
            sandbox_api.delete_project_via_api(sandbox_url, created.name, created.api_key, verify_ssl=_VERIFY_SSL)


def _first_running_pod(project: CreatedProject) -> tuple[str, str]:
    """The namespace and one running application pod of a created project."""
    namespaces = cluster._project_namespaces(project.name)
    assert namespaces, f"geen namespace voor project '{project.name}'"
    namespace = namespaces[0]
    assert cluster.wait_for(
        lambda: bool(cluster.running_pod_names(namespace, project.deployment_name)),
        timeout=420,
        interval=5,
    ), f"geen draaiende pod in {namespace}"
    return namespace, cluster.running_pod_names(namespace, project.deployment_name)[0]


def _call_models(namespace: str, pod: str, api_url: str, *, probe: str) -> str:
    """GET {api_url}/v1/models from inside the pod's own network namespace.

    Via an ephemeral debug container, for the reason ``cluster.probe_in_pod`` explains: the
    workload image is distroless and carries no shell. What matters here is that an
    ephemeral container shares the POD's network namespace, and a NetworkPolicy selects
    pods -- so this call is subject to exactly the rules the platform generated for this
    deployment, which is the whole point of the measurement.
    """
    script = f'wget -q -T 8 -O - "{api_url}/v1/models" || echo "{_NO_ANSWER}"'
    return cluster.probe_in_pod(namespace, pod, script, probe=probe) or ""


#: What the probe prints when wget got nothing back (a refusal or, with egress closed, a
#: timeout). A marker rather than an exit code, because the log is all that comes back.
_NO_ANSWER = "GEEN-ANTWOORD"


@pytest.mark.timeout(900)
def test_the_consumer_reaches_the_models_endpoint(vlam_project: CreatedProject, stub_endpoint: VlamEndpoint) -> None:
    """The measurement this whole service exists for: the address answers from the pod."""
    namespace, pod = _first_running_pod(vlam_project)

    answer = _call_models(namespace, pod, stub_endpoint.api_url, probe="rc144-models")

    assert _NO_ANSWER not in answer, (
        f"de afnemer kreeg geen antwoord van {stub_endpoint.api_url}/v1/models. "
        "Verdacht: de uitgaande netwerkregel van de dienst, de inkomende regel op de stub, "
        f"of de stub zelf. Uitvoer: {answer!r}"
    )
    assert vlam_stub.STUB_MODEL_ID in answer, (
        f"antwoord van {stub_endpoint.api_url}/v1/models bevat de stub-modellen niet: {answer!r}"
    )


@pytest.fixture(scope="module")
def policies_are_enforced(vlam_project: CreatedProject, stub_endpoint: VlamEndpoint) -> bool:
    """Whether THIS cluster enforces NetworkPolicies -- measured on the stub, not assumed.

    The negative measurement below only says something on a cluster that actually enforces
    policies. Two of this repo's own documents disagreed about whether the sandbox does, so
    the suite settles it itself: take the open rule away and see whether the consumer that
    DOES have the service still gets through. If it does, nothing is being enforced here.
    If it does not, that open rule is demonstrably what holds the door.
    """
    namespace, pod = _first_running_pod(vlam_project)
    answer = vlam_stub.without_the_open_rule(
        _CLUSTER,
        stub_endpoint,
        lambda: _call_models(namespace, pod, stub_endpoint.api_url, probe="rc144-zonderregel"),
    )
    enforced = vlam_stub.STUB_MODEL_ID not in answer
    logger.info("NetworkPolicy-handhaving op %s: %s (uitvoer zonder de open regel: %r)", _CLUSTER, enforced, answer)
    return enforced


@pytest.mark.timeout(900)
def test_a_project_without_the_service_does_not_get_through(
    project_without_vlam: CreatedProject, stub_endpoint: VlamEndpoint, policies_are_enforced: bool
) -> None:
    """Without the service there is no road: the tenant baseline only opens 80 and 443.

    This is the half that makes the positive measurement mean something -- but only on a
    cluster that enforces NetworkPolicies, which is why that is measured first rather than
    taken for granted.
    """
    if not policies_are_enforced:
        pytest.skip(
            "dit cluster handhaaft geen NetworkPolicies: met de open regel weggehaald kwam de "
            "afnemer er nog steeds doorheen. Dat de uitgaande regel bestaat en precies de proxy "
            "noemt staat in test_the_egress_policy_opens_only_the_proxy; dat hij ook BLOKKEERT is "
            "alleen vast te stellen op een cluster met een handhavende CNI (ODCN/Calico)."
        )
    namespace, pod = _first_running_pod(project_without_vlam)

    answer = _call_models(namespace, pod, stub_endpoint.api_url, probe="rc144-dicht")

    assert vlam_stub.STUB_MODEL_ID not in answer, (
        f"een project ZONDER de vlam-dienst bereikte {stub_endpoint.api_url}/v1/models, terwijl dit "
        "cluster NetworkPolicies wel handhaaft. De uitgaande regel is dan niet wat poort 8081 opent. "
        f"Uitvoer: {answer!r}"
    )
    assert _NO_ANSWER in answer, f"verwacht dat de aanroep vastloopt zonder de dienst, maar de probe zei: {answer!r}"
