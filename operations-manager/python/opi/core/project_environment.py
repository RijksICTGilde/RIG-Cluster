"""De OTAP-markering van een deployment.

Eén plek voor de waarden, de standaard en het aflezen. Het veld heet
``environment`` in het projectbestand; de waarden zijn de OTAP-termen.

De markering hoort bij een deployment en niet bij een project: de omgeving is een
eigenschap van een draaiende instantie, en een project heeft omgevingen in plaats
van er een te zijn. Een project toont daarom hooguit de hoogste markering van zijn
deployments (``highest_environment``), afgeleid en niet opgeslagen.

Naast ``opi.core.project_schema`` en niet bij de pydantic-modellen van de
formulierlaag: de API, de manager en de migratie lezen de markering ook, en die
mogen de formulierlaag niet importeren.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


class Omgeving(StrEnum):
    """De OTAP-fase waarin een deployment zit, van laag naar hoog."""

    ONTWIKKELING = "ontwikkeling"
    TEST = "test"
    ACCEPTATIE = "acceptatie"
    PRODUCTIE = "productie"


#: Wat een deployment zonder markering is. Toegepast bij het LEZEN, niet in elk
#: bestand weggeschreven: anders krijgt elk bestaand projectbestand een diff.
DEFAULT_ENVIRONMENT = Omgeving.ONTWIKKELING

#: Hoe de markering op het scherm heet.
ENVIRONMENT_LABELS: dict[Omgeving, str] = {
    Omgeving.ONTWIKKELING: "Ontwikkeling",
    Omgeving.TEST: "Test",
    Omgeving.ACCEPTATIE: "Acceptatie",
    Omgeving.PRODUCTIE: "Productie",
}

#: De volgorde van laag naar hoog, voor ``highest_environment``.
_ORDER: tuple[Omgeving, ...] = tuple(Omgeving)


def _parse(value: Any) -> Omgeving | None:
    """De markering zoals ze in het bestand staat, of None als ze er niet staat.

    Een onbekende waarde komt hier niet langs: het schema weigert hem.
    """
    if isinstance(value, Omgeving):
        return value
    if isinstance(value, str) and value in set(Omgeving):
        return Omgeving(value)
    return None


def deployment_environment(deployment: dict[str, Any]) -> Omgeving | None:
    """De markering zoals ze bij deze deployment in het bestand staat, of None."""
    return _parse(deployment.get("environment"))


def effective_environment(deployment: dict[str, Any]) -> Omgeving:
    """De markering die voor deze deployment geldt, met de standaard toegepast."""
    return deployment_environment(deployment) or DEFAULT_ENVIRONMENT


def environment_label(deployment: dict[str, Any]) -> str:
    """De effectieve markering van een deployment als label."""
    return ENVIRONMENT_LABELS[effective_environment(deployment)]


def highest_environment(project_data: dict[str, Any]) -> Omgeving:
    """De hoogste markering van de deployments van het project. Standaard ``ontwikkeling``."""
    return max(
        (effective_environment(deployment) for deployment in _deployments(project_data)),
        key=_ORDER.index,
        default=DEFAULT_ENVIRONMENT,
    )


def highest_environment_label(project_data: dict[str, Any]) -> str:
    """De hoogste markering van een project als label."""
    return ENVIRONMENT_LABELS[highest_environment(project_data)]


def is_production_downgrade(before: Omgeving, after: Omgeving) -> bool:
    """Of deze overgang een markering ``productie`` verlaat.

    De haak voor de step-up uit ``verwijderen-in-productie-vraagt-een-verse-otp.md``.
    """
    return before is Omgeving.PRODUCTIE and after is not Omgeving.PRODUCTIE


def environment_markings(project_data: dict[str, Any]) -> dict[str, Omgeving]:
    """De effectieve markering per deployment, om een wijziging tegen af te zetten."""
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
    """Meld elke deployment die ``productie`` verlaat op WARNING.

    Tot het derde plan er is, logt OPI een verlaging.
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
    """De deploymentsecties van een projectbestand, zonder de rommel ertussen."""
    return [entry for entry in project_data.get("deployments") or [] if isinstance(entry, dict)]
