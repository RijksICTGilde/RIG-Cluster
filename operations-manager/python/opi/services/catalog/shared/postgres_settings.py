"""The room a project has on its own (CNPG) PostgreSQL cluster.

The fields themselves live in ``shared/postgres.py``; how far a project may go with them lives
here. Through ``config_settings``, so the bound is written down once and the form, the
service-config API and a hand-edited project file all come past the same side of it. Both
services using ``DedicatedPostgresFields`` (``postgresql-database`` with ``scope: project``
and ``namespace-postgresql-database``) carry this config on the project layer, so they share
these declarations.
"""

from __future__ import annotations

from typing import Final

from opi.services.catalog.base import ConfigLayer
from opi.services.catalog.config_settings import ConfigSetting, QuantityKind, QuantitySetting
from opi.services.catalog.shared.postgres import DatabaseResources
from opi.services.catalog.shared.storage import STORAGE_SIZES

#: ``grow_only``, because a PVC does not shrink: a smaller value either does nothing or jams
#: the rollout. The lower bound is the smallest size the platform offers. The upper bound is
#: NOT that list: it runs to 1Gi, while a database volume in practice already carries 2Gi.
#: 20Gi is the largest size written down for this field anywhere, in
#: features/namespace-postgresql-database.md.
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

#: The upper bounds are those of an ordinary pod on this platform: ``max_memory_request_mi``,
#: ``max_memory_limit_mi``, ``max_cpu_request_m`` and ``max_cpu_limit_m`` from
#: ``opi/core/cluster_config.py``, with ``min_memory_limit_mi`` and ``min_cpu_m`` as the lower
#: bound. As literals and not as a call, because a ``QuantitySetting`` has a fixed bound while
#: cluster_config's hangs on a cluster; all three clusters carry the same numbers today. Should
#: they drift apart, a bound that can query the cluster is the next step, not a second list
#: here.
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

#: What a project may set on its own PostgreSQL cluster. ``image`` is NOT among them: a
#: ``ChoiceSetting`` would need a closed list of images for that, and such a list cannot be
#: derived from what is written (production runs its own CNPG image with its own tag, and
#: features/namespace-postgresql-database.md allows private images). The shape of that field is
#: checked on the model itself.
DEDICATED_POSTGRES_SETTINGS: Final[tuple[ConfigSetting, ...]] = (POSTGRES_STORAGE, *POSTGRES_RESOURCES)
