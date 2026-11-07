"""Elke aanroep die OPI bij een afhankelijkheid doet, is tegen die echte afhankelijkheid gemeten.

Per afhankelijkheid draait een groep OPI tegen een echte instantie in de gepinde versie: de
connector methode voor methode, en de manager over meer dan een deployment. Deze toets
bewaakt de volledigheid van die groepen:

1. elke publieke connectormethode die ``opi/`` aanroept, wordt in de groep aangeroepen;
2. voor Keycloak bovendien: elke DIRECTE ``.admin.*``-aanroep buiten de connector noemt de
   toets die hem draait, zodat er geen nieuwe ongemeten bij kan komen. De manager en de
   yaml-handler reiken langs de connector, en zij bepalen de ORDE van de stappen: daar zat
   de 409 van Keycloak 26.8.1 (RC-433);
3. niets in ``opi/`` gebruikt nog ``raw_post``/``raw_put``/``raw_delete``.

Statisch met opzet, zodat hij in de standaardrun draait terwijl de groepen zelf hun eigen
task nodig hebben. Een groene standaardrun zegt dus niets over de groepen; zie
``docs/development/toetsen.md``.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

import pytest

PYTHON_ROOT = Path(__file__).resolve().parent.parent
OPI_ROOT = PYTHON_ROOT / "opi"


@dataclass(frozen=True)
class Connector:
    """Een connectorklasse, als pad binnen ``opi/`` en klassenaam."""

    module: str
    klass: str

    @property
    def path(self) -> Path:
        return PYTHON_ROOT / "opi" / self.module


@dataclass(frozen=True)
class Dependency:
    """Een afhankelijkheid met een live groep, en wat die groep moet dekken."""

    name: str
    connectors: tuple[Connector, ...]
    group: tuple[str, ...]
    #: Methoden die ``opi/`` aanroept en die de groep met opzet niet meet, met de reden.
    #: Een regel hier is een beslissing en geen punt op een lijst.
    exempt: dict[str, str] = field(default_factory=dict)
    #: Publieke methoden zonder enkele aanroeper in ``opi/``. Ze vallen buiten het gemeten
    #: oppervlak; ze weghalen is een losse beslissing.
    unused: dict[str, str] = field(default_factory=dict)

    @property
    def group_modules(self) -> tuple[Path, ...]:
        return tuple(PYTHON_ROOT / "tests" / module for module in self.group)


KEYCLOAK = Dependency(
    name="keycloak",
    connectors=(Connector("connectors/keycloak.py", "KeycloakConnector"),),
    group=("integration/test_keycloak_admin_api.py", "integration/test_keycloak_scenarios.py"),
    unused={
        "update_deployment_client_hosts": "no caller in opi/",
        "ensure_standard_oidc_mappers": "no caller in opi/",
        "assign_realm_management_role": "no caller in opi/",
        "assign_realm_role_to_user": "no caller in opi/",
    },
)

POSTGRES = Dependency(
    name="postgres",
    connectors=(Connector("connectors/postgres.py", "PostgresConnector"),),
    group=(
        "integration/test_postgres_live.py",
        "integration/test_postgres_scenarios.py",
        "test_clone_all_schemas.py",
    ),
    exempt={},
    unused={
        "test_connection": "no caller in opi/",
        "grant_schema_permissions": "no caller in opi/; grant_readonly_on_schema is the one in use",
    },
)

MINIO = Dependency(
    name="minio",
    connectors=(Connector("connectors/minio_mc.py", "MinioConnector"),),
    group=("integration/test_minio_live.py",),
    exempt={},
    unused={
        "create_policy": "no caller in opi/; grant_bucket_access writes the policy",
        "enable_bucket_versioning": "no caller in opi/; ensure_bucket_versioning is the one in use",
        "suspend_bucket_versioning": "no caller in opi/; ensure_bucket_versioning is the one in use",
        "get_bucket_lifecycle": "no caller in opi/",
        "get_configured_aliases": "no caller in opi/",
        "test_alias_connection": "no caller in opi/; configure_alias already tests the connection",
    },
)

GIT = Dependency(
    name="git",
    connectors=(Connector("connectors/git.py", "GitConnector"), Connector("connectors/git_repo.py", "GitRepo")),
    group=("integration/test_forgejo_live.py",),
    exempt={},
    unused={},
)

KUBERNETES = Dependency(
    name="kubernetes",
    connectors=(Connector("connectors/kubectl.py", "KubectlConnector"),),
    group=("integration/test_kubernetes_live.py",),
    exempt={},
    unused={
        "apply_annotation_to_resource": "no caller in opi/; apply_labels_to_resource is the one in use",
        "encrypt_file_with_sops": "no caller in opi/; opi/utils/sops.py encrypts without kubectl",
        "get_namespace_label_map": "no caller in opi/; get_namespace_labels_map is the one in use",
    },
)

ARGOCD = Dependency(
    name="argocd",
    connectors=(Connector("connectors/argo.py", "ArgoConnector"),),
    group=("integration/test_argocd_live.py", "integration/test_argocd_scenarios.py"),
    exempt={},
    unused={
        "login_and_sync": "no caller in opi/; login plus sync_application, both measured on their own",
        "sync_application": "no caller in opi/; OPI syncs through git, the group measures it anyway",
        "hard_refresh_application": "no caller in opi/; refresh_application(hard_refresh=True) is the one in use",
    },
)

DEPENDENCIES = (KEYCLOAK, POSTGRES, MINIO, GIT, KUBERNETES, ARGOCD)

#: raw_post en broers geven het antwoord maar niemand keek; fouten verdwenen stil. Via
#: benoemde python-keycloak-methoden gooit het beter.
RAW_METHODS = ("raw_post", "raw_put", "raw_delete", "raw_get")

#: Directe admin-aanroepen buiten de connector, elk met de toets die hem meet. Een nieuwe
#: ongemeten aanroep laat deze toets falen.
DIRECT_ADMIN_CALLS: dict[str, str] = {
    "change_current_realm": "test_the_deployments_of_a_project_share_a_realm",
    "create_client": "test_the_deployments_of_a_project_share_a_realm",
    "get_clients": "test_the_deployments_of_a_project_share_a_realm",
    "update_client": "test_the_platform_client_is_repaired_on_the_next_run",
}

#: Directe ``admin.<methode>``-aanroepen die de groep niet bereikt, met de reden.
DIRECT_ADMIN_EXEMPT: dict[str, str] = {
    "get_client_service_account_user": "only through serviceAccountClientRoles, which only the algoritmeregister blueprint uses",
    "get_client_roles": "same path as get_client_service_account_user",
    "assign_client_role": "same path as get_client_service_account_user",
}


@cache
def _tree(path: Path) -> ast.Module:
    """De AST van een bestand, een keer per run: elke toets hieronder loopt heel ``opi/`` na."""
    return ast.parse(path.read_text())


@cache
def _opi_files() -> tuple[Path, ...]:
    return tuple(sorted(OPI_ROOT.rglob("*.py")))


def _public_methods(connector: Connector) -> set[str]:
    klass = next(
        node for node in _tree(connector.path).body if isinstance(node, ast.ClassDef) and node.name == connector.klass
    )
    return {
        node.name
        for node in klass.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and not node.name.startswith("_")
    }


def _attribute_calls(path: Path, names: set[str]) -> set[str]:
    """De namen uit ``names`` die ``path`` als attribuut aanroept (``x.name(...)``)."""
    return {
        node.func.attr
        for node in ast.walk(_tree(path))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in names
    }


def _called_from_opi(dependency: Dependency, names: set[str]) -> set[str]:
    own = {connector.path for connector in dependency.connectors}
    called: set[str] = set()
    for path in _opi_files():
        if path in own:
            continue
        called |= _attribute_calls(path, names)
    return called


def _direct_admin_calls() -> dict[str, set[str]]:
    """De ``<iets>.admin.<methode>(...)``-aanroepen buiten de connector, per methode.

    De connector is de enige plek die bij de admin-client mag; elders gaat dit langs de
    connector heen, dus de waarde is de verzameling bestanden die dat doet.
    """
    connector = KEYCLOAK.connectors[0].path
    found: dict[str, set[str]] = {}
    for path in _opi_files():
        if path == connector:
            continue
        for node in ast.walk(_tree(path)):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            owner = node.func.value
            if isinstance(owner, ast.Attribute) and owner.attr == "admin":
                found.setdefault(node.func.attr, set()).add(str(path.relative_to(PYTHON_ROOT)))
    return found


def _measured_names(dependency: Dependency) -> set[str]:
    """Elke naam die de groep als attribuut aanroept, plus de namen van zijn eigen toetsen."""
    names: set[str] = set()
    for module in dependency.group_modules:
        for node in ast.walk(_tree(module)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                names.add(node.func.attr)
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                names.add(node.name)
    return names


@pytest.mark.parametrize("dependency", DEPENDENCIES, ids=lambda d: d.name)
def test_the_group_exists(dependency: Dependency) -> None:
    """Een afhankelijkheid in deze lijst heeft zijn groep, anders meet de lijst niets."""
    missing = [str(module) for module in dependency.group_modules if not module.exists()]
    assert not missing, f"De groep {dependency.name}_live verwijst naar modules die er niet zijn: {missing}"


@pytest.mark.parametrize("dependency", DEPENDENCIES, ids=lambda d: d.name)
def test_every_connector_method_opi_calls_is_measured(dependency: Dependency) -> None:
    for connector in dependency.connectors:
        public = _public_methods(connector)
        called = _called_from_opi(dependency, public)
        measured: set[str] = set()
        for module in dependency.group_modules:
            measured |= _attribute_calls(module, public)

        unmeasured = called - measured - dependency.exempt.keys()
        assert not unmeasured, (
            f"Aangeroepen uit opi/ maar niet gemeten in de groep {dependency.name}_live "
            f"({connector.klass}): {sorted(unmeasured)}. Voeg een toets toe, of een regel in "
            "exempt met de reden."
        )


@pytest.mark.parametrize("dependency", DEPENDENCIES, ids=lambda d: d.name)
def test_unused_methods_have_no_caller(dependency: Dependency) -> None:
    """De unused-lijst blijft waar: een methode die een aanroeper krijgt, wordt gemeten."""
    for connector in dependency.connectors:
        public = _public_methods(connector)
        called = _called_from_opi(dependency, public)

        gained_a_caller = sorted(dependency.unused.keys() & called)
        assert not gained_a_caller, f"unused noemt methoden die opi/ nu aanroept: {gained_a_caller}"

    all_public = {name for connector in dependency.connectors for name in _public_methods(connector)}
    gone = sorted(dependency.unused.keys() - all_public)
    assert not gone, f"unused noemt methoden die de connector niet meer heeft: {gone}"


@pytest.mark.parametrize("dependency", DEPENDENCIES, ids=lambda d: d.name)
def test_exemptions_are_about_methods_that_exist_and_are_called(dependency: Dependency) -> None:
    """Een uitzondering die niet meer geldt, gaat weg in plaats van blijven staan."""
    called: set[str] = set()
    for connector in dependency.connectors:
        called |= _called_from_opi(dependency, _public_methods(connector))

    stale = sorted(dependency.exempt.keys() - called)
    assert not stale, f"exempt noemt methoden die opi/ niet aanroept: {stale}"

    assert all(reason for reason in dependency.exempt.values()), "Elke uitzondering noemt een reden"
    assert all(reason for reason in dependency.unused.values()), "Elke ongebruikte methode noemt een reden"


def test_every_direct_admin_call_is_measured_or_exempt() -> None:
    """De manager en de handler reiken langs de connector; die paden tellen ook."""
    direct = _direct_admin_calls()

    unlisted = sorted(set(direct) - DIRECT_ADMIN_CALLS.keys() - DIRECT_ADMIN_EXEMPT.keys())
    assert not unlisted, (
        f"Directe admin.*-aanroepen buiten de connector die niemand claimt te meten: "
        f"{ {name: sorted(direct[name]) for name in unlisted} }. Noem de toets in de groep die "
        "hem draait in DIRECT_ADMIN_CALLS, of zonder hem uit met een reden."
    )

    gone = sorted((DIRECT_ADMIN_CALLS.keys() | DIRECT_ADMIN_EXEMPT.keys()) - direct.keys())
    assert not gone, f"Deze admin.*-aanroepen doet opi/ niet meer: {gone}"

    measured = _measured_names(KEYCLOAK)
    missing = sorted(test for test in DIRECT_ADMIN_CALLS.values() if test not in measured)
    assert not missing, f"DIRECT_ADMIN_CALLS wijst naar toetsen die de groep niet heeft: {missing}"

    assert all(reason for reason in DIRECT_ADMIN_EXEMPT.values()), "Elke uitzondering noemt een reden"


def test_no_raw_http_calls_in_opi() -> None:
    """Een raw-aanroep negeert de HTTP-status, dus een 409 die iets anders betekende verdween."""
    offenders = {
        str(path.relative_to(PYTHON_ROOT)): sorted(calls)
        for path in _opi_files()
        if (calls := _attribute_calls(path, set(RAW_METHODS)))
    }
    assert not offenders, (
        f"Kale HTTP-aanroepen op de Keycloak-verbinding: {offenders}. Gebruik de benoemde "
        "python-keycloak-methode, die gooit bij een onverwachte status."
    )
