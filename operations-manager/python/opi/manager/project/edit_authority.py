"""Who a project edit asks about the change it is about to make.

A protocol and not the portal request itself, so the use case weighs an action without
reaching into the entry layers. The adapter for an HTTP request lives in
:mod:`opi.http.auth.request_authority`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from opi.core.authz.actions import Action


class EditAuthority(Protocol):
    """The authority a project edit weighs its change against."""

    def require(self, action: Action, *, deployment: str | None = None) -> None:
        """Let ``action`` through on this project, or raise the entrance's own refusal."""
        ...

    def step_up_is_fresh(self) -> bool:
        """Whether the caller showed a second factor recently enough."""
        ...

    @property
    def actor(self) -> str:
        """Who is making the change, for the audit notice and the log."""
        ...
