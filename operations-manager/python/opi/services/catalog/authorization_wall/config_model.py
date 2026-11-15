"""Config model for the ``authorization-wall`` service.

One optional banner string, which is all the manager reads (project_manager.py,
authorization_wall handling). The auth wall additionally requires a keycloak service to
activate, but that is a cross-service dependency (handled by
``ServiceDefinition.requires``), not part of this config's shape.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class AuthorizationWallConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    banner: str | None = Field(default=None, description="Text shown on the sign-in page in front of the application.")
