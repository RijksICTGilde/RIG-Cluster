"""Generated editables - computed at submit time, not rendered in forms.

These editables use generators to produce values from the merged YAML
data during final submission. Order matters: the list below defines
the execution order, and later generators may depend on values
produced by earlier ones.
"""

from __future__ import annotations

from opi.forms.editables.editable import Editable
from opi.forms.editables.generators import (
    AttachmentStagingResolveGenerator,
    ProjectNameGenerator,
)

# --- Pure Editable definitions (data logic only) ---

PROJECT_NAME_EDITABLE = Editable(
    yaml_path="name",
    generator=ProjectNameGenerator(),
)

ATTACHMENTS_RESOLVE_GEN_EDITABLE = Editable(
    yaml_path="_generated/attachments-resolved",
    generator=AttachmentStagingResolveGenerator(),
)

GENERATED_EDITABLES_PURE: list[Editable] = [
    PROJECT_NAME_EDITABLE,
    ATTACHMENTS_RESOLVE_GEN_EDITABLE,
]
