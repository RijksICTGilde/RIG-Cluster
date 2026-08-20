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

The sandbox has no VLAM upstream and no RON: its cluster configuration carries a
placeholder endpoint so exactly this wiring can be walked end to end. Reaching VLAM itself
is only testable on production, and that is the acceptance test in the plan, not this one.

Skips when E2E_BASE_URL is unset. Requires YOUR build on the sandbox.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest
from opi.services.catalog.vlam.endpoint import vlam_endpoint
from opi.services.services_enums import ServiceType
from tests.e2e.conftest import FORGEJO_VERIFY_SSL, SANDBOX_TEST_USER
from tests.e2e.helpers import cluster, sandbox_api
from tests.e2e.helpers.lifecycle import CreatedProject, create_project_with_services
from tests.e2e.helpers.wizard import WizardHelper, unique_project_name

if TYPE_CHECKING:
    from collections.abc import Generator

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
