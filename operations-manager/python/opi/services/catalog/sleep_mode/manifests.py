"""Build the waker's manifests: which component gets a waker, and its values dicts.

The waker shares the app component's Service and Ingress -- it lands there because its
Deployment carries the same ``app: <unique_name>`` label and ``component: application``
(the Service selector), plus ``zad-role: waker`` to tell the two Deployments apart. So
there is exactly one waker per deployment, in front of one web component. This module is
pure: it decides and builds dicts; ``project_manager`` renders and writes them.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from opi.core.cluster_config import get_cluster_config
from opi.core.config import settings
from opi.services.catalog.sleep_mode.secret import WakeTokenSecret

if TYPE_CHECKING:
    from opi.handlers.project_file_handler import ProjectFileHandler
    from opi.services.catalog.sleep_mode.config_model import SleepModeConfig

logger = logging.getLogger(__name__)

#: The label that distinguishes the waker Deployment from the app Deployment while both
#: match the same Service selector.
WAKER_ROLE_LABEL: dict[str, str] = {"zad-role": "waker"}
#: The waker container/HTTP port.
WAKER_PORT = 8080
#: TLS modes the waker cannot serve (it holds no certificate of its own).
_UNSUPPORTED_TLS = ("passthrough", "provided")


def waker_object_name(app_name: str) -> str:
    return f"{app_name}-waker"


def waker_config_name(app_name: str) -> str:
    return f"{app_name}-waker-config"


def waker_token_secret_name(app_name: str) -> str:
    return WakeTokenSecret.get_secret_name(app_name)


def ops_api_url(cluster: str) -> str:
    """In-cluster URL of the OPI API the waker calls (over tenant->ops egress)."""
    ops_namespace = get_cluster_config(cluster).get("namespace", "rig-system")
    return f"http://operations-manager.{ops_namespace}.svc.cluster.local:8000"


def select_waker_component(
    project_data: dict[str, Any],
    deployment: dict[str, Any],
    config: SleepModeConfig,
    handler: ProjectFileHandler,
) -> str | None:
    """Pick the one component that gets the waker, or None (rules in plan section 5).

    1. ``waker-component`` set and web-published with standard TLS -> that one.
    2. Exactly one web-published (standard TLS) component -> that one.
    3. Zero, or two-or-more without ``waker-component`` -> no waker, and a log line
       naming the candidates. Not picking is honest: the deployment still sleeps and is
       wakeable via the UI/API, and one waker per hostname would waste a pod each.
    """
    deployment_name = deployment.get("name", "")
    web: list[str] = []
    for comp in deployment.get("components", []) or []:
        ref = comp.get("reference")
        if not ref or not handler.extract_component_publish_on_web(project_data, ref):
            continue
        tls = handler.extract_component_publish_on_web_tls(project_data, ref, deployment_name)
        if tls in _UNSUPPORTED_TLS:
            continue
        web.append(ref)

    if config.waker_component is not None:
        if config.waker_component in web:
            return config.waker_component
        logger.warning(
            "sleep-mode: waker-component '%s' on deployment '%s' is not web-published with standard TLS; "
            "no waker (candidates: %s)",
            config.waker_component,
            deployment_name,
            web,
        )
        return None

    if len(web) == 1:
        return web[0]
    if not web:
        logger.info("sleep-mode: deployment '%s' has no standard-TLS web component; no waker", deployment_name)
    else:
        logger.warning(
            "sleep-mode: deployment '%s' has multiple web components %s and no waker-component set; no waker. "
            "Set waker-component to the one visitors reach first.",
            deployment_name,
            web,
        )
    return None


def build_waker_deployment_values(
    *,
    app_name: str,
    namespace: str,
    project_name: str,
    deployment_name: str,
    cluster: str,
    pod_replacement_mode: str = "RollingUpdate",
    generated_at: str = "",
    image_pull_secrets_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Values for ``deployment.yaml.jinja`` for the waker (plan section 7).

    Only the fields that differ from a normal component; everything app-specific
    (storage, sidecars, app env/secrets) is explicitly emptied so the waker pod is
    minimal and never mounts the app's resources.
    """
    image = settings.SLEEP_MODE_WAKER_IMAGE
    # A moving :latest tag must be re-pulled; a pinned tag (incl. a kind-loaded local
    # image on the sandbox) must use the present image, so IfNotPresent.
    image_pull_policy = "Always" if image.endswith(":latest") else "IfNotPresent"
    return {
        "object_name": waker_object_name(app_name),
        "name": app_name,  # app label + Service match
        "extra_selector_labels": WAKER_ROLE_LABEL,
        "namespace": namespace,
        "project": {"name": project_name},
        "deployment_name": deployment_name,
        "cluster": cluster,
        "pod_replacement_mode": pod_replacement_mode,
        "generated_at": generated_at,
        "imageURL": image,
        "imagePullPolicy": image_pull_policy,
        "imagePullSecretsMap": image_pull_secrets_map or {},
        "replicas": 1,
        "inbound_ports": [WAKER_PORT],
        "application_port": WAKER_PORT,
        "probe_scheme": "http",
        "probe_liveness_path": "/__zad/healthz",
        "probe_readiness_path": "/__zad/ready",
        "probe_readiness_failure_threshold": 1,
        "env_from_configmaps": [waker_config_name(app_name)],
        "env_from_secrets": [waker_token_secret_name(app_name)],
        "resources_requests_cpu": "10m",
        "resources_requests_memory": "25Mi",
        "resources_limits_cpu": "100m",
        "resources_limits_memory": "64Mi",
        # Explicitly empty: the waker mounts none of the app's resources.
        "storage_configs": [],
        "sidecars": [],
        "attachment_secret_mounts": [],
        "ca_config": None,
        "env_vars": {},
        "command": None,
        "metrics_config": None,
        "security": None,
    }


def build_waker_configmap_values(
    *,
    app_name: str,
    namespace: str,
    project_name: str,
    deployment_name: str,
    component_reference: str,
    config: SleepModeConfig,
    cluster: str,
) -> dict[str, Any]:
    """Values for ``configmap.yaml.jinja`` holding the waker's presentation config."""
    title_template = config.title or "{deployment}"
    title = title_template.format(project=project_name, deployment=deployment_name, component=component_reference)
    data = {
        "ZAD_API_URL": ops_api_url(cluster),
        "ZAD_PROJECT": project_name,
        "ZAD_DEPLOYMENT": deployment_name,
        "ZAD_APP_TITLE": title,
        "ZAD_APP_DESCRIPTION": config.description,
        "ZAD_WAKE_MODE": config.wake_mode,
        "ZAD_POLL_INTERVAL_SEC": "3",
    }
    return {
        "name": waker_config_name(app_name),
        "namespace": namespace,
        "app_label": app_name,
        "project": {"name": project_name},
        "data": data,
    }
