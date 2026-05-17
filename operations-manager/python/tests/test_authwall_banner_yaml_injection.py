"""Regression test for the authorization-wall banner YAML injection (Vuln 2).

services/authorization-wall/config/banner is free-form user text from the
project file. It was rendered as an unquoted YAML scalar inside the sidecar
container's `args:` list:

    - --banner={{ authorization_wall.banner }}

A multi-line banner with crafted indentation could break out of the args list
and inject sibling keys into the container spec (e.g. a privileged,
runAsUser: 0 securityContext), which is then committed and applied by ArgoCD.

This test renders the real sidecar template with an injection payload and
parses the result as YAML.

Red (vulnerable template, unquoted scalar): the parsed container gains an
attacker-controlled `securityContext.privileged: true` (or the YAML is
restructured), so the assertion fails.
Green (fixed template, value rendered via | tojson): the payload stays a
single literal arg string, the container securityContext is unchanged.
"""

import os

from opi.generation.manifests import render_template
from ruamel.yaml import YAML

MANIFESTS_DIR = os.path.join(os.path.dirname(__file__), "..", "manifests")

# A banner value that, rendered unquoted at 12-space indent inside `args:`,
# closes the list item and injects a privileged securityContext as a sibling
# key of the authorization-wall container mapping (10-space indent).
INJECTION_BANNER = (
    "pwn\n"
    "          securityContext:\n"
    "            privileged: true\n"
    "            runAsUser: 0\n"
    "            allowPrivilegeEscalation: true"
)


def _render_container() -> str:
    return render_template(
        "sidecar-authorization-wall.yaml.jinja",
        {
            "section": "container",
            "application_port": 8080,
            "hostname": "app.example.com",
            "name": "myapp",
            "authorization_wall": {
                "issuer_url": "https://keycloak.example.com/realms/r",
                "client_id": "myapp",
                "banner": INJECTION_BANNER,
                "keycloak_secret_name": "myapp-oidc",
                "cookie_secret_name": "myapp-cookie",
            },
        },
    )


def test_banner_cannot_inject_security_context() -> None:
    rendered = _render_container()

    # The sidecar template emits a list item (the container) under an 8-space
    # indent. Wrap it so it parses as a standalone document.
    doc = YAML().load("containers:\n" + rendered)
    containers = doc["containers"]
    assert len(containers) == 1, f"banner injection altered container count: {containers}"

    container = containers[0]
    assert container["name"] == "authorization-wall"

    # The only securityContext must be the hardened one defined in the template.
    sec = container.get("securityContext", {})
    assert sec.get("privileged") is not True, "banner injected privileged securityContext"
    assert sec.get("runAsUser") != 0, "banner injected runAsUser: 0"
    assert sec.get("allowPrivilegeEscalation") is not True, "banner injected allowPrivilegeEscalation"
    assert sec.get("runAsNonRoot") is True, "hardened securityContext was overwritten by injection"

    # The banner must survive intact as exactly one literal arg.
    banner_args = [a for a in container["args"] if str(a).startswith("--banner=")]
    assert len(banner_args) == 1
    assert banner_args[0] == f"--banner={INJECTION_BANNER}"


def test_rendered_template_is_valid_single_structure() -> None:
    """The injection must not produce extra top-level YAML documents/keys either."""
    rendered = _render_container()
    docs = list(YAML().load_all("containers:\n" + rendered))
    assert len(docs) == 1, "banner injection produced extra YAML documents"
