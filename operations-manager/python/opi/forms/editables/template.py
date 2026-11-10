"""Project template loader.

Loads ``configs/project-template.yaml`` and resolves ``${SETTING}``
placeholders from :mod:`opi.core.config.settings`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from opi.core.config import settings
from opi.services.project_utils import encrypted_repository_password
from opi.services.schema_migration import LATEST_SCHEMA_VERSION_VALUE
from opi.utils.age import generate_age_key_pair, wrap_project_key_sync
from opi.utils.yaml_util import load_yaml_safe

_TEMPLATE_PATH = Path(__file__).resolve().parents[2] / "configs" / "project-template.yaml"
_PLACEHOLDER_RE = re.compile(r"\$\{(\w+)\}")


def _resolve_placeholders(value: Any) -> Any:
    """Recursively resolve ``${SETTING}`` placeholders in strings."""
    if isinstance(value, str):
        return _PLACEHOLDER_RE.sub(lambda m: str(getattr(settings, m.group(1), m.group(0))), value)
    if isinstance(value, dict):
        return {k: _resolve_placeholders(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_placeholders(item) for item in value]
    return value


def load_project_template() -> dict[str, Any]:
    """Load and return the project template with settings resolved.

    The template is stamped with the latest schema version so newly created
    projects are already at the current schema and need no migration. This matters
    because ``process_project`` commits an auto-migration of any file it loads that
    was migrated on read; on create that write-back races the just-pushed project
    file and fails the provisioning task with a ``ConflictError``. Emitting the
    latest version on create avoids the migration entirely.
    """
    data = load_yaml_safe(_TEMPLATE_PATH.read_text(encoding="utf-8"))
    resolved = _resolve_placeholders(data or {})
    resolved["schema-version"] = LATEST_SCHEMA_VERSION_VALUE
    return resolved


def build_new_project_base_data() -> dict[str, Any]:
    """The template plus what a new project must own before its first step is filled in.

    Its own AGE key pair, because the wizard keeps its state in a file on disk: without a
    public key from the start the converters have nothing to encrypt the first secret the
    user types with, and it would sit in that file as plain text until submit.

    The repository password likewise, through the same function the creation API uses: the
    setting may be ``plain:``, a project file may not.
    """
    base_data = load_project_template()
    private_key, public_key = generate_age_key_pair()
    base_data["config"] = {
        "age-public-key": public_key,
        "age-private-key": wrap_project_key_sync(private_key),
    }
    for repository in base_data.get("repositories") or []:
        repository["password"] = encrypted_repository_password()
    return base_data
