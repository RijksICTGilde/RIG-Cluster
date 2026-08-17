"""Typed config model for the ``redis`` service.

One project-level setting. Each deployment gets a dedicated Redis ACL user restricted to
keys prefixed with ``{deployment}-{project}:``; applications that cannot prefix their keys
(Grist, for one) turn that restriction off. Read in ``opi/manager/redis_manager.py``, where
the default has always been ``True``, so leaving the key out keeps the restriction on.

The ``namespace-redis`` variant shares the manager but not this model: it has no config
block in any project file, so it stays a behaviour-only service.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class RedisConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    acl_key_prefix: bool = Field(
        default=True,
        alias="acl-key-prefix",
        description=(
            "Restrict this project's Redis user to keys prefixed with {deployment}-{project}:. Turning it off "
            "gives the user every key in the shared instance, so it is a deliberate widening for applications "
            "that cannot prefix their keys."
        ),
    )
