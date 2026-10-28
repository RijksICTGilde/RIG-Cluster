"""Team section editables: de ledenlijst met per lid een e-mailadres en zijn rollen."""

from __future__ import annotations

from opi.core.authz.engine import Role
from opi.forms.editables.editable import Editable
from opi.forms.editables.validators import EmailValidator, RequiredValidator

# --- Pure Editable definitions (data logic only) ---

USER_EMAIL_EDITABLE = Editable(
    yaml_path="users[*]/email",
    validator=EmailValidator(),
    required=True,
)

#: De rollen van een lid. Een lijst sinds schemaversie 2.13 (B1), en een ZICHTBARE
#: meerkeuze: tot RC-392 was dit een verborgen veld met standaard ``admin``, waardoor elk
#: nieuw lid beheerder werd en een gemanipuleerde POST elke rol kon zetten. De
#: voorinvulling is nu ``developer`` (B12), en de admin die het lid toevoegt kiest.
USER_ROLES_EDITABLE = Editable(
    yaml_path="users[*]/roles",
    required=True,
    default=[Role.DEVELOPER.value],
    values_provider="UserRoleOptionsProvider",
    # Leeg is hier een FOUT en geen weglating: het schema eist minstens een rol, en een
    # lid zonder rollen opent niets. De validator geeft dat als veldmelding, zodat het
    # formulier het zegt in plaats van de opslag.
    validator=RequiredValidator(),
)

USERS_SEQUENCE_EDITABLE = Editable(
    yaml_path="users",
    min_items=1,
    children=[USER_EMAIL_EDITABLE, USER_ROLES_EDITABLE],
)
