"""De CMP-plugin uit de bootstrap, zoals de tests hem lezen.

Een ConfigMap met twee plugins en een script: ``plugin.yaml`` voor tenant-apps,
``plugin-platform.yaml`` voor platform-apps, en ``render.sh`` dat ze allebei draaien.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

CONFIGMAP = Path(__file__).parent.parent.parent.parent / "bootstrap/rig-system/kustomize/configmap-sops-plugin.yaml"


def configmap_data() -> dict[str, str]:
    return YAML(typ="safe").load(CONFIGMAP.read_text())["data"]


def render_script() -> str:
    """Het bash-script dat beide plugins draaien."""
    return configmap_data()["render.sh"]


def plugin(key: str = "plugin.yaml") -> dict[str, Any]:
    """Een ConfigManagementPlugin uit de ConfigMap."""
    return YAML(typ="safe").load(configmap_data()[key])
