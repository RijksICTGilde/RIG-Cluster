"""Load preset definitions from YAML files."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_PRESETS_DIR = Path(__file__).resolve().parent.parent.parent / "configs" / "presets"


@dataclass
class Preset:
    """A single preset/scenario that can pre-fill form values."""

    id: str
    label: str
    description: str
    icon: str = "document"
    color: str = "hemelblauw"
    values: dict[str, Any] = field(default_factory=dict)


def load_presets(section_key: str) -> list[Preset]:
    """Load presets for a given section key.

    Reads from ``opi/configs/presets/{section_key}.yaml``.
    Returns an empty list if the file doesn't exist.
    """
    preset_file = _PRESETS_DIR / f"{section_key}.yaml"
    if not preset_file.exists():
        return []

    try:
        raw = yaml.safe_load(preset_file.read_text())
    except yaml.YAMLError:
        logger.warning("Failed to parse preset file: %s", preset_file)
        return []

    if not isinstance(raw, dict) or "presets" not in raw:
        return []

    presets: list[Preset] = []
    for entry in raw["presets"]:
        if not isinstance(entry, dict) or "id" not in entry:
            continue
        presets.append(
            Preset(
                id=entry["id"],
                label=entry.get("label", entry["id"]),
                description=entry.get("description", ""),
                icon=entry.get("icon", "document"),
                color=entry.get("color", "hemelblauw"),
                values=entry.get("values", {}),
            )
        )
    return presets


def get_preset_by_id(section_key: str, preset_id: str) -> Preset | None:
    """Find a specific preset by section key and preset ID."""
    for preset in load_presets(section_key):
        if preset.id == preset_id:
            return preset
    return None
