"""Regression tests for call sites that did not match their function.

Turning pyright's ``reportCallIssue`` on surfaced ten calls that disagreed with the
function they call. They were not type nits: each one raised ``TypeError`` at runtime,
in some cases silently (an ``except Exception`` swallowed it and logged a warning).
These tests pin the behaviour those calls were supposed to have, so the same class of
break cannot come back without a red test as well as a red type check.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.forms.editables.editable import Editable
from opi.forms.editables.processor import _validator_accepts_context


class TestGitMonitorClusterCheck:
    """``file_change_handler`` called ``has_deployments_for_current_cluster(content)``.

    That method takes no arguments and is a coroutine function, so every project file
    with deployments raised ``TypeError`` before the namespace check below it could run.
    """

    @pytest.fixture
    def project_content(self) -> dict[str, Any]:
        return {
            "name": "demo",
            "deployments": [{"name": "prod", "cluster": "my-cluster", "namespace": "demo"}],
        }

    async def test_deployment_for_this_cluster_reaches_the_namespace_check(
        self, project_content: dict[str, Any]
    ) -> None:
        from opi.core import git_monitor

        with (
            patch.object(git_monitor.settings, "CLUSTER_MANAGER", "my-cluster"),
            patch.object(git_monitor, "validate_declared_project_schema"),
            patch.object(git_monitor, "check_and_create_namespaces", new=AsyncMock(return_value=True)) as check,
        ):
            await git_monitor.file_change_handler("projects/demo.yaml", project_content)

        check.assert_awaited_once_with(project_content)

    async def test_deployment_for_another_cluster_is_skipped(self, project_content: dict[str, Any]) -> None:
        from opi.core import git_monitor

        with (
            patch.object(git_monitor.settings, "CLUSTER_MANAGER", "other-cluster"),
            patch.object(git_monitor, "validate_declared_project_schema"),
            patch.object(git_monitor, "check_and_create_namespaces", new=AsyncMock(return_value=True)) as check,
        ):
            await git_monitor.file_change_handler("projects/demo.yaml", project_content)

        check.assert_not_awaited()


class TestBootstrapContextPublicHost:
    """``_build_context`` called ``generate_public_url`` with four positional arguments.

    ``generate_public_url(hostname, use_https, path)`` has taken three for a long time,
    so building the bootstrap context raised ``TypeError`` and no bootstrap action for
    any deployment could run.
    """

    async def _build(self, deployment: dict[str, Any]) -> dict[str, Any]:
        from opi.manager.bootstrap_manager import BootstrapManager

        project_manager = MagicMock()
        project_manager.get_name = AsyncMock(return_value="demo")
        manager = BootstrapManager(project_manager)

        with (
            patch("opi.core.cluster_config.get_ingress_postfix", return_value="apps.example.com"),
            patch("opi.core.cluster_config.get_ingress_tls_enabled", return_value=True),
        ):
            return await manager._build_context({"services": []}, deployment, "my-cluster")

    async def test_falls_back_to_the_deployment_name(self) -> None:
        context = await self._build({"name": "prod"})

        assert context["PUBLIC_HOST"] == "https://prod.apps.example.com"

    async def test_uses_the_subdomain_when_set(self) -> None:
        context = await self._build({"name": "prod", "subdomain": "shop"})

        assert context["PUBLIC_HOST"] == "https://shop.apps.example.com"

    async def test_uses_the_external_domain_when_set(self) -> None:
        context = await self._build({"name": "prod", "subdomain": "shop", "base-domain": "rijksapps.nl"})

        assert context["PUBLIC_HOST"] == "https://shop.rijksapps.nl"


class TestMarkedForDeletionConstruction:
    """``MarkedForDeletionService`` is ORM-backed and takes no constructor arguments.

    Two call sites in ``delete_project_manager`` passed it a database pool. The
    ``TypeError`` was caught by an ``except Exception`` that logged a warning and marked
    the manifests "skipped", so deferred cleanup never actually got registered -- a
    silent failure a type check catches and a log line does not.
    """

    def test_service_takes_no_constructor_arguments(self) -> None:
        from opi.services.marked_for_deletion_service import MarkedForDeletionService

        MarkedForDeletionService()

        with pytest.raises(TypeError):
            MarkedForDeletionService("main")  # type: ignore[call-arg]

    def test_no_call_site_passes_arguments(self) -> None:
        """Every instantiation in opi/ must be argument-free."""
        offenders: list[str] = []
        for path in Path("opi").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                    continue
                # Imported under an alias at both call sites, so match on both names.
                if node.func.id not in {"MarkedForDeletionService", "MFDService"}:
                    continue
                if node.args or node.keywords:
                    offenders.append(f"{path}:{node.lineno}")

        assert offenders == []


class _PlainValidator:
    def validate(self, value: Any) -> list[str]:
        return [] if value else ["leeg"]


class _ContextValidator:
    def validate(self, value: Any, context: dict[str, Any] | None = None) -> list[str]:
        return [] if (context or {}).get("allowed") == value else ["niet toegestaan"]


class _RaisingContextValidator:
    def validate(self, value: Any, context: dict[str, Any] | None = None) -> list[str]:
        raise TypeError("kapot")


class TestValidatorContextDispatch:
    """The call site passed ``context=`` to a protocol that does not declare it.

    It worked by catching ``TypeError`` and retrying without context. That also caught a
    ``TypeError`` raised *inside* a context-aware validator and silently re-ran it
    without its context, which is a different and worse thing. Which call form applies
    is now read off the validator's own signature.
    """

    @staticmethod
    def _validate(validator: Any, value: Any, context: dict[str, Any] | None) -> dict[str, list[str]]:
        from opi.forms.editables.processor import EditableFormProcessor

        errors: dict[str, list[str]] = {}
        vis = MagicMock()
        vis.editable = Editable(yaml_path="veld", validator=validator)
        EditableFormProcessor._validate_field(vis, "veld", value, errors, context)
        return errors

    def test_accepts_context_is_read_from_the_signature(self) -> None:
        assert _validator_accepts_context(_ContextValidator) is True
        assert _validator_accepts_context(_PlainValidator) is False

    def test_context_aware_validator_receives_the_context(self) -> None:
        assert self._validate(_ContextValidator(), "ja", {"allowed": "ja"}) == {}
        assert self._validate(_ContextValidator(), "nee", {"allowed": "ja"}) == {"veld": ["niet toegestaan"]}

    def test_plain_validator_is_called_without_context(self) -> None:
        assert self._validate(_PlainValidator(), "iets", {"allowed": "ja"}) == {}
        assert self._validate(_PlainValidator(), "", None) == {"veld": ["leeg"]}

    def test_typeerror_from_the_validator_is_not_swallowed(self) -> None:
        """It used to be retried without context; a broken validator now says so."""
        with pytest.raises(TypeError, match="kapot"):
            self._validate(_RaisingContextValidator(), "iets", {"allowed": "ja"})


class TestDomainSettingsResponse:
    """The domain-settings endpoint never filled ``domain_format`` in its response.

    ``DeploymentDomainSettingsResponse`` declares the field, the deployment carries it
    under ``domain-format``, and the sibling endpoint at the URL-settings modal passes
    it -- this one did not, so the modal always got ``null`` and could not preselect the
    format the deployment actually uses.
    """

    def test_domain_format_is_returned(self, mock_settings: Any) -> None:
        from fastapi.testclient import TestClient
        from opi.server import create_app

        deployment = {
            "name": "prod",
            "cluster": "my-cluster",
            "domain-mode": "custom",
            "domain-format": "deployment-subdomain",
            "subdomain": "shop",
            "base-domain": "rijksapps.nl",
            "root-component": "frontend",
            "components": [{"reference": "frontend"}],
        }
        project = MagicMock()
        project.filename = "demo.yaml"
        project.data = {"name": "demo", "deployments": [deployment]}
        store = MagicMock()
        store.get.return_value = project

        mock_user = {"email": "test@example.com", "name": "Test User"}
        mock_user_service = MagicMock()
        mock_user_service.is_email_allowed.return_value = True

        with (
            patch("opi.middleware.authorization.get_user", return_value=mock_user),
            patch("opi.middleware.authorization.get_user_service", return_value=mock_user_service),
            patch("opi.core.auth_decorators.get_current_user", return_value=mock_user),
            patch("opi.web.router.get_current_user", return_value=mock_user),
            patch("opi.web.router.is_user_authorized_for_project", return_value=True),
            patch("opi.web.router.get_project_store", return_value=store),
        ):
            client = TestClient(create_app())
            response = client.get("/projects/demo/deployments/prod/domain-settings")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["domain_format"] == "deployment-subdomain"
        assert body["domain_mode"] == "custom"
        assert body["subdomain"] == "shop"


class TestFieldExamples:
    """``Field(..., example=...)`` is not a parameter of ``Field``.

    Pydantic v2 swept unknown keyword arguments into ``json_schema_extra`` with a
    deprecation warning and will stop accepting them in v3; the API models did this 64
    times. ``examples`` is the real parameter, and it is what OpenAPI 3.1 expects.
    """

    def test_no_field_call_passes_the_singular_example(self) -> None:
        offenders: list[str] = []
        for path in Path("opi").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if any(kw.arg == "example" for kw in node.keywords):
                    offenders.append(f"{path}:{node.lineno}")

        assert offenders == []

    def test_examples_reach_the_openapi_schema(self) -> None:
        from opi.api.router import ComponentReference

        schema = ComponentReference.model_json_schema()

        assert schema["properties"]["reference"]["examples"] == ["frontend"]
        assert schema["properties"]["image"]["examples"] == ["nginx:1.21"]

    def test_defining_the_models_raises_no_pydantic_deprecation(self) -> None:
        """Re-executing the module is the only way to see a class-definition warning."""
        import importlib
        import warnings

        import opi.api.router

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            importlib.reload(opi.api.router)

        assert [str(w.message) for w in caught if "json_schema_extra" in str(w.message)] == []


class TestPrometheusConnectorConstruction:
    """The package root hands its classes out through a module-level ``__getattr__``.

    A type checker resolves that to the union of everything the shim can return, and
    then rejects the real ``PrometheusConnect(url=..., disable_ssl=...)`` arguments.
    Importing from the submodule resolves to the class itself -- same object at runtime.
    """

    def test_imports_the_class_not_the_shim(self) -> None:
        import prometheus_api_client
        from opi.connectors import prometheus
        from prometheus_api_client.prometheus_connect import PrometheusConnect

        assert prometheus.PrometheusConnect is PrometheusConnect
        assert prometheus.PrometheusConnect is prometheus_api_client.PrometheusConnect
