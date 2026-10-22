"""Toetsen voor de non-interactieve vlaggen van scripts/edit-secret.py (RC-345).

`--yes` bestaat zodat `task sandbox:setup` een vers token kan uitgeven zonder iemand die op
een prompt wacht. Dat mag nooit op productie en nooit voor een hele ronde: beide guards
zitten voor in `rotate_flow`, vóór er iets wordt aangeraakt. `--cluster` kiest het cluster
bij naam in plaats van met de interactieve vraag; een onbekende naam moet een duidelijke
fout geven in plaats van een crash op `available[...]`.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from secret_edit import Cluster, EditFailed  # type: ignore[reportMissingImports]  # noqa: E402


def _edit_secret_module() -> ModuleType:
    """De hyphenated CLI (``edit-secret.py``), geladen bij pad: een gewone import kan niet
    met een koppelteken in de bestandsnaam. Hetzelfde patroon als `_scan_secrets_module`
    in test_secret_scan.py."""
    existing = sys.modules.get("edit_secret_cli")
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location("edit_secret_cli", _SCRIPTS_DIR / "edit-secret.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["edit_secret_cli"] = module
    spec.loader.exec_module(module)
    return module


def _args(**overrides: object) -> argparse.Namespace:
    defaults: dict[str, object] = {
        "dry_run": False,
        "check": False,
        "component": "redis",
        "rotate_all": False,
        "apply": False,
        "context": None,
        "cluster": None,
        "yes": False,
        "seed_overlay": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _cluster(name: str) -> Cluster:
    return Cluster(name=name, namespace="rig-system" if "sandbox" in name else "rig-prd-operations", folders={})


def test_yes_weigert_op_een_productiecluster() -> None:
    edit_secret = _edit_secret_module()
    with pytest.raises(EditFailed, match="productie"):
        edit_secret.rotate_flow(_args(yes=True, component="redis"), _cluster("odcn-production"))


def test_yes_vraagt_altijd_component_of_all(monkeypatch: pytest.MonkeyPatch) -> None:
    edit_secret = _edit_secret_module()
    monkeypatch.setattr(
        "builtins.input", lambda *a: (_ for _ in ()).throw(AssertionError("--yes zonder component mag niet vragen"))
    )
    with pytest.raises(EditFailed, match="--component"):
        edit_secret.rotate_flow(_args(yes=True, component=None, rotate_all=True), _cluster("sandboxed-local"))


def test_yes_met_component_op_een_niet_productiecluster_komt_voorbij_de_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Geen EditFailed van de --yes-guard zelf; wat er daarna gebeurt (echte kubectl-calls)
    toetsen de rotatie-handlers al, dus deze test stopt bij de guard en vangt de volgende
    stap (een kubectl-aanroep buiten een cluster) als bewijs dat de guard niet blokkeerde."""
    edit_secret = _edit_secret_module()
    monkeypatch.setattr(
        edit_secret.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("kubectl-aanroep"))
    )
    with pytest.raises(RuntimeError, match="kubectl-aanroep"):
        edit_secret.rotate_flow(_args(yes=True, component="redis", check=True), _cluster("sandboxed-local"))


def test_onbekend_component_geeft_een_duidelijke_fout_in_plaats_van_een_keyerror() -> None:
    edit_secret = _edit_secret_module()
    with pytest.raises(EditFailed, match="onbekend component"):
        edit_secret.rotate_flow(_args(component="dit-bestaat-niet"), _cluster("sandboxed-local"))


def test_main_met_een_onbekende_clusternaam_geeft_een_duidelijke_fout(monkeypatch: pytest.MonkeyPatch) -> None:
    edit_secret = _edit_secret_module()
    monkeypatch.setattr(edit_secret, "clusters", lambda: [_cluster("sandboxed-local"), _cluster("odcn-production")])
    with pytest.raises(EditFailed, match="onbekend cluster"):
        edit_secret.main(["--cluster", "dit-cluster-bestaat-niet", "--component", "redis", "--check"])


def test_main_met_een_bekende_clusternaam_vraagt_niet_interactief(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--cluster` moet de interactieve clusterkeuze overslaan: zonder deze toets kan een
    taak die `--cluster` meegeeft alsnog op een prompt blijven staan."""
    edit_secret = _edit_secret_module()
    monkeypatch.setattr(edit_secret, "clusters", lambda: [_cluster("sandboxed-local"), _cluster("odcn-production")])
    monkeypatch.setattr(
        "builtins.input", lambda *a: (_ for _ in ()).throw(AssertionError("--cluster mag niet naar de vraag lopen"))
    )
    aangeroepen: list[object] = []
    monkeypatch.setattr(edit_secret, "rotate_flow", lambda args, cluster: aangeroepen.append(cluster.name) or 0)

    result = edit_secret.main(["--cluster", "sandboxed-local", "--component", "redis", "--check"])

    assert result == 0
    assert aangeroepen == ["sandboxed-local"]
