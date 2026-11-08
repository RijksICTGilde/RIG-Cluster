"""The variables an alias may use, grouped per service.

The list is not written down here but derived from the same source as the validation: the
service definitions ``variable_to_service()`` in references.py walks as well. A service that
gains a variable therefore shows up in the help by itself, and the help can never name a
variable the validation rejects.

Per DEFINITION and not through the name-to-service map of ``variable_to_service()``: that map
is keyed on variable name, so of two services providing the same variables (minio-storage and
seaweedfs-storage, redis and namespace-redis) only one keeps its entry and the other
disappears from the help.
"""

from __future__ import annotations

from dataclasses import dataclass

from opi.services.services import ServiceAdapter, ServiceType


@dataclass(frozen=True)
class AliasVariable:
    naam: str
    beschrijving: str
    andere_namen: list[str]
    """Alternative names. They resolve to the same value at deploy time, so they are as
    valid in an alias as the primary name."""


@dataclass(frozen=True)
class AliasService:
    naam: str
    label: str
    icoon: str
    variabelen: list[AliasVariable]


def alias_variabelen() -> list[AliasService]:
    """Every service that provides variables, with its variables, sorted by name.

    Only the PRIMARY name gets a line of its own; the alternatives are named alongside it.
    """
    per_service: dict[ServiceType, list[AliasVariable]] = {}
    for service_type in ServiceAdapter.SERVICE_DEFINITIONS:
        for var_def in ServiceAdapter.get_service_definition(service_type).variables:
            per_service.setdefault(service_type, []).append(
                AliasVariable(
                    naam=var_def.name,
                    beschrijving=var_def.description,
                    andere_namen=list(var_def.aliases),
                )
            )

    services: list[AliasService] = []
    for service_type, variables in per_service.items():
        definition = ServiceAdapter.get_service_definition(service_type)
        services.append(
            AliasService(
                naam=service_type.value,
                label=definition.name,
                icoon=definition.icon,
                variabelen=sorted(variables, key=lambda v: v.naam),
            )
        )
    return sorted(services, key=lambda d: d.label)
