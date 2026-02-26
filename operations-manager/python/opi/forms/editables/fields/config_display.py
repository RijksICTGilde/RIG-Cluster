"""Config display section editables: read-only AGE keys and API key."""

from __future__ import annotations

from opi.forms.editables.converters import EncryptedDisplayConverter, TruncateConverter
from opi.forms.editables.editable import ProjectEditable

AGE_PUBLIC_KEY = ProjectEditable(
    yaml_path="config/age-public-key",
    widget="display_card",
    label="AGE publieke sleutel",
    readonly=True,
    converter=TruncateConverter(20),
)

AGE_PRIVATE_KEY = ProjectEditable(
    yaml_path="config/age-private-key",
    widget="display_card",
    label="AGE privé sleutel",
    readonly=True,
    converter=EncryptedDisplayConverter(),
)

API_KEY = ProjectEditable(
    yaml_path="config/api-key",
    widget="display_card",
    label="API sleutel",
    readonly=True,
    converter=EncryptedDisplayConverter(),
)
