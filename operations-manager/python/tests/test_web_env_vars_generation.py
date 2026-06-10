"""
Tests for ProjectManager._generate_web_env_vars_from_services.

Verifies that the publish-on-web service emits BOTH:
- PUBLIC_HOST  : full URL with scheme (e.g. https://app.example.com)
- PUBLIC_HOSTNAME : bare hostname without scheme (e.g. app.example.com)

The hostname-only variant is needed for apps (OpenProject etc.) whose
host-name config fields reject a URL with scheme.
"""

from unittest.mock import MagicMock, patch

import pytest


def _make_project_manager():
    """Construct a ProjectManager with all heavy collaborators mocked out."""
    with (
        patch("opi.manager.project_manager.KubectlConnector"),
        patch("opi.handlers.sops.SopsHandler"),
        patch("opi.generation.manifests.ManifestGenerator"),
        patch("opi.manager.argo_manager.ArgoManager", return_value=MagicMock()),
        patch("opi.manager.bootstrap_manager.BootstrapManager", return_value=MagicMock()),
        patch("opi.manager.delete_project_manager.DeleteProjectManager", return_value=MagicMock()),
        patch("opi.manager.keycloak_manager.KeycloakManager", return_value=MagicMock()),
        patch("opi.manager.minio_manager.MinioManager", return_value=MagicMock()),
        patch("opi.manager.redis_manager.RedisManager", return_value=MagicMock()),
        patch("opi.manager.pvc_manager.PVCManager", return_value=MagicMock()),
    ):
        from opi.manager.project_manager import ProjectManager

        return ProjectManager()


class TestGenerateWebEnvVarsFromServices:
    """Direct unit tests for _generate_web_env_vars_from_services."""

    def test_emits_both_public_host_and_public_hostname_https(self):
        pm = _make_project_manager()
        env_vars = pm._generate_web_env_vars_from_services("app.example.com", use_https=True)
        assert env_vars["PUBLIC_HOST"] == "https://app.example.com"
        assert env_vars["PUBLIC_HOSTNAME"] == "app.example.com"

    def test_emits_both_public_host_and_public_hostname_http(self):
        pm = _make_project_manager()
        env_vars = pm._generate_web_env_vars_from_services("app.example.com", use_https=False)
        assert env_vars["PUBLIC_HOST"] == "http://app.example.com"
        assert env_vars["PUBLIC_HOSTNAME"] == "app.example.com"

    @pytest.mark.parametrize(
        ("hostname", "expected_host", "expected_hostname"),
        [
            ("example.com", "https://example.com", "example.com"),
            ("sub.example.com", "https://sub.example.com", "sub.example.com"),
            (
                "sub.example.com:8443",
                "https://sub.example.com:8443",
                "sub.example.com:8443",
            ),
            ("localhost", "https://localhost", "localhost"),
            (
                "productie-openp-7lh.sandbox.rijksapp.dev",
                "https://productie-openp-7lh.sandbox.rijksapp.dev",
                "productie-openp-7lh.sandbox.rijksapp.dev",
            ),
        ],
    )
    def test_hostname_variants(self, hostname, expected_host, expected_hostname):
        pm = _make_project_manager()
        env_vars = pm._generate_web_env_vars_from_services(hostname, use_https=True)
        assert env_vars["PUBLIC_HOST"] == expected_host
        assert env_vars["PUBLIC_HOSTNAME"] == expected_hostname

    def test_localhost_http_with_port(self):
        """A common dev pattern: http://localhost:3000."""
        pm = _make_project_manager()
        env_vars = pm._generate_web_env_vars_from_services("localhost:3000", use_https=False)
        assert env_vars["PUBLIC_HOST"] == "http://localhost:3000"
        assert env_vars["PUBLIC_HOSTNAME"] == "localhost:3000"

    def test_public_hostname_never_contains_scheme(self):
        pm = _make_project_manager()
        for hostname in ("a.b.c", "deep.sub.example.com:8443", "host"):
            env_vars = pm._generate_web_env_vars_from_services(hostname, use_https=True)
            assert "://" not in env_vars["PUBLIC_HOSTNAME"], (
                f"PUBLIC_HOSTNAME must not contain '://' for input {hostname!r}, got {env_vars['PUBLIC_HOSTNAME']!r}"
            )
