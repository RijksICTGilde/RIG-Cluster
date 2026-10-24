"""Config display section editables: read-only AGE keys."""

from __future__ import annotations

from opi.forms.editables.converters import EncryptedDisplayConverter, TruncateConverter
from opi.forms.editables.editable import Editable

# --- Pure Editable definitions (data logic only) ---

AGE_PUBLIC_KEY_EDITABLE = Editable(
    yaml_path="config/age-public-key",
    converter=TruncateConverter(20),
)

AGE_PRIVATE_KEY_EDITABLE = Editable(
    yaml_path="config/age-private-key",
    converter=EncryptedDisplayConverter(),
)
