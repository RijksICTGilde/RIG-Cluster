"""Project template loader.

Loads ``configs/project-template.yaml`` and resolves ``${SETTING}``
placeholders from :mod:`opi.core.config.settings`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from opi.core.config import settings
from opi.services.schema_migration import LATEST_SCHEMA_VERSION

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

    The template is stamped with ``LATEST_SCHEMA_VERSION`` so newly created
    projects are already at the current schema and need no migration. This matters
    because ``process_project`` commits an auto-migration of any file it loads that
    was migrated on read; on create that write-back races the just-pushed project
    file and fails the provisioning task with a ``ConflictError``. Emitting the
    latest version on create avoids the migration entirely.
    """
    yaml = YAML()
    with _TEMPLATE_PATH.open() as f:
        data = yaml.load(f)
    resolved = _resolve_placeholders(data or {})
    resolved["schema-version"] = LATEST_SCHEMA_VERSION
    return resolved
