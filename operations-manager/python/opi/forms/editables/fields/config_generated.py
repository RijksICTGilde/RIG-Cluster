"""Generated editables - computed at submit time, not rendered in forms.

These editables use generators to produce values from the merged YAML
data during final submission. Order matters: the list below defines
the execution order, and later generators may depend on values
produced by earlier ones.
"""

from __future__ import annotations

from opi.forms.editables.editable import Editable
from opi.forms.editables.generators import (
    AGEKeyPairGenerator,
    EncryptedAPIKeyGenerator,
    EncryptedPrivateKeyGenerator,
    ProjectNameGenerator,
    UserEnvVarsEncryptGenerator,
)

# --- Pure Editable definitions (data logic only) ---

PROJECT_NAME_EDITABLE = Editable(
    yaml_path="name",
    generator=ProjectNameGenerator(),
)

AGE_PUBLIC_KEY_GEN_EDITABLE = Editable(
    yaml_path="config/age-public-key",
    generator=AGEKeyPairGenerator(),
)

AGE_PRIVATE_KEY_GEN_EDITABLE = Editable(
    yaml_path="config/age-private-key",
    generator=EncryptedPrivateKeyGenerator(),
)

API_KEY_GEN_EDITABLE = Editable(
    yaml_path="config/api-key",
    generator=EncryptedAPIKeyGenerator(),
)

USER_ENV_VARS_ENCRYPT_GEN_EDITABLE = Editable(
    yaml_path="_generated/user-env-vars-encrypted",
    generator=UserEnvVarsEncryptGenerator(),
)

GENERATED_EDITABLES_PURE: list[Editable] = [
    PROJECT_NAME_EDITABLE,
    AGE_PUBLIC_KEY_GEN_EDITABLE,
    AGE_PRIVATE_KEY_GEN_EDITABLE,
    API_KEY_GEN_EDITABLE,
    USER_ENV_VARS_ENCRYPT_GEN_EDITABLE,
]
