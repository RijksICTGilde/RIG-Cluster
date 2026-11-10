"""Read a project's members, telling a human apart from a service account.

``users`` in a project file carries two kinds of member. Everything that happens to a HUMAN
(a Keycloak user, an invite, mail, OTP, the login list) must see only the humans, and
everything that happens to a CREDENTIAL sees both. The distinction lives here in one place,
and ``tests/test_human_members_guard.py`` sees who skips it.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from opi.services.members import MemberKind

if TYPE_CHECKING:
    from opi.services.project_service import ProjectSummary, ProjectUser

#: What an issued token starts with. The old project key has no prefix, so this prefix is how
#: a reader tells a token from the key the 2.12 migration carried over.
TOKEN_PREFIX = "zad_"  # noqa: S105  # a token's prefix, not a secret

#: Random bytes in a token. 32 bytes is too much to guess, so the hash below need not be a
#: password hash.
TOKEN_BYTES = 32


def new_token() -> tuple[str, str]:
    """A new token and its hash. The value comes from here once and never again."""
    value = TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_BYTES)
    return value, hash_of(value)


def hash_of(value: str) -> str:
    """A token's stored form: sha256 in hex, never the value itself."""
    return hashlib.sha256(value.encode()).hexdigest()


def expiry_date(days: int) -> datetime:
    """The expiry date of a token that runs out in ``days`` days."""
    return datetime.now(UTC) + timedelta(days=days)


def is_expired(expires: datetime, *, now: datetime | None = None) -> bool:
    """Whether this expiry date has passed. A date without a timezone counts as UTC."""
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    return expires <= (now or datetime.now(UTC))


def human_members(project: ProjectSummary) -> list[ProjectUser]:
    """The HUMANS in this project, without the service accounts.

    Every reader that creates a Keycloak user, sends an invite or mail, offers OTP or fills
    the login list uses this function. A service account has no mailbox and no second factor,
    so it would yield a user nobody can use that does grant access.
    """
    return [member for member in project.users or [] if member.type is not MemberKind.SERVICE_ACCOUNT]


def human_emails(project: ProjectSummary) -> list[str]:
    """The email addresses of the humans in this project."""
    return [member.email for member in human_members(project) if member.email]


def service_accounts(project: ProjectSummary) -> list[ProjectUser]:
    """The service accounts in this project."""
    return [member for member in project.users or [] if member.type is MemberKind.SERVICE_ACCOUNT]


def member_named(project: ProjectSummary, name: str) -> ProjectUser | None:
    """The member that goes by this name: an email address, or a service account's name."""
    for member in project.users or []:
        if member.principal.lower() == name.lower():
            return member
    return None


def human_members_from_data(data: dict[str, Any]) -> list[dict[str, Any]]:
    """The same filter on RAW project data, for readers that have no ProjectSummary."""
    members = data.get("users")
    if not isinstance(members, list):
        return []
    return [
        member
        for member in members
        if isinstance(member, dict) and member.get("type", MemberKind.PERSON.value) != MemberKind.SERVICE_ACCOUNT.value
    ]
