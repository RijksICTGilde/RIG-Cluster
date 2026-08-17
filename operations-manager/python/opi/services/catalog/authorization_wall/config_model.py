"""Config model for the ``authorization-wall`` service (RC-5 Phase 2).

Today this config is schema-unconstrained; the manager reads only ``banner``
(project_manager.py, authorization_wall handling). v1.0 models exactly that: a
single optional banner string. The auth wall additionally requires a keycloak
service to activate, but that is a cross-service dependency (handled by
``ServiceDefinition.requires``), not part of this config's shape.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class AuthorizationWallConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    banner: str | None = Field(default=None, description="Text shown on the sign-in page in front of the application.")
