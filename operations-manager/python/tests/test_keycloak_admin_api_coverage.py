"""Every connector method OPI calls must be measured against a real Keycloak.

The integration module ``tests/integration/test_keycloak_admin_api.py`` runs
``KeycloakConnector`` against a live Keycloak. This test guards its completeness: it reads
the connector's public methods, the call sites in ``opi/``, and the calls in the
integration module, and fails when OPI starts calling a method that nothing measures.

Static on purpose, so it runs in the normal test run while the integration module needs
``task test-keycloak-image``.
"""

from __future__ import annotations

import ast
from pathlib import Path

PYTHON_ROOT = Path(__file__).resolve().parent.parent
CONNECTOR = PYTHON_ROOT / "opi" / "connectors" / "keycloak.py"
OPI_ROOT = PYTHON_ROOT / "opi"
INTEGRATION_MODULE = PYTHON_ROOT / "tests" / "integration" / "test_keycloak_admin_api.py"

#: Methods that are called from ``opi/`` but deliberately not measured, with the reason.
#: An entry here is a decision, not a backlog item: anything removed from the integration
#: module without a reason here makes this test fail.
EXEMPT: dict[str, str] = {}

#: Public methods with no caller anywhere in ``opi/``. They are outside the measured
#: surface because nothing in OPI depends on them; removing them is a separate decision.
UNUSED: dict[str, str] = {
    "update_deployment_client_hosts": "no caller in opi/",
    "ensure_standard_oidc_mappers": "no caller in opi/",
    "assign_realm_management_role": "no caller in opi/",
    "assign_realm_role_to_user": "no caller in opi/",
}


def _public_connector_methods() -> set[str]:
    tree = ast.parse(CONNECTOR.read_text())
    klass = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "KeycloakConnector")
    return {
        node.name
        for node in klass.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and not node.name.startswith("_")
    }


def _attribute_calls(path: Path, names: set[str]) -> set[str]:
    """The names from ``names`` that ``path`` calls as an attribute (``x.name(...)``)."""
    tree = ast.parse(path.read_text())
    return {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in names
    }


def _called_from_opi(names: set[str]) -> set[str]:
    called: set[str] = set()
    for path in sorted(OPI_ROOT.rglob("*.py")):
        if path == CONNECTOR:
            continue
        called |= _attribute_calls(path, names)
    return called


def test_every_connector_method_opi_calls_is_measured() -> None:
    public = _public_connector_methods()
    called = _called_from_opi(public)
    measured = _attribute_calls(INTEGRATION_MODULE, public)

    unmeasured = called - measured - EXEMPT.keys()
    assert not unmeasured, (
        f"Called from opi/ but not measured in {INTEGRATION_MODULE.name}: {sorted(unmeasured)}. "
        "Add a test there, or an EXEMPT entry with the reason."
    )


def test_unused_methods_have_no_caller() -> None:
    """The UNUSED list stays honest: an entry that gains a caller must be measured."""
    public = _public_connector_methods()
    called = _called_from_opi(public)

    gained_a_caller = sorted(UNUSED.keys() & called)
    assert not gained_a_caller, f"UNUSED lists methods that opi/ now calls: {gained_a_caller}"

    gone = sorted(UNUSED.keys() - public)
    assert not gone, f"UNUSED lists methods the connector no longer has: {gone}"


def test_exemptions_are_about_methods_that_exist_and_are_called() -> None:
    """An EXEMPT entry that no longer applies is removed rather than left behind."""
    public = _public_connector_methods()
    called = _called_from_opi(public)

    stale = sorted(EXEMPT.keys() - called)
    assert not stale, f"EXEMPT lists methods opi/ does not call: {stale}"

    assert all(reason for reason in EXEMPT.values()), "Every exemption states a reason"
