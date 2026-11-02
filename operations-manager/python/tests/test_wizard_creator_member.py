"""The wizard seeds its creator as a member the schema accepts.

RC-392 turned a member's ``role`` into ``roles``; the seed kept writing ``role`` and every
project created through the wizard was rejected on ``users/0``.
"""

from __future__ import annotations

from jsonschema import Draft202012Validator
from opi.core.project_schema import _load_latest_schema
from opi.web.router_wizard import creator_as_member


def test_the_creator_is_a_valid_admin_member() -> None:
    schema = _load_latest_schema()
    member_schema = {**schema["$defs"]["user"], "$defs": schema["$defs"]}
    member = creator_as_member("maker@example.nl")

    assert list(Draft202012Validator(member_schema).iter_errors(member)) == []
    assert member["roles"] == ["admin"]
