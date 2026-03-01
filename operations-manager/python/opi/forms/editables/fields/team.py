"""Team section editables: users sequence with email and role."""

from __future__ import annotations

from opi.forms.editables.editable import Editable
from opi.forms.editables.validators import EmailValidator

# --- Pure Editable definitions (data logic only) ---

USER_EMAIL_EDITABLE = Editable(
    yaml_path="users[*]/email",
    validator=EmailValidator(),
    required=True,
)

USER_ROLE_EDITABLE = Editable(
    yaml_path="users[*]/role",
    required=True,
    default="administrator",
)

USERS_SEQUENCE_EDITABLE = Editable(
    yaml_path="users",
    min_items=1,
    children=[USER_EMAIL_EDITABLE, USER_ROLE_EDITABLE],
)
