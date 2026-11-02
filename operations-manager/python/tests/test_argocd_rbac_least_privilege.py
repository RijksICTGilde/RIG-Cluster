"""ArgoCD-rollen en de Kubernetes-Role op de CR's blijven op least privilege (RC-345).

Twee paden geven toegang tot een Application, en een rol in ArgoCD beperkt alleen het
eerste:

* de ArgoCD-API en -UI, begrensd door ``spec.rbac.policy`` in de ArgoCD-CR;
* de Kubernetes-API op ``applications.argoproj.io``, begrensd door RBAC. Wie daar
  schrijfrecht heeft gaat om de ArgoCD-rollen heen.

Daarom toetst dit bestand ze samen. De inventaris van wat OPI werkelijk aanroept staat als
commentaar boven de policy in beide CR's en boven de Role; deze toetsen zijn de grendel die
een ruimere regel rood maakt in plaats van onopgemerkt te laten.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]
_OVERLAYS = _REPO_ROOT / "bootstrap/rig-system/kustomize/overlays"
_OPI_KUSTOMIZE = _REPO_ROOT / "bootstrap/rig-system/kustomize/operations-manager"

#: Rollen die alles mogen. Elke andere binding op role:admin is een gemis.
ADMIN_SUBJECTS = {"system:cluster-admins", "rig-prd-admin-group", "rig-staging-admin-group"}

#: Resources waarop een eigen rol van dit platform nooit mag schrijven: aanmaken,
#: wijzigen en verwijderen loopt via git en OPI, of via de admingroep.
GUARDED_RESOURCES = {"applications", "projects", "repositories", "clusters", "accounts"}
WRITE_ACTIONS = {"create", "update", "delete"}

#: De verbs uit de inventaris van `opi/connectors/kubectl.py` en
#: `opi/manager/delete_project_manager.py`. `update` staat er niet bij: een merge- of
#: json-patch vraagt alleen patch.
ARGO_CR_VERBS = {
    "applications": {"get", "list", "patch", "delete"},
    "appprojects": {"get", "list", "delete"},
}


def _documents(path: Path) -> list[dict]:
    return [document for document in yaml.safe_load_all(path.read_text()) if document]


def _argocd_crs() -> list[tuple[str, dict]]:
    """Elke ArgoCD-CR onder de rig-system-overlays, gevonden op ``kind``.

    Dezelfde walk als in ``test_argocd_extra_config_keys.py``: een overlay die later wordt
    toegevoegd of hernoemd blijft gedekt in plaats van stil uit de parametrize te vallen.
    """
    found: list[tuple[str, dict]] = []
    for path in sorted(_OVERLAYS.rglob("*.yaml")):
        found.extend(
            (str(path.relative_to(_REPO_ROOT)), document.get("spec", {}) or {})
            for document in _documents(path)
            if document.get("kind") == "ArgoCD"
        )
    return found


_CRS = _argocd_crs()
_IDS = [path for path, _ in _CRS]


def _policy_lines(spec: dict) -> list[list[str]]:
    """De policy als lijst van velden per regel; commentaar en lege regels eruit."""
    policy = (spec.get("rbac", {}) or {}).get("policy", "") or ""
    lines: list[list[str]] = []
    for raw in policy.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        lines.append([part.strip() for part in line.split(",")])
    return lines


def test_elke_overlay_levert_een_policy() -> None:
    """Een lege parametrize slaat over in plaats van te falen, dus zonder deze toets melden
    de checks hieronder succes over niets."""
    covered = {Path(path).relative_to("bootstrap/rig-system/kustomize/overlays").parts[0] for path in _IDS}
    assert covered == {"odcn-production", "odcn-staging", "sandboxed-local"}, f"overlays gedekt: {sorted(covered)}"
    for path, spec in _CRS:
        assert _policy_lines(spec), f"{path} levert geen rbac.policy"


@pytest.mark.parametrize(("path", "spec"), _CRS, ids=_IDS)
def test_alleen_de_beheergroepen_staan_op_role_admin(path: str, spec: dict) -> None:
    """role:admin kan Applications en AppProjects aanmaken, wijzigen en verwijderen, en
    repositories toevoegen. De ontwikkelaarsgroep stond er tot RC-345 ook op."""
    admins = {line[1] for line in _policy_lines(spec) if line[0] == "g" and line[2] == "role:admin"}
    assert admins <= ADMIN_SUBJECTS, f"{path} zet {sorted(admins - ADMIN_SUBJECTS)} op role:admin"


@pytest.mark.parametrize(("path", "spec"), _CRS, ids=_IDS)
def test_de_eigen_rollen_schrijven_nergens(path: str, spec: dict) -> None:
    """``role:zad-opi`` en ``role:zad-developer`` mogen lezen, syncen, refreshen en
    resource-actions doen. Heeft OPI er ooit wel een schrijfactie nodig, dan hoort die
    expliciet in deze toets genoemd te worden en niet stil in de policy te verschijnen."""
    for line in _policy_lines(spec):
        if line[0] != "p" or not line[1].startswith("role:zad-"):
            continue
        role, resource, action = line[1], line[2], line[3]
        if resource not in GUARDED_RESOURCES or line[5] != "allow":
            continue
        assert action not in WRITE_ACTIONS, f"{path}: {role} mag {action} op {resource}"


@pytest.mark.parametrize(("path", "spec"), _CRS, ids=_IDS)
def test_het_account_van_opi_kan_niet_inloggen_en_rbac_wordt_gelogd(path: str, spec: dict) -> None:
    """Alleen ``apiKey``: zonder ``login`` is er geen wachtwoord waarmee iemand als OPI de
    UI in komt. En een geweigerde aanroep hoort zichtbaar te zijn."""
    extra = spec.get("extraConfig", {}) or {}
    assert extra.get("accounts.opi") == "apiKey", f"{path} levert accounts.opi={extra.get('accounts.opi')!r}"
    assert extra.get("server.rbac.log.enforce.enable") == "true", f"{path} logt RBAC-beslissingen niet"


@pytest.mark.parametrize(("path", "spec"), _CRS, ids=_IDS)
def test_het_admin_account_houdt_zijn_login(path: str, spec: dict) -> None:
    """Met ``defaultPolicy: role:none`` is admin de enige die nog zonder groep de UI in komt,
    en op de sandbox de enige menselijke login: er is daar geen SSO. Wie hem op ``apiKey``
    zet of ``disableAdmin`` aanzet sluit daarmee iedereen buiten."""
    extra = spec.get("extraConfig", {}) or {}
    assert "accounts.admin" not in extra, f"{path} zet accounts.admin={extra['accounts.admin']!r}"
    assert extra.get("admin.enabled", "true") == "true", f"{path} zet admin.enabled uit"
    assert spec.get("disableAdmin") is not True, f"{path} zet disableAdmin aan"


@pytest.mark.parametrize(("path", "spec"), _CRS, ids=_IDS)
def test_het_account_van_opi_heeft_zijn_rol(path: str, spec: dict) -> None:
    lines = _policy_lines(spec)
    assert ["g", "opi", "role:zad-opi"] in lines, f"{path} bindt het account opi niet aan role:zad-opi"


def test_de_role_op_de_cr_s_draagt_precies_de_verbs_uit_de_inventaris() -> None:
    """De Kubernetes-kant. Staat in base, zodat beide clusters hem krijgen: de
    namespace-transformer van de odcn-overlay zet hem in rig-prd-operations."""
    documents = _documents(_OPI_KUSTOMIZE / "base/argo-application-role.yaml")
    role = next(d for d in documents if d["kind"] == "Role")
    found = {
        resource: set(rule["verbs"])
        for rule in role["rules"]
        if rule["apiGroups"] == ["argoproj.io"]
        for resource in rule["resources"]
    }
    assert found == ARGO_CR_VERBS, f"de Role draagt {found}"

    binding = next(d for d in documents if d["kind"] == "RoleBinding")
    assert binding["roleRef"]["name"] == role["metadata"]["name"]
    assert binding["subjects"] == [{"kind": "ServiceAccount", "name": "namespace-manager", "namespace": "rig-system"}]


def test_geen_clusterrole_geeft_nog_cluster_breed_recht_op_de_cr_s() -> None:
    """Een Role in de namespace van ArgoCD is alleen een inperking zolang er geen
    ClusterRole naast staat die hetzelfde cluster-breed weggeeft."""
    for path in sorted(_OPI_KUSTOMIZE.rglob("*.yaml")):
        for document in _documents(path):
            if document.get("kind") != "ClusterRole":
                continue
            for rule in document.get("rules", []):
                if "argoproj.io" not in (rule.get("apiGroups") or []):
                    continue
                pytest.fail(f"{path.relative_to(_REPO_ROOT)} geeft {rule} cluster-breed weg")
