"""Every Keycloak call OPI makes must be measured against a real Keycloak.

The group ``keycloak_live`` runs OPI against a live Keycloak: the connector method by method
(``tests/integration/test_keycloak_admin_api.py``) and the manager over several deployments
in one realm (``tests/integration/test_keycloak_scenarios.py``). This test guards the
completeness of that group in three ways:

1. every public connector method that ``opi/`` calls is called in the group;
2. every DIRECT ``.admin.*`` call outside the connector names the test that drives it, so a
   new one cannot slip in unmeasured. The manager and the yaml handler reach past the
   connector, and they decide the ORDER the steps run in, which is where the 409 of
   Keycloak 26.8.1 hid (RC-433);
3. nothing in ``opi/`` uses ``raw_post``/``raw_put``/``raw_delete`` again.

Static on purpose, so it runs in the normal test run while the group needs
``task test-keycloak``.
"""

from __future__ import annotations

import ast
from pathlib import Path

PYTHON_ROOT = Path(__file__).resolve().parent.parent
CONNECTOR = PYTHON_ROOT / "opi" / "connectors" / "keycloak.py"
OPI_ROOT = PYTHON_ROOT / "opi"
INTEGRATION_MODULE = PYTHON_ROOT / "tests" / "integration" / "test_keycloak_admin_api.py"
SCENARIO_MODULE = PYTHON_ROOT / "tests" / "integration" / "test_keycloak_scenarios.py"
GROUP_MODULES = (INTEGRATION_MODULE, SCENARIO_MODULE)

#: ``raw_post`` and friends return the HTTP response and nobody looked at it: a 409 that
#: meant something else than "already there" disappeared without a trace, and the execution
#: it should have configured stayed empty. Every call goes through a named python-keycloak
#: method, which raises.
RAW_METHODS = ("raw_post", "raw_put", "raw_delete", "raw_get")

#: Direct ``admin.<method>`` calls from outside the connector, each naming the test in the
#: group that drives its path. Statically this is an inventory and not proof of execution,
#: so the pointer is the part that matters: it says where to look, and a new direct call
#: fails this test until someone either measures it or exempts it below.
DIRECT_ADMIN_CALLS: dict[str, str] = {
    "change_current_realm": "test_the_deployments_of_a_project_share_a_realm",
    "create_client": "test_the_deployments_of_a_project_share_a_realm",
    "get_clients": "test_the_deployments_of_a_project_share_a_realm",
    "update_client": "test_the_platform_client_is_repaired_on_the_next_run",
}

#: Direct ``admin.<method>`` calls that the group does not reach, with the reason.
DIRECT_ADMIN_EXEMPT: dict[str, str] = {
    "get_client_service_account_user": "only through serviceAccountClientRoles, which only the algoritmeregister blueprint uses",
    "get_client_roles": "same path as get_client_service_account_user",
    "assign_client_role": "same path as get_client_service_account_user",
}

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


def _direct_admin_calls() -> dict[str, set[str]]:
    """The ``<something>.admin.<method>(...)`` calls outside the connector, per method.

    The connector itself is the one place that may reach the admin client; everywhere else
    this bypasses the connector, so the value is the set of files that do it.
    """
    found: dict[str, set[str]] = {}
    for path in sorted(OPI_ROOT.rglob("*.py")):
        if path == CONNECTOR:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            owner = node.func.value
            if isinstance(owner, ast.Attribute) and owner.attr == "admin":
                found.setdefault(node.func.attr, set()).add(str(path.relative_to(PYTHON_ROOT)))
    return found


def _measured_names() -> set[str]:
    """Every name the group calls as an attribute, plus the names of its own tests."""
    names: set[str] = set()
    for module in GROUP_MODULES:
        tree = ast.parse(module.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                names.add(node.func.attr)
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                names.add(node.name)
    return names


def test_every_connector_method_opi_calls_is_measured() -> None:
    public = _public_connector_methods()
    called = _called_from_opi(public)
    measured: set[str] = set()
    for module in GROUP_MODULES:
        measured |= _attribute_calls(module, public)

    unmeasured = called - measured - EXEMPT.keys()
    assert not unmeasured, (
        f"Called from opi/ but not measured in the keycloak_live group: {sorted(unmeasured)}. "
        "Add a test there, or an EXEMPT entry with the reason."
    )


def test_every_direct_admin_call_is_measured_or_exempt() -> None:
    """The manager and the handler reach past the connector; those paths count too."""
    direct = _direct_admin_calls()

    unlisted = sorted(set(direct) - DIRECT_ADMIN_CALLS.keys() - DIRECT_ADMIN_EXEMPT.keys())
    assert not unlisted, (
        f"Direct admin.* calls outside the connector that nothing claims to measure: "
        f"{ {name: sorted(direct[name]) for name in unlisted} }. Name the test in the group that "
        "drives it in DIRECT_ADMIN_CALLS, or exempt it with a reason."
    )

    gone = sorted((DIRECT_ADMIN_CALLS.keys() | DIRECT_ADMIN_EXEMPT.keys()) - direct.keys())
    assert not gone, f"These admin.* calls are no longer made from opi/: {gone}"

    measured = _measured_names()
    missing = sorted(test for test in DIRECT_ADMIN_CALLS.values() if test not in measured)
    assert not missing, f"DIRECT_ADMIN_CALLS points at tests the group does not have: {missing}"

    assert all(reason for reason in DIRECT_ADMIN_EXEMPT.values()), "Every exemption states a reason"


def test_no_raw_http_calls_in_opi() -> None:
    """A raw call ignores the HTTP status, so a 409 that meant something else vanished."""
    offenders = {
        str(path.relative_to(PYTHON_ROOT)): sorted(calls)
        for path in sorted(OPI_ROOT.rglob("*.py"))
        if (calls := _attribute_calls(path, set(RAW_METHODS)))
    }
    assert not offenders, (
        f"Raw HTTP calls on the Keycloak connection: {offenders}. Use the named python-keycloak "
        "method, which raises on an unexpected status."
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
