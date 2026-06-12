"""process_project must thread the deployment scope into manifest + argocd generation.

Regression guard for the scope-drop bug: process_project correctly scoped its
namespace/resource/clone steps to `targets`, but called
`_process_application_manifests(deployment_name)` and
`create_argocd_resources(deployment_name)` with the *singular* arg (None on the
plural-scoped path), so a scoped upsert of one deployment regenerated EVERY
deployment's manifests -- rewriting unrelated deployments and colliding with a
concurrent delete (the pr-32 resurrection).

These two calls must pass the resolved scope via `deployment_names=`.
"""

import ast
import inspect

from opi.manager.project_manager import ProjectManager

_MUST_BE_SCOPED = ("_process_application_manifests", "create_argocd_resources")


def _calls_in_process_project(attr_name: str) -> list[ast.Call]:
    source = inspect.getsource(ProjectManager.process_project)
    module = ast.parse(source.strip())
    return [
        node
        for node in ast.walk(module)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == attr_name
    ]


def test_manifest_and_argocd_generation_receive_deployment_names() -> None:
    for attr in _MUST_BE_SCOPED:
        calls = _calls_in_process_project(attr)
        assert calls, f"expected process_project to call {attr}()"
        for call in calls:
            kwargs = {kw.arg for kw in call.keywords}
            assert "deployment_names" in kwargs, (
                f"{attr}() in process_project must be called with deployment_names=<scope>, "
                f"otherwise a scoped op regenerates every deployment's manifests "
                f"(got keywords: {kwargs or 'none'})"
            )
