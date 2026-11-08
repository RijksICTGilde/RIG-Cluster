"""What kind of member sits in the ``users`` list of a project file."""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class MemberKind(StrEnum):
    """A human, or a service account.

    A service account is not a separate list next to ``users`` but a member in that list: it
    carries the same roles and can hold the same tokens, but there is no human behind it. That
    difference counts for every reader that does something with a HUMAN (Keycloak, invitation,
    mail, OTP, the login list); those read
    :func:`opi.services.project_leden.human_members` and not ``users`` itself.
    """

    PERSON = "person"
    SERVICE_ACCOUNT = "service-account"


def member_list_changed(before: dict[str, Any], after: dict[str, Any]) -> bool:
    """Whether this save touches the member list or the roles of a member.

    Asked by a guard or a step-up, and here only once: the three paths that can write
    members have to see the same boundary.
    """
    return _members_with_roles(before) != _members_with_roles(after)


def _members_with_roles(data: dict[str, Any]) -> set[tuple[str, tuple[str, ...]]]:
    """The members with their roles, in a shape that compares on those two alone."""
    return {
        (
            str(member.get("email") or member.get("name") or ""),
            tuple(sorted(str(role) for role in (member.get("roles") or []))),
        )
        for member in data.get("users") or []
        if isinstance(member, dict)
    }
