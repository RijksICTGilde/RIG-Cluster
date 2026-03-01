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
    """Load and return the project template with settings resolved."""
    yaml = YAML()
    with _TEMPLATE_PATH.open() as f:
        data = yaml.load(f)
    return _resolve_placeholders(data or {})
