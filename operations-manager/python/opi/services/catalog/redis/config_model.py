"""Typed config model for the ``redis`` service.

One project-level setting. Each deployment gets a dedicated Redis ACL user restricted to
keys prefixed with ``{deployment}-{project}:``; applications that cannot prefix their keys
(Grist, for one) turn that restriction off. Read in ``opi/manager/redis_manager.py``, where
the default has always been ``True``, so leaving the key out keeps the restriction on.
Turning it off is a request: it only takes effect once a platform admin approved it
(``acl-key-prefix-approval``, written by the approval flow).

The ``namespace-redis`` variant shares the manager but not this model: it has no config
block in any project file, so it stays a behaviour-only service.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from opi.services.config_managed import PLATFORM_MANAGED

#: De sleutel van het veld in het projectbestand, gedeeld door het model en de goedkeuring
#: die eraan hangt.
ACL_KEY_PREFIX_FIELD = "acl-key-prefix"


class AclKeyPrefixApproval(BaseModel):
    """Of een platformbeheerder het uitzetten van de sleutelbeperking heeft toegestaan.

    Zelfde vorm als het goedkeuringsblok van send-email, met de goedgekeurde ``value``
    erbij: een oordeel geldt voor de waarde waarover het ging. Geschreven door de
    goedkeuringsroute (``opi/services/approvals.py``).
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    status: str = Field(description="requested, approved of denied. Geschreven door de goedkeuringsroute.")
    value: bool | None = Field(
        default=None,
        description="De waarde van acl-key-prefix waarover dit oordeel ging.",
    )
    history: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Oordeelgeschiedenis: een {date, status, by, message}-regel per besluit.",
    )


class RedisConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    acl_key_prefix: bool = Field(
        default=True,
        alias=ACL_KEY_PREFIX_FIELD,
        description=(
            "Restrict this project's Redis user to keys prefixed with {deployment}-{project}:. Turning it off "
            "gives the user every key in the shared instance, so it is a deliberate widening for applications "
            "that cannot prefix their keys, and it needs the approval of a platform admin before it takes effect."
        ),
    )
    acl_key_prefix_approval: AclKeyPrefixApproval | None = Field(
        default=None,
        alias=f"{ACL_KEY_PREFIX_FIELD}-approval",
        json_schema_extra={PLATFORM_MANAGED: True},
        description=(
            "Of het uitzetten van de sleutelbeperking is goedgekeurd. Geschreven door het platform: zolang "
            "het niet approved zegt, houdt de Redis-gebruiker het prefix van het project."
        ),
    )
