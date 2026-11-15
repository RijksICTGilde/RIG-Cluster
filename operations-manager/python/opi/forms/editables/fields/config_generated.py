"""Generated editables: computed at submit time, not rendered in forms.

Order matters. The list below is the execution order, and a later generator may depend on
what an earlier one produced.
"""

from __future__ import annotations

from opi.forms.editables.editable import Editable
from opi.forms.editables.generators import (
    AttachmentStagingResolveGenerator,
    ProjectNameGenerator,
)

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
