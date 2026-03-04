"""Generated editables — computed at submit time, not rendered in forms.

These editables use generators to produce values from the merged YAML
data during final submission. Order matters: the list below defines
the execution order, and later generators may depend on values
produced by earlier ones.
"""

from __future__ import annotations

from opi.forms.editables.editable import ProjectEditable
from opi.forms.editables.generators import (
    AGEKeyPairGenerator,
    EncryptedAPIKeyGenerator,
    EncryptedPrivateKeyGenerator,
    ProjectNameGenerator,
)

# Order matters — each generator may depend on values from earlier ones.
GENERATED_EDITABLES: list[ProjectEditable] = [
    ProjectEditable(
        yaml_path="name",
        widget="hidden",
        label="Projectnaam (technisch)",
        generator=ProjectNameGenerator(),
    ),
    ProjectEditable(
        yaml_path="config/age-public-key",
        widget="hidden",
        label="AGE publieke sleutel",
        generator=AGEKeyPairGenerator(),
    ),
    ProjectEditable(
        yaml_path="config/age-private-key",
        widget="hidden",
        label="AGE prive-sleutel (versleuteld)",
        generator=EncryptedPrivateKeyGenerator(),
    ),
    ProjectEditable(
        yaml_path="config/api-key",
        widget="hidden",
        label="API-sleutel (versleuteld)",
        generator=EncryptedAPIKeyGenerator(),
    ),
]
