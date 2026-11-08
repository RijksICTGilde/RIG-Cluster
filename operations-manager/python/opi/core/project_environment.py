"""The OTAP marking of a deployment.

One place for the values, the default and the reading. The field is called
``environment`` in the project file; the values are the OTAP terms.

The marking belongs to a deployment and not to a project: the environment is a property
of a running instance, and a project HAS environments instead of being one. A project
therefore shows at most the highest marking of its deployments
(``highest_environment``), derived and not stored.

Next to ``opi.core.project_schema`` and not with the pydantic models of the form layer:
the API, the manager and the migration read the marking too, and those may not import the
form layer.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


class Omgeving(StrEnum):
    """The OTAP phase a deployment is in, from low to high."""

    ONTWIKKELING = "ontwikkeling"
    TEST = "test"
    ACCEPTATIE = "acceptatie"
    PRODUCTIE = "productie"


#: What a deployment without a marking is. Applied on READING, not written into every
#: file: otherwise every existing project file gets a diff.
DEFAULT_ENVIRONMENT = Omgeving.ONTWIKKELING

#: What the marking is called on the screen.
ENVIRONMENT_LABELS: dict[Omgeving, str] = {
    Omgeving.ONTWIKKELING: "Ontwikkeling",
    Omgeving.TEST: "Test",
    Omgeving.ACCEPTATIE: "Acceptatie",
    Omgeving.PRODUCTIE: "Productie",
}

#: The order from low to high, for ``highest_environment``.
_ORDER: tuple[Omgeving, ...] = tuple(Omgeving)


def _parse(value: Any) -> Omgeving | None:
    """The marking as it stands in the file, or None when it is not there.

    An unknown value never gets here: the schema refuses it.
    """
    if isinstance(value, Omgeving):
        return value
    if isinstance(value, str) and value in set(Omgeving):
        return Omgeving(value)
    return None


def deployment_environment(deployment: dict[str, Any]) -> Omgeving | None:
    """The marking as it stands with this deployment in the file, or None."""
    return _parse(deployment.get("environment"))


def effective_environment(deployment: dict[str, Any]) -> Omgeving:
    """The marking that holds for this deployment, with the default applied."""
    return deployment_environment(deployment) or DEFAULT_ENVIRONMENT


def environment_label(deployment: dict[str, Any]) -> str:
    """The effective marking of a deployment, as a label."""
    return ENVIRONMENT_LABELS[effective_environment(deployment)]


def highest_environment(project_data: dict[str, Any]) -> Omgeving:
    """The highest marking among the project's deployments. ``ontwikkeling`` by default."""
    return max(
        (effective_environment(deployment) for deployment in _deployments(project_data)),
        key=_ORDER.index,
        default=DEFAULT_ENVIRONMENT,
    )


def is_production_downgrade(before: Omgeving, after: Omgeving) -> bool:
    """Whether this transition leaves a ``productie`` marking.

    The hook for the step-up from ``verwijderen-in-productie-vraagt-een-verse-otp.md``.
    """
    return before is Omgeving.PRODUCTIE and after is not Omgeving.PRODUCTIE


def environment_markings(project_data: dict[str, Any]) -> dict[str, Omgeving]:
    """The effective marking per deployment, to set a change against."""
    return {
        name: effective_environment(deployment)
        for deployment in _deployments(project_data)
        if (name := deployment.get("name"))
    }


def log_production_downgrades(
    before: dict[str, Omgeving],
    after: dict[str, Omgeving],
    *,
    project_name: str,
    actor: str,
) -> None:
    """Report every deployment that leaves ``productie`` at WARNING.

    Until the third plan exists, OPI logs a downgrade.
    """
    for name, before_value in before.items():
        after_value = after.get(name)
        if after_value is not None and is_production_downgrade(before_value, after_value):
            logger.warning(
                "OTAP-verlaging: deployment '%s' van project '%s' gaat van productie naar %s, door %s",
                name,
                project_name,
                after_value,
                actor,
            )


def _deployments(project_data: dict[str, Any]) -> list[dict[str, Any]]:
    """The deployment sections of a project file, without the junk in between."""
    return [entry for entry in project_data.get("deployments") or [] if isinstance(entry, dict)]
