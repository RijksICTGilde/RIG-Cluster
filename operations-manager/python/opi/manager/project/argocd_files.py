"""The ArgoCD files of one project in the GitOps repository.

Reading which Application names which AppProject, and removing an AppProject only once
nothing points at it any more. Shared by the project deletion and the deployment removal,
which both rebuild this directory.
"""

from __future__ import annotations

import glob
import logging
import os
from typing import TYPE_CHECKING, Any

from opi.utils.naming import generate_argocd_appproject_prefix, get_output_filename_from_template
from opi.utils.yaml_util import YAMLError, load_yaml_safe

if TYPE_CHECKING:
    from collections.abc import Collection

logger = logging.getLogger(__name__)


def namespace_used_by_others(
    deployments: list[dict[str, Any]], deployment: dict[str, Any], leaving: Collection[str] = ()
) -> bool:
    """Whether a deployment that STAYS still uses the namespace of ``deployment``.

    ``leaving`` are the deployments being removed in the SAME round. Without that
    distinction every sibling entry calls the namespace "in use", including one that is going
    away just as hard, and then no round ever cleans it up (case 5 of
    `features/futures/verwijderpaden-in-kaart.md`, where the cases of this module live).
    """
    return any(
        other.get("name") != deployment.get("name")
        and other.get("name") not in leaving
        and other.get("cluster") == deployment.get("cluster")
        and other.get("namespace") == deployment.get("namespace")
        for other in deployments
    )


def application_files(project_dir: str) -> tuple[list[tuple[str, str, str]], list[str]]:
    """The Application files in an argo project directory.

    Returns per Application its path, its ``metadata.name`` and the AppProject it names, plus
    the files that could not be read.
    """
    applications: list[tuple[str, str, str]] = []
    unreadable: list[str] = []
    for path in sorted(glob.glob(os.path.join(project_dir, "*.yaml"))):
        try:
            with open(path, encoding="utf-8") as handle:
                document = load_yaml_safe(handle.read())
        except (OSError, UnicodeDecodeError, YAMLError) as e:
            logger.warning(f"Could not read '{path}' in the ArgoCD folder of this project: {e}")
            unreadable.append(os.path.basename(path))
            continue
        if not isinstance(document, dict) or document.get("kind") != "Application":
            continue
        name = (document.get("metadata") or {}).get("name") or os.path.basename(path)
        applications.append((path, name, (document.get("spec") or {}).get("project") or ""))
    return applications, unreadable


def applications_pointing_at_appproject(project_dir: str, appproject_name: str) -> list[str]:
    """Which Applications in this argo directory still name this AppProject.

    A file that cannot be read counts: unknown is no free pass here.
    """
    applications, unreadable = application_files(project_dir)
    return [name for _, name, project in applications if project == appproject_name] + unreadable


def remove_appproject_file(project_dir: str, project_name: str, base_namespace: str, used_by_others: bool) -> bool:
    """Whether the AppProject is out of the directory; only when nothing points at it, since
    its finalizers would otherwise stall (case 1).
    """
    appproject_name = generate_argocd_appproject_prefix(project_name, base_namespace)
    if used_by_others:
        logger.info(f"Keeping AppProject file '{appproject_name}': another deployment still uses this namespace")
        return False
    pointing = applications_pointing_at_appproject(project_dir, appproject_name)
    if pointing:
        logger.info(f"Keeping AppProject file '{appproject_name}': still named by {', '.join(pointing)}")
        return False

    appproject_filename = get_output_filename_from_template("argocd-appproject.yaml.jinja", appproject_name)
    appproject_file_path = os.path.join(project_dir, appproject_filename)
    if os.path.exists(appproject_file_path):
        os.remove(appproject_file_path)
        logger.info(f"Deleted AppProject file: {appproject_filename}")
    return True
