"""The portal request as the authority a project edit weighs its change against."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from opi.core.request_user import get_current_user
from opi.http.auth.authz import authorize
from opi.services.step_up import has_fresh_step_up

if TYPE_CHECKING:
    from fastapi import Request

    from opi.core.authz.actions import Action

#: What the audit and the log name when the session carries no address.
UNKNOWN_ACTOR = "(onbekend)"


@dataclass(frozen=True)
class RequestAuthority:
    """Implements :class:`~opi.manager.project.edit_authority.EditAuthority` for a request."""

    request: Request
    project_name: str

    def require(self, action: Action, *, deployment: str | None = None) -> None:
        authorize(self.request, action=action, project_name=self.project_name, deployment=deployment)

    def step_up_is_fresh(self) -> bool:
        return has_fresh_step_up(self.request)

    @property
    def actor(self) -> str:
        return str((get_current_user(self.request) or {}).get("email", UNKNOWN_ACTOR))
