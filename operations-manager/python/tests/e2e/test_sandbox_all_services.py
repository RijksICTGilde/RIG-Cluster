"""
Sandbox E2E: create a project with EVERY platform service enabled, then verify
both that the project YAML is correct AND that each service was actually
provisioned in the cluster (database, cache, object storage, auth, ...).

The other sandbox suites create service-less projects, so nothing exercises the
full provisioning path end to end. This suite ticks all 10 create-wizard services
(publish-on-web, keycloak, authorization-wall, metrics-scraper, persistent-storage,
temp-storage, postgresql-database, minio-storage, redis, attachments), and then, on
a separate minimal project, adds the two hidden namespace variants
(namespace-postgresql-database, namespace-redis) via the v2 API - they have no
wizard/edit card - so every ServiceType is covered.

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

import json
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
from tests.e2e.helpers.wizard import unique_project_name

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
    display_name = unique_project_name(prefix="alls")
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

    # Component-level service entries must be in the uniform {reference, config} form -
    # NOT the legacy name-as-key ({persistent-storage: {config: ...}}) or inline
    # ({metrics-scraper: {port, path}}) shape the wizard editables emit. The create
    # path normalizes them so the file is born current (attachments stays legacy).
    for entry in comp_services:
        if isinstance(entry, dict) and "attachments" not in entry:
            assert "reference" in entry, f"component service not in uniform {{reference, config}} form: {entry!r}"


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


# Service -> /status target id, for the subset of bound services that inject
# connection variables and therefore round-trip a real resource. authorization-wall
# and attachments inject no vars, so they have no /status target (their provisioning
# is already asserted structurally above).
_STATUS_TARGETS = {
    "publish-on-web": "web",
    "keycloak": "oidc",
    "metrics-scraper": "metrics",
    "persistent-storage": "storage-data",
    "temp-storage": "storage-temp",
    "postgresql-database": "postgres",
    "minio-storage": "minio",
    "redis": "redis",
}


def _fetch_status(namespace: str) -> dict:
    """GET /status from the workload pod over a port-forward (bypasses the ingress
    and the auth-wall sidecar). Retries pods until one answers with the payload."""
    pods = cluster.running_pod_names(namespace, "")
    assert pods, f"no running pod in namespace '{namespace}' to query /status"
    last_error: Exception | None = None
    for pod in pods:
        try:
            # Short per-attempt cap: this runs inside a poll loop, so a pod that is
            # not serving yet should fail fast and be retried on the next round
            # rather than blocking the whole wait on one slow port-forward.
            code, body = cluster.http_get_via_port_forward(namespace, pod, 8080, "/status", timeout=15.0)
        except Exception as exc:
            last_error = exc
            continue
        if code == 200 and '"services"' in body:
            return json.loads(body)
        last_error = AssertionError(f"pod {pod} /status -> {code}: {body[:200]}")
    raise AssertionError(f"could not read /status from any pod in '{namespace}': {last_error}")


@pytest.mark.timeout(600)
def test_all_services_status_reports_every_binding_ok(
    all_services_project: CreatedProject,
) -> None:
    """The workload's /status confirms every bound service is actually reachable
    with the injected credentials - the point of this whole image.

    'Pod Healthy' only proves the process started and passed the TCP probe. This
    asserts the app could really connect to and round-trip each platform resource
    (database incl. schemas + RO role, redis, minio, oidc, PVCs), reported at
    /status as bound + ok. Skips when kubectl cannot reach the sandbox host.
    """
    if not cluster.kubectl_available():
        pytest.skip("kubectl cannot reach a cluster - /status check needs the sandbox host")

    namespace = f"rig-{all_services_project.name}"

    # The workload only reports green once its first check round has run; give the
    # freshly-created pod a moment to come up and verify its bindings.
    status: dict = {}

    def _ready() -> bool:
        nonlocal status
        try:
            status = _fetch_status(namespace)
        except Exception:
            return False
        return bool(status.get("ready"))

    assert cluster.wait_for(_ready, timeout=_PROVISION_TIMEOUT), f"workload /status never became ready in '{namespace}'"
    logger.info("status for '%s': %s", all_services_project.name, status)

    services = status.get("services") or {}

    # Every service the project bound that round-trips a resource must be bound + ok.
    for svc, target_id in _STATUS_TARGETS.items():
        entry = services.get(target_id)
        assert entry is not None, f"/status has no target '{target_id}' for service '{svc}'"
        assert entry.get("bound") is True, f"service '{svc}' ({target_id}) not bound: {entry}"
        assert entry.get("ok") is True, f"service '{svc}' ({target_id}) not ok: {entry}"

    # Postgres detail: every schema (default + any extra DATABASE_SCHEMA_{POSTFIX})
    # round-tripped, and the read-only role behaved (read, write refused).
    postgres = services["postgres"]
    schemas = (postgres.get("detail") or {}).get("schemas") or {}
    assert schemas, f"postgres /status carries no schema results: {postgres}"
    bad_schemas = {name: verdict for name, verdict in schemas.items() if verdict != "ok"}
    assert not bad_schemas, f"postgres schema round-trip failed: {bad_schemas}"
    read_only = (postgres.get("detail") or {}).get("read_only", "")
    # The RO role must be either enforced (read ok, write refused) or explicitly
    # reported as not provisioned - never a silent gap.
    assert ("write refused" in read_only) or ("skipped" in read_only), (
        f"RO role result is neither enforced nor a clear skip: {read_only!r}"
    )

    # The overall verdict must be green: no bound service failed.
    assert status.get("all_ok") is True, f"workload reports failures: {status}"


# --- Namespace-variant services (added via the API; hidden in the wizard) --------
#
# namespace-postgresql-database and namespace-redis are hidden=True, so they have no
# wizard/edit card - the only ways to add them are the v2 API and a direct YAML edit.
# We add them via POST /api/v2/projects/{name}/services to a minimal project.
# namespace-postgres provisions a DEDICATED CNPG database cluster in the project's
# infrastructure namespace (the differentiator from the shared postgresql-database).
# namespace-redis is a documented not-yet-implemented stub that falls back to shared
# Redis (opi/manager/redis_manager.py), so we only assert it is accepted, not that it
# provisions a dedicated instance.

_ADD_SERVICE_ATTEMPTS = 3


def _add_service_with_retry(base_url: str, project: CreatedProject, service: str) -> None:
    """Add a service via the v2 API, waiting for the task and retrying the known
    transient race where a just-created CNPG instance is not yet reachable when the
    provisioner connects (fails with 'Connection refused' at user creation). The
    add itself is idempotent (already-present services are skipped)."""
    last_reason = ""
    for attempt in range(_ADD_SERVICE_ATTEMPTS):
        task_id = sandbox_api.start_task(
            base_url,
            "POST",
            f"/api/v2/projects/{project.name}/services",
            project.api_key,
            {"service": service, "components": ["web"]},
            verify_ssl=_VERIFY_SSL,
        )
        state, reason = sandbox_api.task_outcome(
            base_url, task_id, project.api_key, verify_ssl=_VERIFY_SSL, timeout=420
        )
        if state == "completed":
            return
        last_reason = reason or ""
        logger.warning("add-service '%s' attempt %d/%d: %s", service, attempt + 1, _ADD_SERVICE_ATTEMPTS, last_reason)
    raise AssertionError(
        f"add-service '{service}' did not complete after {_ADD_SERVICE_ATTEMPTS} attempts: {last_reason}"
    )


@pytest.fixture(scope="module")
def namespace_variants_project(
    sandbox_context: BrowserContext,
    sandbox_url: str,
    forgejo: ForgejoClient,
) -> Generator[CreatedProject]:
    """A minimal project with the two namespace-variant services added via the API."""
    display_name = unique_project_name(prefix="nsv")
    page = sandbox_context.new_page()
    created: CreatedProject | None = None
    try:
        created = create_project_with_services(
            page, sandbox_url, forgejo, display_name, user_email=SANDBOX_TEST_USER["email"], services=["publish-on-web"]
        )
        logger.info("Created namespace-variants base project: %s", created.name)
        _add_service_with_retry(sandbox_url, created, "namespace-postgresql-database")
        _add_service_with_retry(sandbox_url, created, "namespace-redis")
        yield created
    finally:
        page.close()
        if created is not None:
            sandbox_api.delete_project_via_api(sandbox_url, created.name, created.api_key, verify_ssl=_VERIFY_SSL)


@pytest.mark.timeout(900)
def test_namespace_variants_in_project_yaml(
    namespace_variants_project: CreatedProject,
    forgejo: ForgejoClient,
) -> None:
    """Both namespace variants are accepted and land in the project YAML + component."""
    data = forgejo.get_project_yaml(namespace_variants_project.name)
    assert data is not None
    present = _service_names(data)
    for svc in ("namespace-postgresql-database", "namespace-redis"):
        assert svc in present, f"{svc} not in project services: {present}"


@pytest.mark.timeout(900)
def test_namespace_postgres_provisions_dedicated_cluster(
    namespace_variants_project: CreatedProject,
) -> None:
    """namespace-postgres provisions a DEDICATED CNPG database cluster (+ pod) in the
    project's infrastructure namespace - the thing that distinguishes it from the
    shared postgresql-database."""
    if not cluster.kubectl_available():
        pytest.skip("kubectl cannot reach a cluster - live provisioning checks need the sandbox host")

    infra_ns = f"rig-{namespace_variants_project.name}-infrastructure"
    have_cluster = cluster.wait_for(
        lambda: bool(cluster.cnpg_cluster_names(infra_ns)),
        timeout=_PROVISION_TIMEOUT,
    )
    assert have_cluster, f"no dedicated CNPG cluster provisioned in '{infra_ns}'"

    clusters = cluster.cnpg_cluster_names(infra_ns)
    logger.info("namespace-postgres dedicated CNPG cluster(s) in '%s': %s", infra_ns, clusters)
    # The database pod must actually run.
    pod_running = cluster.wait_for(
        lambda: any("db" in name for name in cluster.resource_names("pods", infra_ns)),
        timeout=180.0,
    )
    assert pod_running, (
        f"CNPG database pod not running in '{infra_ns}'; pods: {cluster.resource_names('pods', infra_ns)}"
    )
