"""Which fields in a service config the PLATFORM writes, so a caller cannot wipe them.

A config block mixes two kinds of field. Most are settings a user chooses. A few are
written by OPI itself and exist nowhere else: ``keycloak.realms`` carries the host, the
realm name and the AGE-encrypted password of the realm-admin account OPI manages the
realm with.

The generic PUT replaces the WHOLE config block with what the caller sent, dumped with
``exclude_unset=True``. So::

    PUT /api/v2/projects/{p}/services/keycloak/config/project  {"template": "sso-only"}

used to leave a config of exactly ``{"template": "sso-only"}`` -- and the realm admin
password was gone. Nothing else holds it, so the project then wedges on the
duplicate-admin guard in ``keycloak_manager`` and the only ways out are the git history
of the project file or an administrator on the master realm. The caller never mentioned
``realms``; leaving a field out is not a request to destroy it.

A field says so itself, next to its own description::

    realms: list[KeycloakRealm] = Field(
        default_factory=list,
        json_schema_extra={PLATFORM_MANAGED: True},
        description="Per-cluster realm admin connections. Written and managed by the platform.",
    )

On the field rather than in a list of key names elsewhere, for two reasons. It cannot
drift from the field it protects -- a renamed field takes its marking along, where a
separate list would quietly protect a key that no longer exists. And it reaches the
JSON-schema fragment and `/openapi.json`, so a client can see that the field is not
its to send instead of finding out by losing it.

Preserved at the one pure mutator every write path goes through,
``ServiceAdapter.set_service_config`` (and its DELETE sibling), so a new write path
inherits the protection rather than having to remember it.
"""

from __future__ import annotations

from pydantic import BaseModel

#: Marker in a field's ``json_schema_extra``: the platform writes this, a caller does not.
PLATFORM_MANAGED = "x-platform-managed"


def platform_managed_keys(config_model: type | None) -> frozenset[str]:
    """The on-disk keys of the fields this model marks as platform-written.

    Empty for nearly every service: the marking is for the handful of fields OPI writes
    into a block a user also configures.
    """
    if not (isinstance(config_model, type) and issubclass(config_model, BaseModel)):
        return frozenset()
    keys: set[str] = set()
    for name, field in config_model.model_fields.items():
        extra = field.json_schema_extra
        if isinstance(extra, dict) and extra.get(PLATFORM_MANAGED):
            keys.add(field.alias or name)
    return frozenset(keys)
