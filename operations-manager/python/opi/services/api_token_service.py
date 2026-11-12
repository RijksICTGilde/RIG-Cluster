"""Manage members and their tokens: who may do what, and what is a valid request.

A layer over :class:`opi.services.project_store.ProjectStore`, so the API and the form walk
the same path: the expiry date, the requester's role and the shape of a name live here and
not in a route.

Membership in the project file is the only source of what anyone may do here. A platform
admin who is not a member of this project manages no members and no tokens either: a token
is never more than the member it belongs to, and whoever is not a member has none.

Everything that happens here is a COMMIT on the project file, so issuing, revoking and
changing a role sit in the same reviewable history as the rest of a project.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from opi.core.actor import user_actor
from opi.core.authz.engine import Role, effective_roles
from opi.core.config import settings
from opi.services.members import MemberKind
from opi.services.project_members import TOKEN_PREFIX, expiry_date, hash_of, is_expired, member_named, new_token
from opi.services.project_roles import roles_from_value
from opi.services.project_store import get_project_store

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime

    from opi.services.project_service import ProjectUser

logger = logging.getLogger(__name__)

#: The roles a member can hold, as keys. Read from the register, so a role added there
#: cannot be forgotten here.
ROLES: tuple[str, ...] = tuple(role.value for role in Role)

#: The roles a new member gets preselected. A prefill and not a fixed grant: the admin who
#: adds the member chooses.
DEFAULT_ROLES: tuple[str, ...] = (Role.DEVELOPER.value,)

#: A service-account name is a DNS label: it travels along in the actor and in the git
#: trailer, so it has to be writable as a label everywhere.
NAME_PATTERN = re.compile(r"^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$")

_TOKEN_NAME_PATTERN = re.compile(r"^[\w .-]{1,64}$")


class TokenRequestRefusedError(Exception):
    """The request is invalid, or the requester may not make it. The message is for the user."""


def _token_expiry(days: int, maximum: int) -> datetime:
    """The expiry date of a new token, or a refusal. There is no branch without an expiry."""
    if days < 1:
        raise TokenRequestRefusedError("Een token moet minstens een dag geldig zijn.")
    if days > maximum:
        raise TokenRequestRefusedError(f"Een token mag hoogstens {maximum} dagen geldig zijn.")
    return expiry_date(days)


def _member(project_name: str, name: str) -> ProjectUser | None:
    """The member of this project that goes by this name, or None when it is not there."""
    project = get_project_store().get(project_name)
    return member_named(project, name) if project is not None else None


def _roles_of(project_name: str, name: str) -> list[str]:
    """This member's current roles in this project; empty when it is not a member."""
    member = _member(project_name, name)
    return list(member.roles) if member is not None else []


def _require_member(project_name: str, by: str) -> None:
    """Refuse whoever is not a member of this project, a platform admin included.

    Without this, a revoke on an EXISTING project was a silent no-op and on a non-existent
    one a 500, and that difference reads as "does this project name exist".
    """
    if not _roles_of(project_name, by):
        raise TokenRequestRefusedError("Je bent geen lid van dit project.")


def is_project_admin(project_name: str, name: str) -> bool:
    """Whether this member may manage the members and tokens of this project.

    For the interface, so a button reads the same source as the refusal behind it: a platform
    admin who is not a member gets False here and so does not see the buttons.
    """
    return Role.ADMIN in effective_roles(roles_from_value(_roles_of(project_name, name)))


def _require_admin(project_name: str, by: str) -> None:
    if Role.ADMIN not in effective_roles(roles_from_value(_roles_of(project_name, by))):
        raise TokenRequestRefusedError("Alleen een beheerder van dit project kan dit doen.")


def _members(data: dict[str, Any]) -> list[dict[str, Any]]:
    members = data.get("users")
    if not isinstance(members, list):
        raise TokenRequestRefusedError("Dit projectbestand heeft geen ledenlijst.")
    return members


def _find_member(data: dict[str, Any], name: str) -> dict[str, Any] | None:
    for member in _members(data):
        if not isinstance(member, dict):
            continue
        own = member.get("name") if member.get("type") == MemberKind.SERVICE_ACCOUNT.value else member.get("email")
        if str(own or "").lower() == name.lower():
            return member
    return None


def _require_token_name(name: str) -> str:
    cleaned = name.strip()
    if not _TOKEN_NAME_PATTERN.match(cleaned):
        raise TokenRequestRefusedError("Geef het token een korte naam van letters, cijfers, spatie, punt of streepje.")
    return cleaned


async def _mutate(project_name: str, change: Any, *, message: str, by: str) -> None:
    await get_project_store().mutate(project_name, change, message=message, actor=user_actor(by))


async def add_service_account(*, project_name: str, name: str, roles: list[str], by: str) -> None:
    """Put a service account in this project's member list."""
    _require_admin(project_name, by)
    if not NAME_PATTERN.match(name):
        raise TokenRequestRefusedError(
            "Een naam bestaat uit kleine letters, cijfers en streepjes, en begint met een letter."
        )
    chosen = clean_roles(roles)

    def change(data: dict[str, Any]) -> dict[str, Any] | None:
        if _find_member(data, name) is not None:
            return None
        _members(data).append({"type": MemberKind.SERVICE_ACCOUNT.value, "name": name, "roles": chosen, "tokens": []})
        return data

    await _mutate(project_name, change, message=f"Service account '{name}' toegevoegd", by=by)


async def remove_service_account(*, project_name: str, name: str, by: str) -> None:
    """Take a service account out of the member list. Its tokens go with it."""
    _require_admin(project_name, by)

    def change(data: dict[str, Any]) -> dict[str, Any] | None:
        member = _find_member(data, name)
        if member is None or member.get("type") != MemberKind.SERVICE_ACCOUNT.value:
            return None
        _members(data).remove(member)
        return data

    await _mutate(project_name, change, message=f"Service account '{name}' verwijderd", by=by)


def clean_roles(roles: Iterable[str]) -> list[str]:
    """The chosen roles as a valid, deduplicated, sorted list.

    Raises TokenRequestRefusedError on an empty list or a role that does not exist.
    """
    chosen = sorted({str(role) for role in roles if str(role)})
    if not chosen:
        raise TokenRequestRefusedError("Kies minstens een rol.")
    unknown = [role for role in chosen if role not in ROLES]
    if unknown:
        raise TokenRequestRefusedError(f"Onbekende rol: {', '.join(unknown)}.")
    return chosen


def require_an_admin(data: dict[str, Any]) -> None:
    """Refuse a member list in which nobody holds the admin role.

    The ONE reader of that rule: both a per-member role change and the team wizard, which
    writes the whole list at once, pass through here.

    Raises TokenRequestRefusedError when no member with the admin role is left.
    """
    if not any(
        isinstance(member, dict) and Role.ADMIN.value in (member.get("roles") or []) for member in _members(data)
    ):
        raise TokenRequestRefusedError("Een project houdt minstens een lid met de rol admin.")


def set_roles_in_data(data: dict[str, Any], name: str, roles: list[str]) -> dict[str, Any] | None:
    """Set this member's roles in the project data; None when nothing changes.

    Apart from :func:`set_member_roles`, which reads and writes the file: this step only
    touches the data.

    Raises TokenRequestRefusedError when the member is not there, or when the project would
    be left without an admin.
    """
    member = _find_member(data, name)
    if member is None:
        raise TokenRequestRefusedError(f"'{name}' is geen lid van dit project.")
    if list(member.get("roles") or []) == roles:
        return None
    previous = member.get("roles")
    member["roles"] = roles
    try:
        require_an_admin(data)
    except TokenRequestRefusedError:
        member["roles"] = previous
        raise
    return data


async def set_member_roles(*, project_name: str, name: str, roles: list[str], by: str) -> None:
    """Give a member other roles. Its tokens follow those roles at once.

    The ONE write path for roles: the Team card, the API route and the team wizard all pass
    through here, so the three cannot drift apart on what a valid role is or on the rule that
    an admin has to be left.
    """
    _require_admin(project_name, by)
    chosen = clean_roles(roles)

    def change(data: dict[str, Any]) -> dict[str, Any] | None:
        return set_roles_in_data(data, name, chosen)

    await _mutate(project_name, change, message=f"Rollen van '{name}' zijn nu {chosen}", by=by)


def _require_may_issue(project_name: str, name: str, by: str) -> int:
    """Refuse whoever may not give this member a token, and return the platform maximum.

    An admin does that for a service account of their project; a human does it for
    themselves. A human therefore cannot make a token for someone else: that would be a
    credential another person carries without knowing they have it.

    Apart from :func:`issue_token`, so :func:`rotate_token` can check this rule BEFORE the
    old token goes out. Otherwise a refused rotation would have lost the token.
    """
    _require_member(project_name, by)
    target = _member(project_name, name)
    if target is None:
        raise TokenRequestRefusedError(f"'{name}' is geen lid van dit project.")

    if target.type is MemberKind.SERVICE_ACCOUNT:
        _require_admin(project_name, by)
        return settings.TOKEN_MAX_DAGEN_SERVICE_ACCOUNT
    if name.lower() != by.lower():
        raise TokenRequestRefusedError("Je kunt alleen een token voor jezelf maken.")
    return settings.TOKEN_MAX_DAGEN_PERSOONLIJK


async def issue_token(*, project_name: str, name: str, token_name: str, days: int, by: str) -> str:
    """Issue a token to the member ``name``. The value comes from here once and never again."""
    token_name = _require_token_name(token_name)
    maximum = _require_may_issue(project_name, name, by)
    expires = _token_expiry(days, maximum)
    value, hash_hex = new_token()

    def change(data: dict[str, Any]) -> dict[str, Any] | None:
        member = _find_member(data, name)
        if member is None:
            raise TokenRequestRefusedError(f"'{name}' is geen lid van dit project.")
        tokens = member.setdefault("tokens", [])
        if any(isinstance(t, dict) and t.get("name") == token_name for t in tokens):
            raise TokenRequestRefusedError(f"'{name}' heeft al een token dat '{token_name}' heet.")
        tokens.append({"name": token_name, "expires": expires.isoformat(), "hash": hash_hex})
        return data

    await _mutate(project_name, change, message=f"Token '{token_name}' uitgegeven aan '{name}'", by=by)
    logger.info("Token '%s' uitgegeven aan '%s' in project '%s' door '%s'", token_name, name, project_name, by)
    return value


async def revoke_token(*, project_name: str, name: str, token_name: str, by: str) -> None:
    """Revoke a token by taking it out of the project file.

    An admin revokes every token of their project, a human's included; a human revokes their
    own tokens. Both are members of the project: a platform admin who is not revokes nothing
    here. Revoking is a commit, and so visible in the history.
    """
    _require_member(project_name, by)
    if name.lower() != by.lower():
        _require_admin(project_name, by)

    def change(data: dict[str, Any]) -> dict[str, Any] | None:
        member = _find_member(data, name)
        if member is None:
            return None
        tokens = member.get("tokens") or []
        remaining = [t for t in tokens if not (isinstance(t, dict) and t.get("name") == token_name)]
        if len(remaining) == len(tokens):
            return None
        member["tokens"] = remaining
        return data

    await _mutate(project_name, change, message=f"Token '{token_name}' van '{name}' ingetrokken", by=by)
    logger.info("Token '%s' van '%s' ingetrokken door '%s'", token_name, name, by)


async def rotate_token(*, project_name: str, name: str, token_name: str, days: int, by: str) -> str:
    """Revoke this token and issue a new one under the same name.

    Issuing is checked FIRST: revoking is allowed to an admin for EVERY member and issuing
    only for a service account, so without that check an admin rotating a colleague's token
    lost it on a response that said nothing had happened.
    """
    _require_may_issue(project_name, name, by)
    _require_token_name(token_name)
    await revoke_token(project_name=project_name, name=name, token_name=token_name, by=by)
    return await issue_token(project_name=project_name, name=name, token_name=token_name, days=days, by=by)


def members_overview(project_name: str) -> list[dict[str, Any]]:
    """This project's members with their tokens, for the interface and the API.

    Without hashes: whoever holds a hash can check a guessed token offline.
    """
    project = get_project_store().get(project_name)
    return [
        {
            "naam": member.principal,
            "soort": member.type.value,
            "rollen": list(member.roles),
            "rol_labels": member.rol_labels,
            "tokens": [
                {"naam": t.name, "vervalt": t.expires, "verlopen": is_expired(t.expires)} for t in member.tokens
            ],
        }
        for member in (project.users or [] if project else [])
    ]


__all__ = [
    "DEFAULT_ROLES",
    "NAME_PATTERN",
    "ROLES",
    "TOKEN_PREFIX",
    "TokenRequestRefusedError",
    "add_service_account",
    "clean_roles",
    "hash_of",
    "is_project_admin",
    "issue_token",
    "members_overview",
    "remove_service_account",
    "require_an_admin",
    "revoke_token",
    "rotate_token",
    "set_member_roles",
    "set_roles_in_data",
]
