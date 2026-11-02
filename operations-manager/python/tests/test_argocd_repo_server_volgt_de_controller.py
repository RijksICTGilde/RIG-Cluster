"""De repo-server moet in elke overlay dezelfde ArgoCD-versie draaien als de controller.

De sandbox pinde repo-server op v3.3.10 (met een uitleg dat productie ook anders liep), maar
productie draaide repo-server al op 3.5.1 - dus sandbox mat iets dat productie niet deed.
Deze guard faalt zodra een overlay de repo-server een andere image of versie geeft dan
controller/server; een bewuste afwijking hoort hier gedocumenteerd.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_OVERLAYS = _REPO_ROOT / "bootstrap/rig-system/kustomize/overlays"

#: Het component dat `/spec/repo` wholesale vervangt, en dus image en version uit de CR
#: weggooit. Zonder deze stap meet de guard een veld dat nooit in het cluster aankomt.
_REPO_REPLACING_COMPONENT = "sops-plugin"


def _argocd_cr(overlay: Path) -> dict:
    """De ArgoCD-CR van een overlay, gevonden op ``kind`` en niet op bestandsnaam."""
    for path in sorted(overlay.glob("*.yaml")):
        for document in yaml.safe_load_all(path.read_text()):
            if document and document.get("kind") == "ArgoCD":
                return document.get("spec", {}) or {}
    raise AssertionError(f"{overlay} heeft geen ArgoCD-CR")


def _repo_field_from_patch(patch_body: object, field: str) -> str | None:
    """De waarde die een patch op ``spec.repo.<field>`` zet, in beide kustomize-vormen."""
    if isinstance(patch_body, list):
        found: str | None = None
        for operation in patch_body:
            if isinstance(operation, dict) and operation.get("path") == f"/spec/repo/{field}":
                found = operation.get("value")
        return found
    if isinstance(patch_body, dict):
        return (((patch_body.get("spec") or {}).get("repo")) or {}).get(field)
    return None


def _repo_field_from_patches(overlay: Path, field: str) -> str | None:
    """De laatste waarde die een patch in de overlay op ``spec.repo.<field>`` zet.

    Een patch uit een los bestand (``path:``) leest deze guard niet, en stil overslaan zou
    hem blind maken voor precies de afwijking die hij moet tegenhouden.
    """
    kustomization = yaml.safe_load((overlay / "kustomization.yaml").read_text())
    found: str | None = None
    for patch in kustomization.get("patches", []) or []:
        if (patch.get("target") or {}).get("kind") != "ArgoCD":
            continue
        assert "patch" in patch, f"{overlay.name} patcht de ArgoCD-CR uit een bestand; breid deze guard uit"
        value = _repo_field_from_patch(yaml.safe_load(patch["patch"]), field)
        if value is not None:
            found = value
    return found


def _effective(overlay: Path, spec: dict, field: str) -> str | None:
    """Wat de repo-server na het renderen voor ``image`` of ``version`` krijgt."""
    kustomization = yaml.safe_load((overlay / "kustomization.yaml").read_text())
    components = [Path(entry).name for entry in kustomization.get("components", []) or []]
    from_cr = None if _REPO_REPLACING_COMPONENT in components else (spec.get("repo") or {}).get(field)
    return _repo_field_from_patches(overlay, field) or from_cr or spec.get(field)


_OVERLAY_DIRS = sorted(path for path in _OVERLAYS.iterdir() if (path / "kustomization.yaml").exists())
_IDS = [path.name for path in _OVERLAY_DIRS]


def test_de_walk_vindt_overlays() -> None:
    """Een lege parametrize slaat over in plaats van te falen, dus zonder deze toets melden de
    checks hieronder succes over niets. Geen vaste namen: een nieuwe overlay hoort mee te gaan."""
    assert _IDS, f"geen overlays met een kustomization.yaml onder {_OVERLAYS}"


@pytest.mark.parametrize("overlay", _OVERLAY_DIRS, ids=_IDS)
@pytest.mark.parametrize("field", ["image", "version"])
def test_de_repo_server_draait_dezelfde_image_als_de_rest(overlay: Path, field: str) -> None:
    spec = _argocd_cr(overlay)
    assert spec.get(field), f"{overlay.name} zet spec.{field} niet"
    assert _effective(overlay, spec, field) == spec[field], (
        f"{overlay.name} geeft de repo-server {field} "
        f"{_effective(overlay, spec, field)!r} terwijl de controller {spec[field]!r} draait; "
        f"een meting op die overlay zegt dan niets over de andere"
    )
