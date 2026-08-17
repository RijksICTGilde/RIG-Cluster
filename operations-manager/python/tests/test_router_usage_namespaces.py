"""Regression tests for the namespace list on the admin usage page.

``_get_available_namespaces`` builds namespace strings from the project cache.
When ``ProjectStore.get_all()`` changed from ``dict[str, ProjectSummary]`` to
``list[ProjectSummary]``, ``for name in projects`` silently started yielding Project
objects instead of names, so the f-string interpolated the whole pydantic repr:

    rig-prd-name='demo' api_key='SECRET' filename='demo.yaml' ...

That breaks the PromQL namespace filter and renders every project's plaintext
API key into the admin page. It raises no exception, which is why the whole
suite stayed green -- hence these tests.
"""

from __future__ import annotations

from unittest.mock import patch

from opi.services.project_service import ProjectSummary
from opi.web.router_usage import _get_available_namespaces

CLUSTER = "odcn-production"


def _projects() -> list[ProjectSummary]:
    return [
        ProjectSummary(name="alpha", api_key="SECRET-ALPHA", filename="alpha.yaml", data={}),
        ProjectSummary(name="bravo", api_key="SECRET-BRAVO", filename="bravo.yaml", data={}),
    ]


def _namespaces() -> list[str]:
    with patch("opi.web.router_usage.get_project_store") as store:
        store.return_value.get_all.return_value = _projects()
        return _get_available_namespaces(CLUSTER)


def test_namespaces_are_built_from_project_names() -> None:
    """Each project contributes exactly one namespace ending in its own name."""
    namespaces = _namespaces()

    assert any(ns.endswith("alpha") for ns in namespaces), namespaces
    assert any(ns.endswith("bravo") for ns in namespaces), namespaces


def test_namespaces_never_leak_project_internals() -> None:
    """No API key or other Project field may reach the rendered namespace list.

    Fails loudly on the repr-interpolation bug: the object repr carries
    api_key=, filename= and data= into the string.
    """
    namespaces = _namespaces()

    for ns in namespaces:
        assert "SECRET-ALPHA" not in ns, f"plaintext API key leaked into namespace: {ns}"
        assert "SECRET-BRAVO" not in ns, f"plaintext API key leaked into namespace: {ns}"
        assert "api_key=" not in ns, f"Project repr leaked into namespace: {ns}"
        assert "filename=" not in ns, f"Project repr leaked into namespace: {ns}"


def test_namespaces_are_plausible_kubernetes_names() -> None:
    """A namespace is a short dns-label-ish string, not a serialised object."""
    for ns in _namespaces():
        assert len(ns) < 64, f"namespace far too long, likely an object repr: {ns}"
        assert "'" not in ns, f"namespace contains repr punctuation: {ns}"
        assert "=" not in ns, f"namespace contains repr punctuation: {ns}"
