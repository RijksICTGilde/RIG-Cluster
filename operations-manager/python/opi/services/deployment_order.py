"""Process deployments in an order their clone dependencies allow.

A deployment can clone its database, buckets and volumes from another deployment in the
same project (``clone-from: {type: deployment, reference: <name>}``). That only works if
the source has been provisioned first, so the order in which deployments are processed is
not free.

Nothing enforced it. ``process_project`` walked ``project_data["deployments"]`` in file
order, and that happened to work only because the files grew that way: in ``regel-k4c``
the source ``regelrecht`` sits first and four PR deployments clone from it; in ``wies``
``main`` sits third and fourteen clone from it. Adding a deployment at the top of the file
would have broken it silently.

Production rarely notices, because a ``mode: once`` clone is long done and its result is
recorded in ``revisions``. A fresh environment notices immediately, which is exactly where
the upgrade-safety test runs and where this surfaced.
"""

from __future__ import annotations

import logging
from typing import Any

from opi.services.services_enums import CloneFromType

logger = logging.getLogger(__name__)


def clone_source_name(deployment: dict[str, Any]) -> str | None:
    """The deployment this one clones from, or None.

    Only ``type: deployment`` constrains the order within a project. A backup or a
    remote source lives outside the project, so it imposes nothing here.
    """
    clone_from = deployment.get("clone-from")
    if not isinstance(clone_from, dict):
        return None
    if clone_from.get("type") != CloneFromType.DEPLOYMENT.value:
        return None
    reference = clone_from.get("reference")
    return reference if isinstance(reference, str) and reference else None


def order_deployments_by_clone_dependency(deployments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the deployments with every clone source ahead of the deployment cloning it.

    A stable topological sort: deployments that constrain nothing keep their file order,
    so a project without clones comes back byte-identical and diffs stay readable.

    A reference to a deployment that is not in the list is left alone rather than treated
    as an error. That happens for real: ``wies`` has ``pr-274`` cloning from ``staging``,
    which no longer exists in the file (removed after the clone was long done). Refusing
    to order such a project would turn a historical leftover into a hard stop, and that is
    the failure mode where a project silently stops deploying with nobody seeing an error.
    Whether the source still exists is a question for provisioning, which can tell the
    difference between "already cloned" and "cannot clone", and can say so out loud.

    Raises:
        ValueError: if the clone references form a cycle. There is no order that satisfies
            it, so proceeding would mean picking one arbitrarily and failing later on.
    """
    by_name = {name: d for d in deployments if (name := d.get("name"))}

    ordered: list[dict[str, Any]] = []
    placed: set[str] = set()
    # Names on the current recursion path, so a cycle is reported with the path that shows it.
    visiting: list[str] = []

    def place(deployment: dict[str, Any]) -> None:
        name = deployment.get("name")
        if name in placed:
            return
        if name in visiting:
            cycle = " -> ".join([*visiting[visiting.index(name) :], name])
            raise ValueError(f"Circular clone-from dependency between deployments: {cycle}")

        source_name = clone_source_name(deployment)
        source = by_name.get(source_name) if source_name else None
        if source is not None:
            visiting.append(name)
            place(source)
            visiting.pop()

        placed.add(name)
        ordered.append(deployment)

    for deployment in deployments:
        place(deployment)

    if [d.get("name") for d in ordered] != [d.get("name") for d in deployments]:
        logger.info(f"Reordered deployments so clone sources are provisioned first: {[d.get('name') for d in ordered]}")

    return ordered
