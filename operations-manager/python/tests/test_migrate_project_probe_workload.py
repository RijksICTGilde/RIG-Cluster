"""Tests for the --probe-image rewrite in scripts/migrate_project_to_sandbox.py (RC-19).

The upgrade-safety test swaps every component workload for the e2e-allservices probe
so /status verifies each service binding. Two things must be rewritten together (image
and inbound port) and nothing else about the project's declared bindings may change.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# The migration helpers live in a top-level scripts/ dir, imported here directly.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from migrate_project_to_sandbox import (  # noqa: E402
    DEFAULT_PROBE_IMAGE,
    DEFAULT_PROBE_PORT,
    SANDBOX_ENV_PLACEHOLDER,
    _blank_env_values,
    apply_probe_workload,
)


@pytest.fixture
def project() -> dict:
    return {
        "name": "moza",
        "components": [
            {
                "name": "api",
                "ports": {"inbound": [8000, 9000], "outbound": [80, 443, 5432]},
                "path": [{"match": "/api"}],
                "services": ["publish-on-web", "keycloak", "namespace-postgresql-database"],
            },
            {
                "name": "frontend",
                "ports": {"inbound": [3000], "outbound": [80, 443]},
                "services": ["publish-on-web"],
            },
        ],
        "deployments": [
            {
                "name": "deployment-1",
                "components": [
                    {"reference": "api", "image": "ghcr.io/x/api:1", "registry": "github-registry"},
                    {
                        "reference": "frontend",
                        "image": "ghcr.io/x/frontend:1",
                        "imagePullPolicy": "Always",
                    },
                ],
            }
        ],
    }


def test_images_replaced_and_registry_dropped(project: dict) -> None:
    apply_probe_workload(project, DEFAULT_PROBE_IMAGE, DEFAULT_PROBE_PORT)

    for comp in project["deployments"][0]["components"]:
        assert comp["image"] == DEFAULT_PROBE_IMAGE
        # The probe is public: a lingering registry / pull-policy reference would break the pull.
        assert "registry" not in comp
        assert "imagePullPolicy" not in comp


def test_inbound_ports_moved_to_probe_port(project: dict) -> None:
    apply_probe_workload(project, DEFAULT_PROBE_IMAGE, DEFAULT_PROBE_PORT)

    for comp in project["components"]:
        assert comp["ports"]["inbound"] == [DEFAULT_PROBE_PORT]


def test_bindings_and_outbound_untouched(project: dict) -> None:
    """The whole point is to exercise the project's declared services, so service

    bindings, outbound ports and path routing must survive the workload swap.
    """
    apply_probe_workload(project, DEFAULT_PROBE_IMAGE, DEFAULT_PROBE_PORT)

    api = project["components"][0]
    assert api["services"] == ["publish-on-web", "keycloak", "namespace-postgresql-database"]
    assert api["ports"]["outbound"] == [80, 443, 5432]
    assert api["path"] == [{"match": "/api"}]


def test_custom_port_is_honoured(project: dict) -> None:
    apply_probe_workload(project, "my/probe:tag", 9999)

    assert project["components"][0]["ports"]["inbound"] == [9999]
    assert project["deployments"][0]["components"][0]["image"] == "my/probe:tag"


def test_component_without_ports_gets_inbound(project: dict) -> None:
    project["components"].append({"name": "worker", "services": []})
    apply_probe_workload(project, DEFAULT_PROBE_IMAGE, DEFAULT_PROBE_PORT)

    worker = project["components"][-1]
    assert worker["ports"]["inbound"] == [DEFAULT_PROBE_PORT]


# ---------------------------------------------------------------------------
# Blanking user-env-var values before a production project reaches the sandbox
# ---------------------------------------------------------------------------


class TestBlankEnvValues:
    """Converting a production project re-encrypts its key with the SANDBOX key.

    Everything encrypted with the project's own key therefore becomes readable by anyone
    holding the sandbox key, and user-env-vars is where teams put API tokens and
    passwords. Values are blanked; keys, comments and layout stay so the upgrade test
    still proves the block survives migration and reaches the manifest.
    """

    def test_dotenv_format_keeps_keys_and_blanks_values(self):
        out = _blank_env_values("DJANGO_SECRET_KEY=s3cr3t\nDEBUG=false\n")

        assert "s3cr3t" not in out
        assert f"DJANGO_SECRET_KEY={SANDBOX_ENV_PLACEHOLDER}" in out
        assert f"DEBUG={SANDBOX_ENV_PLACEHOLDER}" in out

    def test_yaml_colon_format_is_handled_too(self):
        """Both formats occur in real files; only handling ``=`` shipped a real secret.

        The first version of this passed every ``KEY: value`` line through untouched,
        which left openp-4pw's production SECRET_KEY_BASE in the converted output.
        """
        out = _blank_env_values('SECRET_KEY_BASE: "abc123"\nOPENPROJECT_HTTPS: "true"\n')

        assert "abc123" not in out
        assert f"SECRET_KEY_BASE: {SANDBOX_ENV_PLACEHOLDER}" in out
        assert f"OPENPROJECT_HTTPS: {SANDBOX_ENV_PLACEHOLDER}" in out

    def test_a_value_holding_the_other_separator_is_cut_at_the_first_one(self):
        # A URL value contains ':' while the line's separator is '='; cutting at the
        # wrong one would leave the host in the output.
        out = _blank_env_values("API_URL=https://example.com:8443/path\n")

        assert "example.com" not in out
        assert out.strip() == f"API_URL={SANDBOX_ENV_PLACEHOLDER}"

    def test_comments_and_blank_lines_survive(self):
        out = _blank_env_values("# a comment\n\nKEY=value\n")

        assert "# a comment" in out
        assert "\n\n" in out

    def test_an_unparsable_line_raises_instead_of_passing_through(self):
        """Silence is the dangerous failure here: an unrecognised line is a value.

        Passing it through unchanged is exactly how the SECRET_KEY_BASE leak happened,
        so a line without a separator must stop the run rather than reach the sandbox.
        """
        with pytest.raises(ValueError, match="separator"):
            _blank_env_values("just-a-bare-secret-value\n")
