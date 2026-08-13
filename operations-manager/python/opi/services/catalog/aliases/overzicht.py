"""De variabelen die je in een alias kunt gebruiken, gegroepeerd per dienst.

Waarom dit bestaat: niemand wist wat er in het aliassenveld hoorde. Het hulptekstje zei
"gebruik $VARIABELE_NAAM" zonder ergens te noemen WELKE namen er zijn, en die staan
verspreid over acht dienstdefinities. Wie het niet uit zijn hoofd wist, moest de code in.

De lijst wordt hier NIET opgeschreven maar afgeleid uit dezelfde bron als de validatie:
``variable_to_service()`` in references.py, die over alle dienstdefinities loopt. Een
dienst die er een variabele bij krijgt staat daarmee vanzelf in de uitleg, en de uitleg
kan nooit een naam noemen die de validatie afkeurt. Een tweede lijst zou binnen een
release uit de pas lopen.
"""

from __future__ import annotations

from dataclasses import dataclass

from opi.services.catalog.aliases.references import variable_to_service
from opi.services.services import ServiceAdapter, ServiceType


@dataclass(frozen=True)
class AliasVariabele:
    """Een variabele zoals hij in de uitleg staat."""

    naam: str
    beschrijving: str
    andere_namen: list[str]
    """Alternatieve namen. Ze lossen bij het uitrollen op naar dezelfde waarde, dus ze
    zijn in een alias net zo geldig als de primaire naam."""


@dataclass(frozen=True)
class AliasDienst:
    """Een dienst met de variabelen die hij levert."""

    naam: str
    label: str
    icoon: str
    variabelen: list[AliasVariabele]


def alias_variabelen() -> list[AliasDienst]:
    """Elke dienst die variabelen levert, met zijn variabelen, op naam gesorteerd.

    Alleen de PRIMAIRE naam wordt een eigen regel; de alternatieven staan erbij. Anders
    zou dezelfde waarde drie keer in de lijst staan en wordt de uitleg langer dan het
    probleem dat hij oplost.
    """
    per_dienst: dict[ServiceType, list[AliasVariabele]] = {}
    for service_type, var_def in variable_to_service().values():
        variabelen = per_dienst.setdefault(service_type, [])
        if any(bestaand.naam == var_def.name for bestaand in variabelen):
            continue
        variabelen.append(
            AliasVariabele(
                naam=var_def.name,
                beschrijving=var_def.description,
                andere_namen=list(var_def.aliases),
            )
        )

    diensten: list[AliasDienst] = []
    for service_type, variabelen in per_dienst.items():
        definitie = ServiceAdapter.get_service_definition(service_type)
        diensten.append(
            AliasDienst(
                naam=service_type.value,
                label=definitie.name,
                icoon=definitie.icon,
                variabelen=sorted(variabelen, key=lambda v: v.naam),
            )
        )
    return sorted(diensten, key=lambda d: d.label)
