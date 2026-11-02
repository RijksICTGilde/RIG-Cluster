"""De twee schotten uit het aanvalsproef-plan: Kyverno in waarschuwmodus en Falco.

Geen van beide draait hier: dat vraagt een cluster, en dat meet de job in
``.github/workflows/aanvalsproef.yml``. Wat wel zonder cluster te toetsen is, is de
belofte uit het plan dat deze stap zelf niet mag breken: elke Kyverno-policy staat in
``Audit``, nooit in ``Enforce`` (afdwingen hangt aan het open image-signing-besluit, zie
features/aanvalsproef-harnas.md), en de kustomizations verwijzen alleen naar bestanden
die er ook echt staan.
"""

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
KYVERNO_BASE = REPO_ROOT / "infrastructure/bootstrap/infrastructure/kyverno/policies/base"
FALCO_BASE = REPO_ROOT / "infrastructure/bootstrap/infrastructure/falco/controller/base"


def _kustomizations() -> list[Path]:
    return [KYVERNO_BASE / "kustomization.yaml", FALCO_BASE / "kustomization.yaml"]


@pytest.mark.parametrize("pad", _kustomizations())
def test_kustomization_resources_bestaan_allemaal(pad: Path) -> None:
    inhoud = yaml.safe_load(pad.read_text(encoding="utf-8"))
    for naam in inhoud["resources"]:
        assert (pad.parent / naam).is_file(), f"{pad}: resource {naam} bestaat niet"


def _kyverno_policies() -> list[Path]:
    paden = sorted(KYVERNO_BASE.glob("*.yaml"))
    policies = [pad for pad in paden if pad.name != "kustomization.yaml"]
    assert policies, "geen enkele kyverno-policy gevonden"
    return policies


@pytest.mark.parametrize("pad", _kyverno_policies())
def test_elke_kyverno_regel_staat_op_audit_niet_op_enforce(pad: Path) -> None:
    """Afdwingen hangt aan het open image-signing-/admission-besluit; deze toets bewaakt
    dat niemand dat besluit stilletjes vooruitschuift door hier Enforce te zetten."""
    policy = yaml.safe_load(pad.read_text(encoding="utf-8"))
    gevonden = []
    for regel in policy["spec"]["rules"]:
        if "validate" in regel:
            gevonden.append((regel["name"], regel["validate"].get("failureAction")))
        gevonden.extend((regel["name"], entry.get("failureAction")) for entry in regel.get("verifyImages", []))
    assert gevonden, f"{pad.name}: geen validate- of verifyImages-regel gevonden om te toetsen"
    for naam, actie in gevonden:
        assert actie == "Audit", f"{pad.name}:{naam} staat op {actie!r}, verwacht Audit"


def test_falco_image_heeft_een_eigen_tag() -> None:
    """Dezelfde eis als de kyverno-policy geen-latest-tag.yaml, nu op de eigen uitrol."""
    daemonset = yaml.safe_load((FALCO_BASE / "daemonset.yaml").read_text(encoding="utf-8"))
    container = daemonset["spec"]["template"]["spec"]["containers"][0]
    melding = f"falco-image {container['image']!r} heeft geen eigen tag of staat op :latest"
    assert ":" in container["image"], melding
    assert not container["image"].endswith(":latest"), melding


def test_falco_regels_is_geldige_yaml_met_minstens_een_regel() -> None:
    configmap = yaml.safe_load((FALCO_BASE / "regels.yaml").read_text(encoding="utf-8"))
    regels = yaml.safe_load(configmap["data"]["zad_regels.yaml"])
    namen = [regel["rule"] for regel in regels if "rule" in regel]
    assert namen, "regels.yaml draagt geen enkele rule"
