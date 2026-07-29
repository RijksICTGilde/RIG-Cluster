"""Golden-manifest byte-diff harness (RC-5 Phase 0 guardrail).

Purpose
-------
The RC-5 migration ("Uniform, Declarative Platform Services") progressively moves
the per-service manifest contribution -- the ``component_uses_*`` flags and the
``env_from_secrets`` / ``sidecars`` / ``storage_configs`` / ``metrics_config`` /
``attachment_secret_mounts`` variables assembled in
``ProjectManager.create_application_manifests`` -- onto per-service providers
(``contribute_manifest_context``). The plan flags that manifest cutover (Phase 6)
as the highest regression risk and mandates a byte-diff guardrail established
*before* any behaviour changes.

This module is that guardrail. For a representative matrix of service scenarios it
renders the affected Jinja templates through the real, connector-free renderer
(``opi.generation.manifests.render_template``) and byte-compares the output against
committed golden files under ``tests/golden/manifests/``. Rendering happens at the
template layer (Layer A) because it is fully deterministic offline; the full
``process_project`` pipeline (Layer B) needs live git/SOPS/keycloak/db/minio and
non-deterministic SOPS encryption, which cannot be byte-compared.

The single non-deterministic template field, ``generated_at`` (normally
``datetime.now(UTC)``), is pinned to a fixed value here.

What each phase does with this harness
--------------------------------------
* Phases 1-5 do not touch the manifest templates or the assembled context, so the
  golden files must stay byte-identical -- a diff means an unintended regression.
* Phase 6 extracts the context assembly into ``contribute_manifest_context``. The
  providers must emit the *same* context keys, so these golden renders must remain
  byte-identical. A dedicated Phase 6 test additionally asserts the provider-emitted
  context dict equals the expected dict per service.

Regenerating goldens
--------------------
Intentional template changes require regenerating the goldens::

    UPDATE_GOLDEN=1 uv run pytest tests/test_golden_manifests.py -q

Review the resulting diff carefully -- an unexpected change here is exactly the
class of regression this harness exists to catch. Never regenerate blindly.
"""

import os
from pathlib import Path

import pytest
from opi.generation.manifests import render_template

GOLDEN_DIR = Path(__file__).parent / "golden" / "manifests"

# Fixed timestamp so renders are byte-stable (real code uses datetime.now(UTC)).
FIXED_GENERATED_AT = "2024-01-01T00:00:00Z"


def _deployment_vars(**overrides: object) -> dict[str, object]:
    """Realistic per-component deployment variables.

    Mirrors the ``variables`` dict built in
    ``ProjectManager.create_application_manifests`` (project_manager.py:5111-5167)
    so the golden output reflects what the real pipeline renders. Scenarios override
    only the service-contribution keys they exercise.
    """
    base: dict[str, object] = {
        "name": "prod-web",
        "deployment_name": "prod",
        "component_name": "web",
        "namespace": "rig-myproject",
        "hostname": "web.example.com",
        "project": {"name": "myproject"},
        "cluster": "odcn-production",
        "pod_replacement_mode": "RollingUpdate",
        "imageURL": "ghcr.io/org/web:1.2.3",
        "imagePullPolicy": "IfNotPresent",
        "application_port": 8080,
        "service_port": 8080,
        "inbound_ports": [8080],
        "probe_scheme": "tcp",
        "probe_readiness_path": "/",
        "probe_liveness_path": "/",
        "path": "/",
        "storage_configs": [],
        "env_vars": {},
        # Platform secret is always present and last of the "always" secrets.
        "env_from_secrets": ["prod-web-platform"],
        "attachment_secret_mounts": [],
        "enable_tls": True,
        "ip_whitelist": None,
        "imagePullSecretsMap": {},
        "generated_at": FIXED_GENERATED_AT,
        "ca_config": {"enabled": False},
        "metrics_config": None,
        "resources_requests_memory": "128Mi",
        "resources_requests_cpu": "100m",
        "resources_limits_memory": "256Mi",
        "resources_limits_cpu": "500m",
        "replicas": 1,
        "security": None,
        "command": None,
    }
    base.update(overrides)
    return base


# Deployment env_from_secrets orderings mirror project_manager.py:5061-5098:
# deployment-level service secrets first (db, minio, keycloak, redis, metrics),
# then the always-present platform secret, then the optional per-component user secret.
_AUTH_WALL = {
    "issuer_url": "https://keycloak.example.com/realms/myproject-odcn-production",
    "client_id": "prod-web",
    "keycloak_secret_name": "prod-keycloak",
    "cookie_secret_name": "prod-web-oauth2-cookie",
    "banner": None,
}

# Each scenario: (id, template_name, variables). The id is the golden filename stem.
SCENARIOS: list[tuple[str, str, dict[str, object]]] = [
    # --- deployment.yaml.jinja: one per service contribution ------------------
    ("deployment-web-only", "deployment.yaml.jinja", _deployment_vars()),
    (
        "deployment-postgresql",
        "deployment.yaml.jinja",
        _deployment_vars(env_from_secrets=["prod-database", "prod-web-platform"]),
    ),
    (
        "deployment-minio",
        "deployment.yaml.jinja",
        _deployment_vars(env_from_secrets=["prod-minio", "prod-web-platform"]),
    ),
    (
        "deployment-keycloak",
        "deployment.yaml.jinja",
        _deployment_vars(env_from_secrets=["prod-keycloak", "prod-web-platform"]),
    ),
    (
        "deployment-redis",
        "deployment.yaml.jinja",
        _deployment_vars(env_from_secrets=["prod-redis", "prod-web-platform"]),
    ),
    (
        "deployment-metrics-scraper",
        "deployment.yaml.jinja",
        _deployment_vars(
            env_from_secrets=["prod-metrics-auth", "prod-web-platform"],
            metrics_config={"port": 9090, "path": "/metrics"},
        ),
    ),
    (
        # Explicit port AND path, both differing from the fallbacks (application_port /
        # "/metrics"), so the prometheus.io annotations must render the configured
        # values. Locks the metrics-scraper latent-fix: the service now propagates the
        # real port/path (metrics_scraper.contribute_manifest_context) instead of the
        # template always falling back to the app port / "/metrics".
        "deployment-metrics-scraper-explicit",
        "deployment.yaml.jinja",
        _deployment_vars(
            env_from_secrets=["prod-metrics-auth", "prod-web-platform"],
            metrics_config={"port": 9464, "path": "/actuator/prometheus"},
        ),
    ),
    (
        "deployment-storage",
        "deployment.yaml.jinja",
        _deployment_vars(
            storage_configs=[
                {
                    "name": "data",
                    "type": "persistent",
                    "mount-path": "/data",
                    "size": "10Gi",
                    "pvc_name": "prod-web-data",
                },
                {"name": "temp", "type": "ephemeral", "mount-path": "/tmp", "size": "500Mi"},
            ],
        ),
    ),
    (
        "deployment-attachments",
        "deployment.yaml.jinja",
        _deployment_vars(
            attachment_secret_mounts=[
                {
                    "name": "cert-attachment",
                    "mount_path": "/etc/certs/tls.crt",
                    "sub_path": "tls.crt",
                    "secret_name": "prod-web-attachment-cert",
                }
            ],
        ),
    ),
    (
        "deployment-authorization-wall",
        "deployment.yaml.jinja",
        _deployment_vars(
            env_from_secrets=["prod-keycloak", "prod-web-platform"],
            service_port=4180,
            sidecars=["authorization-wall"],
            authorization_wall=_AUTH_WALL,
        ),
    ),
    (
        "deployment-full-stack",
        "deployment.yaml.jinja",
        _deployment_vars(
            env_vars={"APP_ENV": "production"},
            env_from_secrets=[
                "prod-database",
                "prod-minio",
                "prod-keycloak",
                "prod-redis",
                "prod-web-platform",
                "prod-web-user",
            ],
            storage_configs=[
                {
                    "name": "data",
                    "type": "persistent",
                    "mount-path": "/data",
                    "size": "10Gi",
                    "pvc_name": "prod-web-data",
                },
            ],
        ),
    ),
    (
        # Kind/sandbox securityContext branch (numeric UID + fsGroup for PVC write).
        "deployment-sandbox-storage",
        "deployment.yaml.jinja",
        _deployment_vars(
            cluster="sandboxed-local",
            namespace="rig-myproject",
            security={"runAsUser": 1001, "runAsGroup": 1001, "fsGroup": 1001},
            storage_configs=[
                {
                    "name": "data",
                    "type": "persistent",
                    "mount-path": "/data",
                    "size": "1Gi",
                    "pvc_name": "prod-web-data",
                },
            ],
        ),
    ),
    # --- service.yaml.jinja ----------------------------------------------------
    (
        "service-basic",
        "service.yaml.jinja",
        {
            "name": "prod-web",
            "namespace": "rig-myproject",
            "project": {"name": "myproject"},
            "application_port": 8080,
            "inbound_ports": [8080],
        },
    ),
    (
        # Auth-wall fronts the primary port on 4180; extra inbound ports stay direct.
        "service-authorization-wall",
        "service.yaml.jinja",
        {
            "name": "prod-web",
            "namespace": "rig-myproject",
            "project": {"name": "myproject"},
            "application_port": 8080,
            "service_port": 4180,
            "inbound_ports": [8080, 9090],
        },
    ),
    # --- ingress.yaml.jinja (publish-on-web) -----------------------------------
    (
        "ingress-publish-on-web",
        "ingress.yaml.jinja",
        {
            "name": "prod-web-ingress",
            "hostname": "web.example.com",
            "enable_tls": True,
            "cluster_issuer": "letsencrypt-prod",
            "tls_secret_name": "prod-web-tls",
        },
    ),
    # --- pvc.yaml.jinja (persistent-storage) -----------------------------------
    (
        "pvc-persistent",
        "pvc.yaml.jinja",
        {
            "name": "prod-web-data",
            "namespace": "rig-myproject",
            "access_modes": ["ReadWriteOnce"],
            "storage_class_name": "standard",
            "size": "10Gi",
            "backup_enabled": True,
        },
    ),
    # --- sidecar-authorization-wall.yaml.jinja (configmap section) -------------
    (
        "sidecar-authorization-wall-configmap",
        "sidecar-authorization-wall.yaml.jinja",
        {
            "section": "configmap",
            "name": "prod-web",
            "namespace": "rig-myproject",
            "project": {"name": "myproject"},
        },
    ),
]

_UPDATE = os.environ.get("UPDATE_GOLDEN") == "1"


@pytest.mark.parametrize(("scenario_id", "template_name", "variables"), SCENARIOS, ids=[s[0] for s in SCENARIOS])
def test_golden_manifest(scenario_id: str, template_name: str, variables: dict[str, object]) -> None:
    rendered = render_template(template_name, variables)
    golden_path = GOLDEN_DIR / f"{scenario_id}.golden.yaml"

    if _UPDATE:
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        golden_path.write_text(rendered, encoding="utf-8")
        pytest.skip(f"UPDATE_GOLDEN: wrote {golden_path.name}")

    assert golden_path.exists(), (
        f"Missing golden file {golden_path}. Generate it with "
        f"`UPDATE_GOLDEN=1 uv run pytest tests/test_golden_manifests.py` and review the diff before committing."
    )
    expected = golden_path.read_text(encoding="utf-8")
    assert rendered == expected, (
        f"Rendered manifest for '{scenario_id}' drifted from its golden file.\n"
        f"If this change is intentional, regenerate with UPDATE_GOLDEN=1 and review the diff. "
        f"Template: {template_name}"
    )


def test_all_scenarios_have_unique_ids() -> None:
    ids = [s[0] for s in SCENARIOS]
    assert len(ids) == len(set(ids)), "Duplicate scenario ids in SCENARIOS"


def test_no_orphan_golden_files() -> None:
    """Every committed golden must map to a scenario, so stale goldens can't linger."""
    if not GOLDEN_DIR.exists():
        pytest.skip("golden dir not created yet")
    expected_names = {f"{s[0]}.golden.yaml" for s in SCENARIOS}
    actual_names = {p.name for p in GOLDEN_DIR.glob("*.golden.yaml")}
    orphans = actual_names - expected_names
    assert not orphans, f"Orphan golden files with no scenario: {sorted(orphans)}"
