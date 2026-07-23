"""Flow/section registry snapshot guardrail (RC-5 Phase 3).

Phase 3 moves the per-service config sections + modal flows onto the service
providers and derives SERVICE_CONFIG_SECTIONS / SERVICE_CONFIG_MODAL_FLOWS (and their
parts of EDIT_SECTIONS / FLOW_REGISTRY) by iterating the registry instead of the
hand-synced dicts. This snapshot captures the *current* structure of those four
registries so the generic derivation can be proven structure-identical before/after.

It digests structure (section ids, titles, editable yaml-paths, post-save actions,
flow section order), not object identity, so it is stable across the refactor while
still catching a dropped/re-ordered/renamed section or flow.

Regenerate intentional changes with::

    UPDATE_GOLDEN=1 uv run pytest tests/test_flow_registry_snapshot.py -q
"""

import json
import os
from pathlib import Path
from typing import Any

from opi.forms.visualizers.flows import FLOW_REGISTRY, SERVICE_CONFIG_MODAL_FLOWS, FormFlow
from opi.forms.visualizers.sections import FormSection
from opi.forms.visualizers.wizard_sections import EDIT_SECTIONS, SERVICE_CONFIG_SECTIONS

GOLDEN = Path(__file__).parent / "golden" / "flow_registry.json"


def _editable_id(ev: Any) -> str:
    """Stable identifier for an editable visualizer (its data path, else its label)."""
    editable = getattr(ev, "editable", None)
    path = getattr(editable, "yaml_path", None)
    return path or getattr(ev, "label", None) or repr(ev)[:40]


def _section_digest(section: FormSection) -> dict[str, Any]:
    return {
        "section_id": section.section_id,
        "title": section.title,
        "post_save_action": section.post_save_action,
        "editables": [_editable_id(ev) for ev in section.editables],
    }


def _flow_digest(flow: FormFlow) -> dict[str, Any]:
    return {
        "flow_id": flow.flow_id,
        "mode": flow.mode.value,
        "sections": [s.section_id for s in flow.sections],
    }


def _snapshot() -> dict[str, Any]:
    return {
        "service_config_sections": {name: _section_digest(sec) for name, sec in SERVICE_CONFIG_SECTIONS.items()},
        "edit_sections": {key: _section_digest(sec) for key, sec in EDIT_SECTIONS.items()},
        "service_config_modal_flows": dict(SERVICE_CONFIG_MODAL_FLOWS),
        "flow_registry": {fid: _flow_digest(flow) for fid, flow in FLOW_REGISTRY.items()},
    }


def test_flow_registry_snapshot() -> None:
    snapshot = _snapshot()
    rendered = json.dumps(snapshot, indent=2, sort_keys=True) + "\n"

    if os.environ.get("UPDATE_GOLDEN") == "1":
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(rendered, encoding="utf-8")
        import pytest

        pytest.skip("UPDATE_GOLDEN: wrote flow_registry.json")

    assert GOLDEN.exists(), (
        f"Missing {GOLDEN}. Generate with `UPDATE_GOLDEN=1 uv run pytest tests/test_flow_registry_snapshot.py`."
    )
    assert rendered == GOLDEN.read_text(encoding="utf-8"), (
        "Flow/section registry structure drifted from the snapshot. If intentional (e.g. the Phase 3 "
        "generic derivation), regenerate with UPDATE_GOLDEN=1 and review the diff -- it must be empty for a "
        "behaviour-preserving refactor."
    )
