"""Hoe OPI zich bij ArgoCD meldt, gemeten op de gerenderde overlays (RC-345).

Het token is de weg: `role:zad-opi` mag precies wat OPI aanroept, het admin-account mag
alles. Op odcn is het token daarom de enige weg, en zonder token weigert
``create_argo_connector`` hard. Op de sandbox valt OPI terug op het admin-account zolang
``opi-argocd-token`` er niet is, zodat een sandbox die van voor deze wijziging dateert na een
``sandbox-deploy`` blijft syncen: alleen ``task sandbox:issue-argocd-opi-token`` geeft het
token uit, en dat draait niet bij een gewone uitrol.

Daarom rendert deze toets de overlays in plaats van de patchbestanden te lezen: het
wachtwoord terugzetten in ``base/`` of weghalen uit de sandbox verandert allebei alleen de
render, en moet allebei rood worden.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_OPI_KUSTOMIZE = _REPO_ROOT / "bootstrap/rig-system/kustomize/operations-manager"

#: Het geheim waaruit de ArgoCD-operator admin.password naar argocd-secret hasht: dezelfde
#: bron als waar `sandbox-gegevens` het admin-wachtwoord leest.
SANDBOX_WACHTWOORD = {"secretKeyRef": {"name": "argocd-cluster", "key": "admin.password"}}

#: optional, zodat een cluster zonder uitgegeven token blijft booten.
TOKEN = {"secretKeyRef": {"name": "opi-argocd-token", "key": "token", "optional": True}}


def _overlays(tmp_path: Path) -> Path:
    """Kopie van de kustomize-boom zonder de SOPS-generators, die een sleutel vragen."""
    if shutil.which("kustomize") is None:
        pytest.skip("kustomize ontbreekt")
    copy = tmp_path / "operations-manager"
    shutil.copytree(_OPI_KUSTOMIZE, copy)
    for kustomization in (copy / "overlays").glob("*/kustomization.yaml"):
        document = yaml.safe_load(kustomization.read_text())
        if document.pop("generators", None):
            kustomization.write_text(yaml.safe_dump(document))
    return copy / "overlays"


def _argocd_env(overlay: Path) -> dict[str, object]:
    """De ARGOCD_*-env van de OPI-container, op naam."""
    rendered = subprocess.run(
        ["kustomize", "build", str(overlay), "--load-restrictor", "LoadRestrictionsNone"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    deployment = next(
        document
        for document in yaml.safe_load_all(rendered)
        if document and document["kind"] == "Deployment" and document["metadata"]["name"] == "operations-manager"
    )
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    return {
        entry["name"]: entry.get("value", entry.get("valueFrom"))
        for entry in container["env"]
        if entry["name"].startswith("ARGOCD_")
    }


@pytest.fixture(scope="module")
def env_per_overlay(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict[str, object]]:
    overlays = _overlays(tmp_path_factory.mktemp("opi-overlays"))
    return {overlay.name: _argocd_env(overlay) for overlay in sorted(overlays.iterdir()) if overlay.is_dir()}


def test_elke_overlay_is_gerenderd(env_per_overlay: dict[str, dict[str, object]]) -> None:
    """Zonder deze toets melden de checks hieronder succes over een lege lijst, en valt een
    overlay die later wordt toegevoegd stil buiten de grendel."""
    assert set(env_per_overlay) == {
        "odcn-production",
        "odcn-staging",
        "sandboxed-local",
        "sandboxed-local-debug",
        "sandboxed-local-dev",
    }, f"gerenderd: {sorted(env_per_overlay)}"


def test_de_sandbox_valt_terug_op_het_admin_account(env_per_overlay: dict[str, dict[str, object]]) -> None:
    """Een bewuste keuze: de sandbox houdt zijn admin-wachtwoorden om eenvoudig te blijven.
    Is het token er wel, dan wint het token, want create_argo_connector kijkt daar eerst."""
    for naam, env in env_per_overlay.items():
        if not naam.startswith("sandboxed-local"):
            continue
        assert env.get("ARGOCD_USERNAME") == "admin", f"{naam} zet ARGOCD_USERNAME={env.get('ARGOCD_USERNAME')!r}"
        assert env.get("ARGOCD_PASSWORD") == SANDBOX_WACHTWOORD, f"{naam} leest het wachtwoord niet uit argocd-cluster"
        assert env.get("ARGOCD_TOKEN") == TOKEN, f"{naam} draagt het token niet optioneel"


def test_odcn_heeft_alleen_het_token(env_per_overlay: dict[str, dict[str, object]]) -> None:
    """Geen wachtwoord in de deployment betekent: zonder token geen ArgoCD, in plaats van
    met het beheeraccount binnenkomen. Het wachtwoord in base terugzetten maakt dit rood."""
    for naam, env in env_per_overlay.items():
        if not naam.startswith("odcn-"):
            continue
        assert "ARGOCD_PASSWORD" not in env, f"{naam} draagt ARGOCD_PASSWORD"
        assert "ARGOCD_USERNAME" not in env, f"{naam} draagt ARGOCD_USERNAME"
        assert env.get("ARGOCD_TOKEN") == TOKEN, f"{naam} draagt het token niet optioneel"
