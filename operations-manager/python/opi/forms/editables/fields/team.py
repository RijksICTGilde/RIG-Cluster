"""Team section editables: the member list, with an email address and roles per member."""

from __future__ import annotations

from opi.core.authz.engine import Role
from opi.forms.editables.editable import Editable
from opi.forms.editables.validators import EmailValidator, RequiredValidator

USER_EMAIL_EDITABLE = Editable(
    yaml_path="users[*]/email",
    validator=EmailValidator(),
    required=True,
)

#: The roles of a member: a list since schema version 2.13, and a visible multi-select. It
#: must stay visible and pre-filled with ``developer``: as a hidden field defaulting to
#: ``admin`` every new member became an administrator, and a manipulated POST could set any
#: role.
USER_ROLES_EDITABLE = Editable(
    yaml_path="users[*]/roles",
    required=True,
    default=[Role.DEVELOPER.value],
    values_provider="UserRoleOptionsProvider",
    # Empty is an error here, not an omission: the schema demands at least one role, and a
    # member without roles opens nothing. The validator reports it at the field, so the form
    # says it instead of the storage layer.
    validator=RequiredValidator(),
)

USERS_SEQUENCE_EDITABLE = Editable(
    yaml_path="users",
    min_items=1,
    children=[USER_EMAIL_EDITABLE, USER_ROLES_EDITABLE],
)
