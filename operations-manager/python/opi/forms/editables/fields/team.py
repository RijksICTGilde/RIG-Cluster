"""Team section editables: users sequence with email and role."""

from __future__ import annotations

from opi.forms.editables.editable import ProjectEditable
from opi.forms.editables.validators import EmailValidator

USER_EMAIL = ProjectEditable(
    yaml_path="users[*]/email",
    widget="text",
    label="E-mailadres",
    required=True,
    validator=EmailValidator(),
)

USER_ROLE = ProjectEditable(
    yaml_path="users[*]/role",
    widget="hidden",
    label="Rol",
    required=True,
    default="administrator",
    # TODO: Make visible again when roles are implemented.
    # widget="select",
    # options_provider="UserRoleOptionsProvider",
)

USERS_SEQUENCE = ProjectEditable(
    yaml_path="users",
    widget="sequence",
    label="Projectleden",
    min_items=1,
    children=[USER_EMAIL, USER_ROLE],
)
