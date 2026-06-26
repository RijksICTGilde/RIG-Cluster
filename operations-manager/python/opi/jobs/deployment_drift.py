"""Read-only deployment drift detection.

Compares the deployments declared in project files against the ArgoCD
Application resources actually live on the cluster, surfacing deployments that
are live but no longer declared. This is the durable failure mode a per-deployment
delete can leave behind when a step fails terminally: the toets-hn7/pr-32 case,
where the deployment was removed from the project file but its ArgoCD Application,
manifests and pods kept running for days with nothing detecting it.

Report-only: this module never mutates anything. Remediation is a deliberate,
operator-driven step (mirrors the service-orphan sweep's "no auto-delete from a
scan" safety rule).
"""

from typing import Any

from opi.utils.naming import generate_argocd_application_name, generate_infrastructure_application_name


def _declared_app_names(project_yamls: list[dict[str, Any]], cluster: str) -> dict[str, tuple[str, str]]:
    """Map expected ArgoCD application name -> (project, deployment) for a cluster."""
    expected: dict[str, tuple[str, str]] = {}
    for project in project_yamls:
        project_name = project.get("name", "")
        if not project_name:
            continue
        for deployment in project.get("deployments", []):
            if deployment.get("cluster") != cluster:
                continue
            deployment_name = deployment.get("name", "")
            if not deployment_name:
                continue
            app_name = generate_argocd_application_name(project_name, deployment_name)
            expected[app_name] = (project_name, deployment_name)
    return expected


def classify_deployment_drift(
    project_yamls: list[dict[str, Any]],
    cluster: str,
    argo_apps: list[dict[str, Any]],
) -> dict[str, Any]:
    """Classify drift between declared deployments and live ArgoCD applications.

    Args:
        project_yamls: Parsed project files (each a dict with name + deployments).
        cluster: The cluster this OPI manages (CLUSTER_MANAGER).
        argo_apps: Live ArgoCD Application resources (kubectl JSON ``items``), each
            expected to carry a ``project`` metadata label (set by OPI's template).

    Returns:
        A report dict with an ``orphaned_deployments`` list (live but not declared,
        the pr-32 case) and a ``missing_deployments`` list (declared but no live
        application). No deletions are performed or scheduled.
    """
    expected = _declared_app_names(project_yamls, cluster)
    expected_names = set(expected)
    live_names: set[str] = set()
    orphaned: list[dict[str, Any]] = []

    for app in argo_apps:
        meta = app.get("metadata", {}) or {}
        name = meta.get("name", "")
        if not name:
            continue

        labels = meta.get("labels", {}) or {}
        project_label = labels.get("project", "")

        # Project-infrastructure apps ("{project}-infrastructure") are not
        # deployments; they manage per-project infra (e.g. PostgreSQL clusters)
        # and are never listed in deployments[]. Skip them so they are not
        # mis-reported as orphaned deployments.
        if project_label and name == generate_infrastructure_application_name(project_label):
            continue

        live_names.add(name)
        if name in expected_names:
            continue

        # app name is "{project}-{deployment}"; strip the known project prefix.
        deployment = name[len(project_label) + 1 :] if project_label and name.startswith(f"{project_label}-") else None
        status = app.get("status", {}) or {}
        orphaned.append(
            {
                "argocd_app": name,
                "project": project_label or None,
                "deployment": deployment,
                "sync": (status.get("sync") or {}).get("status"),
                "health": (status.get("health") or {}).get("status"),
                "reason": "live ArgoCD application not declared in any project file for this cluster",
            }
        )

    missing = [
        {
            "argocd_app": app_name,
            "project": project_name,
            "deployment": deployment_name,
            "reason": "declared deployment has no live ArgoCD application",
        }
        for app_name, (project_name, deployment_name) in expected.items()
        if app_name not in live_names
    ]

    return {
        "cluster": cluster,
        "summary": {
            "declared": len(expected_names),
            "live": len(live_names),
            "orphaned": len(orphaned),
            "missing": len(missing),
        },
        "orphaned_deployments": orphaned,
        "missing_deployments": missing,
    }
