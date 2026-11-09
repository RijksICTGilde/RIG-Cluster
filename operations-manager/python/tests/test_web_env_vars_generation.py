"""
Tests for aliases.generate_web_env_vars_from_services.

Verifies that the publish-on-web service emits BOTH:
- PUBLIC_HOST  : full URL with scheme (e.g. https://app.example.com)
- PUBLIC_HOSTNAME : bare hostname without scheme (e.g. app.example.com)

The hostname-only variant is needed for apps (OpenProject etc.) whose
host-name config fields reject a URL with scheme.
"""

import pytest
from opi.manager.project.aliases import generate_web_env_vars_from_services


class TestGenerateWebEnvVarsFromServices:
    """Direct unit tests for generate_web_env_vars_from_services."""

    def test_emits_both_public_host_and_public_hostname_https(self):
        env_vars = generate_web_env_vars_from_services("app.example.com", use_https=True)
        assert env_vars["PUBLIC_HOST"] == "https://app.example.com"
        assert env_vars["PUBLIC_HOSTNAME"] == "app.example.com"

    def test_emits_both_public_host_and_public_hostname_http(self):
        env_vars = generate_web_env_vars_from_services("app.example.com", use_https=False)
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
        env_vars = generate_web_env_vars_from_services(hostname, use_https=True)
        assert env_vars["PUBLIC_HOST"] == expected_host
        assert env_vars["PUBLIC_HOSTNAME"] == expected_hostname

    def test_localhost_http_with_port(self):
        """A common dev pattern: http://localhost:3000."""
        env_vars = generate_web_env_vars_from_services("localhost:3000", use_https=False)
        assert env_vars["PUBLIC_HOST"] == "http://localhost:3000"
        assert env_vars["PUBLIC_HOSTNAME"] == "localhost:3000"

    def test_public_hostname_never_contains_scheme(self):
        for hostname in ("a.b.c", "deep.sub.example.com:8443", "host"):
            env_vars = generate_web_env_vars_from_services(hostname, use_https=True)
            assert "://" not in env_vars["PUBLIC_HOSTNAME"], (
                f"PUBLIC_HOSTNAME must not contain '://' for input {hostname!r}, got {env_vars['PUBLIC_HOSTNAME']!r}"
            )
