"""Tests for read-only deployment drift detection (the pr-32 orphan case)."""

from opi.jobs.deployment_drift import classify_deployment_drift

CLUSTER = "odcn-production"


def _app(name: str, project: str, sync: str = "Synced", health: str = "Healthy") -> dict:
    return {
        "metadata": {"name": name, "labels": {"project": project}},
        "status": {"sync": {"status": sync}, "health": {"status": health}},
    }


def _project(name: str, deployments: list[str], cluster: str = CLUSTER) -> dict:
    return {"name": name, "deployments": [{"name": d, "cluster": cluster} for d in deployments]}


def test_detects_live_but_undeclared_deployment() -> None:
    """A live ArgoCD app with no project-file entry is reported as orphaned.

    Mirrors toets-hn7/pr-32: removed from the project file on delete, but its
    Application kept running.
    """
    project_yamls = [_project("toets-hn7", ["production", "pr-31", "pr-38"])]
    argo_apps = [
        _app("toets-hn7-production", "toets-hn7"),
        _app("toets-hn7-pr-31", "toets-hn7"),
        _app("toets-hn7-pr-38", "toets-hn7"),
        _app("toets-hn7-pr-32", "toets-hn7", health="Healthy"),  # orphan
    ]

    report = classify_deployment_drift(project_yamls, CLUSTER, argo_apps)

    assert report["summary"]["orphaned"] == 1
    orphan = report["orphaned_deployments"][0]
    assert orphan["argocd_app"] == "toets-hn7-pr-32"
    assert orphan["project"] == "toets-hn7"
    assert orphan["deployment"] == "pr-32"
    assert orphan["health"] == "Healthy"


def test_no_drift_when_all_declared() -> None:
    """When every live app is declared, nothing is flagged."""
    project_yamls = [_project("toets-hn7", ["production", "pr-31"])]
    argo_apps = [
        _app("toets-hn7-production", "toets-hn7"),
        _app("toets-hn7-pr-31", "toets-hn7"),
    ]

    report = classify_deployment_drift(project_yamls, CLUSTER, argo_apps)

    assert report["summary"]["orphaned"] == 0
    assert report["summary"]["missing"] == 0


def test_declared_but_no_live_app_is_missing_not_orphan() -> None:
    """A declared deployment with no live application is reported as missing."""
    project_yamls = [_project("toets-hn7", ["production", "pr-99"])]
    argo_apps = [_app("toets-hn7-production", "toets-hn7")]

    report = classify_deployment_drift(project_yamls, CLUSTER, argo_apps)

    assert report["summary"]["orphaned"] == 0
    assert report["summary"]["missing"] == 1
    assert report["missing_deployments"][0]["deployment"] == "pr-99"


def test_infrastructure_apps_are_not_reported_as_orphans() -> None:
    """`{project}-infrastructure` apps manage per-project infra, not deployments,
    so they must not be flagged as orphaned even though they carry a project label."""
    project_yamls = [_project("algor-odc", ["production"])]
    argo_apps = [
        _app("algor-odc-production", "algor-odc"),
        _app("algor-odc-infrastructure", "algor-odc"),  # infra app, not a deployment
    ]

    report = classify_deployment_drift(project_yamls, CLUSTER, argo_apps)

    assert report["summary"]["orphaned"] == 0
    assert report["summary"]["live"] == 1  # only the deployment counts as live


def test_other_cluster_deployments_are_ignored() -> None:
    """Deployments declared on another cluster don't count toward this cluster."""
    project_yamls = [
        {
            "name": "toets-hn7",
            "deployments": [
                {"name": "production", "cluster": CLUSTER},
                {"name": "pr-50", "cluster": "some-other-cluster"},
            ],
        }
    ]
    # pr-50 lives only on the other cluster, so it is not a live app here.
    argo_apps = [_app("toets-hn7-production", "toets-hn7")]

    report = classify_deployment_drift(project_yamls, CLUSTER, argo_apps)

    assert report["summary"]["declared"] == 1
    assert report["summary"]["orphaned"] == 0
    assert report["summary"]["missing"] == 0
