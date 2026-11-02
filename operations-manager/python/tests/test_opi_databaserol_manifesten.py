"""De manifesten achter OPI's eigen databaserol (RC-410).

De code in ``tests/test_opi_op_een_eigen_databaserol.py`` opent de database met
``DATABASE_APP_USER``. Dat werkt alleen als het cluster die rol ook echt heeft, en als de
pod zijn wachtwoord krijgt. Gemeten op de GERENDERDE manifesten, want de rol uit
``cluster.yaml`` halen of het secret optioneel maken verandert allebei alleen de render.

De SOPS-generators worden uit de kopie gehaald voordat er gerenderd wordt: die vragen de
AGE-sleutel van het cluster, en die staat niet in de repo. Dezelfde aanpak als
``tests/test_argocd_auth_env_per_overlay.py``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from opi.core.database_pools import MAIN_POOL_MAX_SIZE
from opi.core.db import ORM_MAX_OVERFLOW, ORM_POOL_SIZE

_REPO_ROOT = Path(__file__).resolve().parents[3]
_OPI_KUSTOMIZE = _REPO_ROOT / "bootstrap/rig-system/kustomize/operations-manager"
_POSTGRESQL_BASE = _REPO_ROOT / "infrastructure/bootstrap/infrastructure/postgresql/database/base"
_ODCN_DECRYPT = _REPO_ROOT / "infrastructure/bootstrap/infrastructure/secrets/config/overlays/odcn/decrypt-sops.yaml"
_SECRET_TEMPLATE = (
    _REPO_ROOT / "infrastructure/bootstrap/infrastructure/secrets/templates/operations-manager-db-secret.yaml"
)

_TASKFILE = _REPO_ROOT / "Taskfile.yaml"
_SOPS_BESTAND = "operations-manager-db-secret.yaml.sops.yaml"

#: De naam van de rol, het secret en de database. Eén waarde, want cluster.yaml,
#: databases.yaml, de template en de Deployment moeten dezelfde noemen.
ROL = "operations_manager"
SECRET = "operations-manager-db-credentials"


def _build(pad: Path) -> list[dict[str, Any]]:
    if shutil.which("kustomize") is None:
        pytest.skip("kustomize ontbreekt")
    rendered = subprocess.run(
        ["kustomize", "build", str(pad), "--load-restrictor", "LoadRestrictionsNone"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [document for document in yaml.safe_load_all(rendered) if document]


def _zonder_sops(bron: Path, doel: Path) -> Path:
    """Kopie van een kustomize-boom zonder de generators die een AGE-sleutel vragen."""
    kopie = doel / bron.name
    shutil.copytree(bron, kopie)
    for kustomization in kopie.rglob("kustomization.yaml"):
        document = yaml.safe_load(kustomization.read_text())
        if document.pop("generators", None):
            kustomization.write_text(yaml.safe_dump(document))
    return kopie


@pytest.fixture(scope="module")
def opi_env_per_overlay(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict[str, Any]]:
    """De DATABASE_*-env van de OPI-container, per overlay."""
    overlays = _zonder_sops(_OPI_KUSTOMIZE, tmp_path_factory.mktemp("opi")) / "overlays"
    per_overlay: dict[str, dict[str, Any]] = {}
    for overlay in sorted(overlays.iterdir()):
        if not overlay.is_dir():
            continue
        deployment = next(
            document
            for document in _build(overlay)
            if document["kind"] == "Deployment" and document["metadata"]["name"] == "operations-manager"
        )
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        per_overlay[overlay.name] = {
            entry["name"]: entry.get("value", entry.get("valueFrom"))
            for entry in container["env"]
            if entry["name"].startswith("DATABASE_")
        }
    return per_overlay


@pytest.fixture(scope="module")
def postgresql_base() -> list[dict[str, Any]]:
    return _build(_POSTGRESQL_BASE)


def test_elke_overlay_is_gerenderd(opi_env_per_overlay: dict[str, dict[str, Any]]) -> None:
    """Zonder deze toets melden de checks hieronder succes over een lege lijst."""
    assert set(opi_env_per_overlay) == {
        "odcn-production",
        "odcn-staging",
        "sandboxed-local",
        "sandboxed-local-debug",
        "sandboxed-local-dev",
    }, f"gerenderd: {sorted(opi_env_per_overlay)}"


def test_elke_overlay_leest_de_app_rol_uit_het_secret(opi_env_per_overlay: dict[str, dict[str, Any]]) -> None:
    """Bewust NIET optional: zonder dit secret start de pod niet, en dat is luider dan
    stil terugvallen op de superuser. Infra gaat dus vóór de OPI-release."""
    for naam, env in opi_env_per_overlay.items():
        assert env.get("DATABASE_APP_USER") == {"secretKeyRef": {"name": SECRET, "key": "username"}}, (
            f"{naam} leest DATABASE_APP_USER niet uit {SECRET}: {env.get('DATABASE_APP_USER')!r}"
        )
        assert env.get("DATABASE_APP_PASSWORD") == {"secretKeyRef": {"name": SECRET, "key": "password"}}, (
            f"{naam} leest DATABASE_APP_PASSWORD niet uit {SECRET}: {env.get('DATABASE_APP_PASSWORD')!r}"
        )


def test_de_eigen_database_is_van_de_app_rol(postgresql_base: list[dict[str, Any]]) -> None:
    database = next(
        document
        for document in postgresql_base
        if document["kind"] == "Database" and document["metadata"]["name"] == "operations-manager-db"
    )

    assert database["spec"]["name"] == ROL
    assert database["spec"]["owner"] == ROL, "de database is nog van de superuser"


def test_de_app_rol_is_geen_superuser(postgresql_base: list[dict[str, Any]]) -> None:
    """Dit is de hele winst van de taak: geen superuser op OPI's eigen tabellen."""
    cluster = next(document for document in postgresql_base if document["kind"] == "Cluster")
    rol = next(entry for entry in cluster["spec"]["managed"]["roles"] if entry["name"] == ROL)

    assert rol["login"] is True
    assert rol["superuser"] is False
    assert rol["createdb"] is False
    assert rol["createrole"] is False
    assert rol["passwordSecret"]["name"] == SECRET
    # 10 per pod (MAIN_POOL_MAX_SIZE 5 + ORM_POOL_SIZE 5), twee pods tijdens een uitrol,
    # plus Alembic. tests/test_opi_op_een_eigen_databaserol.py legt die 10 vast in de code.
    piek = 2 * (MAIN_POOL_MAX_SIZE + ORM_POOL_SIZE + ORM_MAX_OVERFLOW) + 1
    assert rol["connectionLimit"] >= piek, "de pools van twee OPI-pods en Alembic passen er niet in"


def test_de_template_levert_de_twee_sleutels_die_cnpg_vraagt() -> None:
    template = yaml.safe_load(_SECRET_TEMPLATE.read_text())

    assert template["type"] == "kubernetes.io/basic-auth"
    assert template["metadata"]["name"] == SECRET
    assert template["metadata"]["labels"]["cnpg.io/reload"] == "true"
    assert template["stringData"]["username"] == ROL
    assert template["stringData"]["password"], "het wachtwoord wordt door de generator gevuld"


def test_de_template_rendert_het_secret_voor_elk_cluster() -> None:
    """De templatemap is de bron voor elk cluster: odcn, odcn-staging en sandboxed-local
    genereren hun eigen SOPS-bestand hieruit."""
    secret = next(
        document
        for document in _build(_SECRET_TEMPLATE.parent)
        if document["kind"] == "Secret" and document["metadata"]["name"] == SECRET
    )

    assert secret["stringData"]["username"] == ROL


def test_odcn_ontsleutelt_het_nieuwe_secret() -> None:
    """Zonder deze regel bestaat het secret op productie niet en start de pod niet."""
    ksops = yaml.safe_load(_ODCN_DECRYPT.read_text())

    assert _SOPS_BESTAND in ksops["files"]
    assert (_ODCN_DECRYPT.parent / _SOPS_BESTAND).exists()


def test_de_sandbox_genereert_het_secret_uit_dezelfde_template() -> None:
    """De rol staat in base en komt dus op elk cluster, dus het secret moet dat ook.

    De sandbox-overlay van de secrets staat in .gitignore: de taak
    ``sandbox:generate-infrastructure-secrets`` maakt hem bij de setup uit deze templatemap,
    met de sandbox-AGE-sleutel en FIXED_PASSWORD, en zet de regel in zijn eigen
    decrypt-sops.yaml. Een bestaand geheim
    slaat die taak per bestand over, dus een nieuwe template komt er op een draaiend
    cluster gewoon bij. Daarom is hier de route de toets en niet een gerenderde overlay:
    gebouwd is hij met de hand, zie de meting op de PR.
    """
    assert (
        _SECRET_TEMPLATE.name
        in yaml.safe_load((_SECRET_TEMPLATE.parent / "kustomization.yaml").read_text())["resources"]
    ), "zonder deze regel rendert geen enkel cluster het secret"
    assert _SECRET_TEMPLATE.name.endswith("-secret.yaml"), (
        "de generator in Taskfile.yaml slaat elk bestand over dat niet zo heet"
    )

    sandbox_taak = yaml.safe_load(_TASKFILE.read_text())["tasks"]["sandbox:generate-infrastructure-secrets"]
    vars_van_de_taak = sandbox_taak["cmds"][0]["vars"]
    assert vars_van_de_taak["TEMPLATES_DIR"] == str(_SECRET_TEMPLATE.parent.relative_to(_REPO_ROOT))
    assert vars_van_de_taak["CLUSTER_FOLDER"] == "sandboxed-local"
    assert vars_van_de_taak["FIXED_PASSWORD"], "de sandbox houdt zijn vaste wachtwoorden"
