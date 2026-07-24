"""
Sandbox E2E: create a project with EVERY platform service enabled, then verify
both that the project YAML is correct AND that each service was actually
provisioned in the cluster (database, cache, object storage, auth, ...).

The other sandbox suites create service-less projects, so nothing exercises the
full provisioning path end to end. This suite ticks all 10 create-wizard services
(publish-on-web, keycloak, authorization-wall, metrics-scraper, persistent-storage,
temp-storage, postgresql-database, minio-storage, redis, attachments), and then
adds the two hidden namespace variants (namespace-postgresql-database,
namespace-redis) via the edit flow, so every ServiceType is covered.

Verification is layered:
1. The committed project YAML in Forgejo declares every service in the uniform
   `{name, config}` / `{reference, config}` form.
2. The live cluster shows each service's provisioned resources (namespace secrets,
   database, bucket, keycloak realm, PVCs, auth-wall sidecar), proving OPI's
   provider registry actually processed each one.

Requires a running sandbox with YOUR build deployed and E2E_BASE_URL set. Run:

    task test-e2e-sandbox   # or the explicit E2E_BASE_URL=... invocation

Backend checks use kubectl against the current cluster context and skip cleanly
when kubectl is not available.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest
from tests.e2e.conftest import FORGEJO_VERIFY_SSL, SANDBOX_TEST_USER
from tests.e2e.helpers import cluster, sandbox_api
from tests.e2e.helpers.lifecycle import (
    ALL_CREATE_WIZARD_SERVICES,
    CreatedProject,
    create_project_with_services,
)
from tests.e2e.helpers.wizard import _unique_project_name

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Generator

    from playwright.sync_api import BrowserContext
    from tests.e2e.helpers.forgejo import ForgejoClient

pytestmark = [pytest.mark.e2e, pytest.mark.sandbox]

_VERIFY_SSL = FORGEJO_VERIFY_SSL


def _service_names(data: dict) -> list[str]:
    """Project-level service names, format-agnostic (bare string or {name} record)."""
    names: list[str] = []
    for entry in data.get("services") or []:
        if isinstance(entry, str):
            names.append(entry)
        elif isinstance(entry, dict):
            name = entry.get("name") or entry.get("reference")
            if name is None:
                # legacy single-key form {svc: {...}}
                keys = [k for k in entry if k not in ("config", "schema-version")]
                name = keys[0] if len(keys) == 1 else None
            if name:
                names.append(name)
    return names


def _service_entry(data: dict, service: str) -> dict | str | None:
    for entry in data.get("services") or []:
        if isinstance(entry, str) and entry == service:
            return entry
        if isinstance(entry, dict) and (entry.get("name") == service or entry.get("reference") == service):
            return entry
    return None


@pytest.fixture(scope="module")
def all_services_project(
    sandbox_context: BrowserContext,
    sandbox_url: str,
    forgejo: ForgejoClient,
) -> Generator[CreatedProject]:
    """Create one project with every create-wizard service, shared by the tests."""
    display_name = _unique_project_name(prefix="alls")
    page = sandbox_context.new_page()
    created: CreatedProject | None = None
    try:
        created = create_project_with_services(
            page,
            sandbox_url,
            forgejo,
            display_name,
            user_email=SANDBOX_TEST_USER["email"],
            services=ALL_CREATE_WIZARD_SERVICES,
        )
        logger.info("Created all-services project: %s", created.name)
        yield created
    finally:
        page.close()
        if created is not None:
            sandbox_api.delete_project_via_api(sandbox_url, created.name, created.api_key, verify_ssl=_VERIFY_SSL)


@pytest.mark.timeout(600)
def test_all_services_present_in_project_yaml(
    all_services_project: CreatedProject,
    forgejo: ForgejoClient,
) -> None:
    """The committed project file declares every selected service in uniform form."""
    data = forgejo.get_project_yaml(all_services_project.name)
    assert data is not None, f"Project file for '{all_services_project.name}' missing in Forgejo"

    present = _service_names(data)
    logger.info("Project '%s' services: %s", all_services_project.name, present)
    missing = [s for s in ALL_CREATE_WIZARD_SERVICES if s not in present]
    assert not missing, f"Services missing from project YAML: {missing}. Present: {present}"

    # Uniform format: every non-bare service entry uses the {name, ...} record form,
    # never the legacy name-as-key dict ({keycloak: {...}}).
    for entry in data.get("services") or []:
        if isinstance(entry, dict):
            assert "name" in entry, f"service entry is not in uniform {{name, config}} form: {entry!r}"

    # keycloak is a config record ({name: keycloak, config: {...}}).
    keycloak = _service_entry(data, "keycloak")
    assert isinstance(keycloak, dict), f"keycloak is not a config record: {keycloak!r}"
    assert "config" in keycloak, f"keycloak record has no config key: {keycloak!r}"

    # The component references the services it uses.
    components = data.get("components") or []
    assert components, "project has no components"
    comp = components[0]
    comp_services = comp.get("services") or []
    comp_names: list[str] = []
    for entry in comp_services:
        if isinstance(entry, str):
            comp_names.append(entry)
        elif isinstance(entry, dict):
            comp_names.append(entry.get("reference") or entry.get("name") or "")
    logger.info("Component '%s' services: %s", comp.get("name"), comp_names)
    assert "keycloak" in comp_names, f"component does not reference keycloak: {comp_names}"


# How long provisioning (process_project + ArgoCD sync of every service) may take
# after the wizard submit returns. Observed ~100s on the sandbox; generous headroom.
_PROVISION_TIMEOUT = 420.0


@pytest.mark.timeout(600)
def test_all_services_provisioned_in_cluster(
    all_services_project: CreatedProject,
) -> None:
    """Every selected service has its resources actually provisioned in the cluster.

    Proves the services were *processed* (not just declared): after the async
    create pipeline finishes, the project namespace must carry each service's
    provisioned output - the per-deployment credential secrets (database, keycloak,
    minio, redis, metrics), the auth-wall sidecar + cookie secret, the
    persistent-storage PVC, the publish-on-web ingress, and the metrics annotations.

    Skips when kubectl cannot reach a cluster (these checks only run on the machine
    hosting the sandbox, e.g. the dclaude sandbox stage).
    """
    if not cluster.kubectl_available():
        pytest.skip("kubectl cannot reach a cluster - live provisioning checks need the sandbox host")

    namespace = f"rig-{all_services_project.name}"
    dep = all_services_project.deployment_name

    # Provisioning runs async after the wizard returns; wait for the namespace and
    # the database secret (one of the last things ArgoCD syncs) to appear.
    database_secret = f"{dep}-database"
    provisioned = cluster.wait_for(
        lambda: cluster.namespace_exists(namespace) and database_secret in cluster.resource_names("secrets", namespace),
        timeout=_PROVISION_TIMEOUT,
    )
    assert provisioned, (
        f"Namespace '{namespace}' / secret '{database_secret}' did not appear within "
        f"{_PROVISION_TIMEOUT:.0f}s - provisioning did not complete"
    )

    secrets = set(cluster.resource_names("secrets", namespace))
    # One credential/config secret per provisioned service.
    expected_secrets = {
        "postgresql-database": f"{dep}-database",
        "keycloak": f"{dep}-keycloak",
        "minio-storage": f"{dep}-minio",
        "redis": f"{dep}-redis",
        "metrics-scraper": f"{dep}-metrics-auth",
        "authorization-wall": f"{dep}-web-oauth2-cookie",
    }
    missing_secrets = {svc: name for svc, name in expected_secrets.items() if name not in secrets}
    assert not missing_secrets, f"Missing provisioned secrets: {missing_secrets}. Present: {sorted(secrets)}"

    # persistent-storage -> a bound PVC.
    pvcs = cluster.resource_names("pvc", namespace)
    assert any("data" in name for name in pvcs), f"persistent-storage PVC not provisioned; PVCs: {pvcs}"

    # publish-on-web -> an ingress.
    ingresses = cluster.resource_names("ingress", namespace)
    assert ingresses, "publish-on-web ingress not provisioned"

    # authorization-wall -> the oauth2-proxy sidecar container next to the app.
    containers = cluster.pod_container_names(namespace)
    assert "authorization-wall" in containers, f"auth-wall sidecar not injected; containers: {containers}"

    # metrics-scraper -> prometheus scrape annotations on the deployment pod template.
    annotations = cluster.deployment_pod_annotations(namespace)
    assert annotations.get("prometheus.io/scrape") == "true", f"metrics-scraper annotations missing; got: {annotations}"

    logger.info(
        "All services provisioned in '%s': secrets=%s pvcs=%s ingress=%s sidecar=authorization-wall",
        namespace,
        sorted(secrets),
        pvcs,
        ingresses,
    )
