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
from opi.handlers.project_file_handler import extract_service_names_from_component
from opi.services.catalog.base import SERVICE_ROLE_LABEL_KEY
from opi.services.catalog.sleep_mode.secret import WakeTokenSecret
from opi.services.services_enums import ServiceType

if TYPE_CHECKING:
    from opi.handlers.project_file_handler import ProjectFileHandler
    from opi.services.catalog.sleep_mode.config_model import SleepModeConfig

logger = logging.getLogger(__name__)

#: The label that distinguishes the waker Deployment from the app Deployment while both
#: match the same Service selector.
# Built from the platform key so "this pod is not the application" stays one concept:
# every application-level pod lookup excludes anything carrying it.
WAKER_ROLE_LABEL: dict[str, str] = {SERVICE_ROLE_LABEL_KEY: "waker"}
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


def _is_behind_authorization_wall(project_data: dict[str, Any], component_reference: str) -> bool:
    """Whether the component's Service is fronted by the auth wall's oauth2-proxy.

    Read from the root component definition, the same place ``project_manager`` reads it
    when it decides to add the sidecar and move ``service_port`` to the proxy.
    """
    for component in project_data.get("components", []) or []:
        if component.get("name") == component_reference:
            return ServiceType.AUTHORIZATION_WALL.value in extract_service_names_from_component(component)
    return False


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

    A component behind an authorization wall is not a candidate either, for the same
    reason ``passthrough`` TLS is not: the waker cannot serve that hostname the way it is
    supposed to be served. The wall moves the Service to the oauth2-proxy port, and the
    waker has no sidecars, so a waker there would answer on the proxy's port WITHOUT the
    proxy -- an anonymous visitor would see the application's title and get a button that
    starts it, on a hostname whose whole point is that it is not anonymous.
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
        if _is_behind_authorization_wall(project_data, ref):
            logger.info(
                "sleep-mode: component '%s' on deployment '%s' is behind an authorization wall; no waker for it",
                ref,
                deployment_name,
            )
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
    port: int,
    pod_replacement_mode: str = "RollingUpdate",
    generated_at: str = "",
    image_pull_secrets_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Values for ``deployment.yaml.jinja`` for the waker (plan section 7).

    Only the fields that differ from a normal component; everything app-specific
    (storage, sidecars, app env/secrets) is explicitly emptied so the waker pod is
    minimal and never mounts the app's resources.

    ``port`` is the port the component's Service targets, and it is a parameter rather
    than a constant because the waker has no Service of its own: it joins the
    application's by carrying the same ``app`` label. A waker on any other port is still
    selected by that Service, still passes its own probes -- they go straight to the
    container port -- and answers nothing. That is how a hardcoded 8080 left every
    project whose component listens elsewhere with a healthy pod and a dead hostname.
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
        "inbound_ports": [port],
        "application_port": port,
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
    port: int,
) -> dict[str, Any]:
    """Values for ``configmap.yaml.jinja`` holding the waker's presentation config.

    ``ZAD_PORT`` is the other half of the port fix: the manifest declares the container
    port, this tells the process inside to listen there. The image defaults to 8080 when
    the variable is absent, so an older waker image keeps behaving as it did.
    """
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
        "ZAD_PORT": str(port),
    }
    return {
        "name": waker_config_name(app_name),
        "namespace": namespace,
        "app_label": app_name,
        "project": {"name": project_name},
        "data": data,
    }
