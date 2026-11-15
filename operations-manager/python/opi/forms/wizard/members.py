"""What the team form owns of the member list, and what it puts back.

``users`` carries two kinds of member and the token hashes of both, while the team form
renders only the email address and the roles of a PERSON. A form that writes that list as a
whole therefore throws away every token and every service account, under an action called
"Projectleden bijgewerkt". This is the same separation :mod:`opi.forms.wizard.secrets` makes
for encrypted values: keep out of the form what it does not edit, and put it back on save.
"""

from __future__ import annotations

import copy
import logging
from typing import Any

from opi.services.api_token_service import TokenRequestRefusedError, clean_roles, require_an_admin
from opi.services.members import MemberKind, member_list_changed

logger = logging.getLogger(__name__)

#: Fields of a member the form does not render and therefore does not write either.
HIDDEN_MEMBER_FIELDS = ("tokens",)


class MemberListRefusedError(ValueError):
    """The submitted member list may not be saved as it is."""


def _is_service_account(member: dict[str, Any]) -> bool:
    return member.get("type") == MemberKind.SERVICE_ACCOUNT.value


def members_for_form(data: dict[str, Any]) -> dict[str, Any]:
    """*data* with only the people in ``users``, without their hidden fields.

    A service-account row in the form lands as a member without an address, and no outcome
    lets it survive: emptying it trips the required-field check, removing it deletes the
    account, and filling in an address makes the schema refuse the row.
    """
    members = data.get("users")
    if not isinstance(members, list):
        return data
    people = [
        {k: v for k, v in member.items() if k not in HIDDEN_MEMBER_FIELDS}
        for member in members
        if isinstance(member, dict) and not _is_service_account(member)
    ]
    return {**data, "users": people}


def restore_hidden_members(merged_data: dict[str, Any], stored_data: dict[str, Any]) -> None:
    """Put back into ``merged_data`` what the form does not carry of ``users``.

    The service accounts come back as the file holds them, and a person's tokens come back on
    their email address. Whoever was removed from the list loses their tokens, which is what
    removing a member is supposed to do.
    """
    submitted = merged_data.get("users")
    stored = stored_data.get("users")
    if not isinstance(submitted, list) or not isinstance(stored, list):
        return

    service_accounts = [member for member in stored if isinstance(member, dict) and _is_service_account(member)]
    hidden = {
        str(member["email"]).lower(): member
        for member in stored
        if isinstance(member, dict) and not _is_service_account(member) and member.get("email")
    }

    people: list[Any] = []
    for member in submitted:
        if not isinstance(member, dict):
            people.append(member)
            continue
        if _is_service_account(member):
            # The form creates no service account, so such a row did not come out of it.
            continue
        new_member = {k: v for k, v in member.items() if k not in HIDDEN_MEMBER_FIELDS}
        stored_member = hidden.get(str(member.get("email") or "").lower())
        if stored_member:
            new_member.update(
                {field: copy.deepcopy(stored_member[field]) for field in HIDDEN_MEMBER_FIELDS if field in stored_member}
            )
        people.append(new_member)

    remaining = {str(member.get("email") or "").lower() for member in people if isinstance(member, dict)}
    departed = [address for address, member in hidden.items() if address not in remaining and member.get("tokens")]
    if departed:
        logger.info("Tokens ingetrokken met het lid dat ze bezat: %s", ", ".join(sorted(departed)))

    merged_data["users"] = people + copy.deepcopy(service_accounts)


def guard_member_list(merged_data: dict[str, Any], stored_data: dict[str, Any], *, owns_users: bool) -> None:
    """Refuse a submission that writes ``users`` without owning that list.

    A flow that does not render the team form renders no roles either, so a submission that
    brings ``users`` along differently did not come out of a form. Refused rather than
    ignored, so a manipulated POST visibly bounces.

    The three rules ABOUT the roles themselves come from :mod:`opi.services.api_token_service`:
    the wizard writes the whole list at once and the role route one member at a time, but what
    a valid role is, and that an admin is left, lives in one place.

    Raises ``MemberListRefusedError`` on a change to ``users`` by a flow that does not own it,
    on an unknown role, on a member without roles, or when the project would be left without
    an admin.
    """
    submitted = merged_data.get("users")
    if not owns_users:
        if member_list_changed(stored_data, merged_data):
            raise MemberListRefusedError("Deze stap bewerkt de projectleden niet.")
        return

    if not isinstance(submitted, list) or not submitted:
        raise MemberListRefusedError("Een project houdt minstens een lid.")

    for member in submitted:
        if not isinstance(member, dict):
            continue
        try:
            clean_roles(member.get("roles") or [])
        except TokenRequestRefusedError as exc:
            raise MemberListRefusedError(f"{exc} ({member.get('email') or member.get('name')})") from exc

    try:
        require_an_admin(merged_data)
    except TokenRequestRefusedError as exc:
        raise MemberListRefusedError(str(exc)) from exc
