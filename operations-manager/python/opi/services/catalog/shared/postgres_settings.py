"""De speelruimte van een eigen (CNPG) PostgreSQL-cluster (RC-327).

De velden zelf staan in ``shared/postgres.py``; hier staat hoe ver een project ermee mag
gaan. Via het mechanisme van RC-168 (``config_settings``), zodat de grens een keer wordt
opgeschreven en het formulier, de service-config-API en een met de hand bijgewerkt
projectbestand erlangs dezelfde kant op komen. Beide diensten die ``DedicatedPostgresFields``
gebruiken (``postgresql-database`` met ``scope: project`` en ``namespace-postgresql-database``)
dragen deze config op de projectlaag, dus ze delen deze declaraties.
"""

from __future__ import annotations

from typing import Final

from opi.services.catalog.base import ConfigLayer
from opi.services.catalog.config_settings import ConfigSetting, QuantityKind, QuantitySetting
from opi.services.catalog.shared.postgres import DatabaseResources
from opi.services.catalog.shared.storage import STORAGE_SIZES

#: ``grow_only``, want een PVC krimpt niet: een kleinere waarde doet of niets of hij laat
#: de uitrol vastlopen. De ondergrens is de kleinste maat die het platform aanbiedt. De
#: bovengrens is NIET die lijst: die loopt tot 1Gi, en gemeten over de beschikbare
#: projectbestanden staat er al 2Gi op een databasevolume (tests/fixtures/upgrade_safety/
#: multi-service.yaml, gesaneerd uit productie). 20Gi is de grootste maat die voor dit veld
#: ergens is opgeschreven, in features/namespace-postgresql-database.md; de meting over de
#: productie-repo kon in deze ronde niet, dus ligt de grens op wat er staat en niet lager.
POSTGRES_STORAGE: Final = QuantitySetting(
    path="storage",
    layers=(ConfigLayer.PROJECT,),
    default="1Gi",
    minimum=STORAGE_SIZES[0],
    maximum="20Gi",
    kind=QuantityKind.MEMORY,
    grow_only=True,
    label="Opslag",
)

#: De bovengrenzen zijn die van een gewone pod op dit platform: ``max_memory_request_mi``,
#: ``max_memory_limit_mi``, ``max_cpu_request_m`` en ``max_cpu_limit_m`` uit
#: ``opi/core/cluster_config.py``, met ``min_memory_limit_mi`` en ``min_cpu_m`` als
#: ondergrens. Als tekst en niet als aanroep, want een ``QuantitySetting`` heeft een vaste
#: grens en die van cluster_config hangt aan een cluster; alle drie de clusters dragen
#: vandaag dezelfde getallen. Gaan ze uit elkaar lopen, dan is een grens die het cluster
#: kan bevragen de eerstvolgende stap, niet een tweede lijstje hier.
POSTGRES_RESOURCES: Final[tuple[ConfigSetting, ...]] = (
    QuantitySetting(
        path="resources.requests.memory",
        layers=(ConfigLayer.PROJECT,),
        default=DatabaseResources().requests.memory,
        minimum="25Mi",
        maximum="1024Mi",
        kind=QuantityKind.MEMORY,
        label="Geheugen (request)",
    ),
    QuantitySetting(
        path="resources.limits.memory",
        layers=(ConfigLayer.PROJECT,),
        default=DatabaseResources().limits.memory,
        minimum="25Mi",
        maximum="4096Mi",
        kind=QuantityKind.MEMORY,
        label="Geheugen (limiet)",
    ),
    QuantitySetting(
        path="resources.requests.cpu",
        layers=(ConfigLayer.PROJECT,),
        default=DatabaseResources().requests.cpu,
        minimum="25m",
        maximum="250m",
        kind=QuantityKind.CPU,
        label="CPU (request)",
    ),
    QuantitySetting(
        path="resources.limits.cpu",
        layers=(ConfigLayer.PROJECT,),
        default=DatabaseResources().limits.cpu,
        minimum="25m",
        maximum="4000m",
        kind=QuantityKind.CPU,
        label="CPU (limiet)",
    ),
)

#: Wat een project van een eigen PostgreSQL-cluster mag zetten. ``image`` staat er NIET
#: bij: daarvoor zou een ``ChoiceSetting`` een gesloten lijst images nodig hebben, en die
#: is niet af te leiden uit wat er staat (productie draait een eigen CNPG-image met een
#: eigen tag, en features/namespace-postgresql-database.md laat eigen images toe). De vorm
#: van dat veld wordt getoetst op het model zelf.
DEDICATED_POSTGRES_SETTINGS: Final[tuple[ConfigSetting, ...]] = (POSTGRES_STORAGE, *POSTGRES_RESOURCES)
