"""A project-realm template may only reference variables the project context provides.

RC-51 put the zad-cli client into the shared sso-only/sso-support templates and defined
``cli_client_id`` only in the platform bootstrap context. Nothing failed until a real realm
was rendered on a cluster; then every project on those templates stopped processing with
"Variable path not found: 'cli_client_id'". The templates and the context that fills them
were never compared to each other, which is what these tests do.

Derived from the files on disk, so a new template or a new ``{{ ... }}`` is covered as soon
as it is added.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml
from opi.handlers.keycloak_yaml_handler import KeycloakYamlHandler
from opi.manager.keycloak_manager import build_project_realm_context

TEMPLATE_DIR = Path(__file__).parent.parent / "opi" / "configs" / "keycloak"

_VARIABLE_PATTERN = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)")

#: Default forEach loop variable when a template does not name one with `as:`.
DEFAULT_LOOP_VAR = "item"

#: Added to the context later in the same code path, only when the project has ingress
#: hosts, so build_project_realm_context() cannot carry it.
OPTIONAL_CONTEXT_KEYS = frozenset({"frontend_redirect_uris"})

#: Names a template binds itself with `as:` (forEach loop vars and captured outputs).
_BINDING_PATTERN = re.compile(r"^\s*-?\s*as:\s*([a-zA-Z_][a-zA-Z0-9_]*)", re.MULTILINE)

#: Templates a PROJECT realm can be created from: everything except the bootstrap configs
#: and the operations-manager's own blueprint, which render against other contexts.
PLATFORM_ONLY = {"bootstrap.yaml", "bootstrap-local.yaml", "bootstrap-sandbox.yaml", "operations-manager-realm.yaml"}
PROJECT_TEMPLATES = sorted(p for p in TEMPLATE_DIR.glob("*.yaml") if p.name not in PLATFORM_ONLY)


def _referenced_variables(text: str) -> set[str]:
    bound = set(_BINDING_PATTERN.findall(text)) | {DEFAULT_LOOP_VAR}
    return set(_VARIABLE_PATTERN.findall(text)) - bound


def _locally_declared(path: Path) -> set[str]:
    content = yaml.safe_load(path.read_text())
    if not isinstance(content, dict):
        return set()
    variables = content.get("variables")
    return set(variables) if isinstance(variables, dict) else set()


def _project_context_keys() -> set[str]:
    return set(
        build_project_realm_context(
            project_name="demo",
            cluster="sandboxed-local",
            keycloak_url="https://keycloak.example",
            realm_name="demo-sandboxed-local",
            platform_client_id="operations-manager-sandboxed-local-platform",
            operations_manager_domain="zad.example",
            account_link=None,
        )
    )


def test_there_are_project_templates_to_check() -> None:
    """Guard the guard: an empty glob would make the parametrized tests vacuous."""
    assert PROJECT_TEMPLATES, f"no project templates found in {TEMPLATE_DIR}"


@pytest.mark.parametrize("template", PROJECT_TEMPLATES, ids=lambda p: p.name)
def test_project_template_only_uses_variables_the_project_context_provides(template: Path) -> None:
    missing = (
        _referenced_variables(template.read_text())
        - _project_context_keys()
        - _locally_declared(template)
        - OPTIONAL_CONTEXT_KEYS
    )

    assert not missing, (
        f"{template.name} references {sorted(missing)}, which build_project_realm_context() does "
        f"not provide, so every project on this template fails to process with "
        f'"Variable path not found". Add them there, or move the block to a platform-only blueprint.'
    )


def test_shared_templates_do_not_carry_the_cli_client() -> None:
    """The CLI client authenticates project CREATION, so a project realm must not get one."""
    for name in ("sso-only.yaml", "sso-support.yaml"):
        text = (TEMPLATE_DIR / name).read_text()
        assert "cli_client_id" not in text, f"{name} still declares the zad-cli client"


def test_operations_manager_blueprint_extends_a_shared_one_and_adds_the_cli_client() -> None:
    """The OPI realm is the shared template plus the CLI client, not a copy of it."""
    handler = KeycloakYamlHandler(keycloak_connector=MagicMock())
    resolved = handler._load_yaml(TEMPLATE_DIR / "operations-manager-realm.yaml")

    client_ids = [c.get("clientId") for c in resolved["clients"] if isinstance(c, dict)]
    assert "{{ cli_client_id }}" in client_ids

    # It inherited the base rather than redefining it.
    base = handler._load_yaml(TEMPLATE_DIR / "sso-support.yaml")
    assert resolved["realms"] == base["realms"]
    assert len(resolved["clients"]) == len(base["clients"]) + 1


def test_extends_appends_a_new_entry_and_overrides_a_matching_one(tmp_path: Path) -> None:
    (tmp_path / "base.yaml").write_text("realms:\n  - realm: base\n    enabled: false\nclients:\n  - clientId: a\n")
    (tmp_path / "child.yaml").write_text(
        "extends: base\nrealms:\n  - realm: base\n    enabled: true\nclients:\n  - clientId: b\n"
    )

    resolved = KeycloakYamlHandler(keycloak_connector=MagicMock())._load_yaml(tmp_path / "child.yaml")

    # A new clientId is added next to the inherited one...
    assert [c["clientId"] for c in resolved["clients"]] == ["a", "b"]
    # ...while a realm of the same name is overridden rather than duplicated.
    assert len(resolved["realms"]) == 1
    assert resolved["realms"][0]["enabled"] is True
    assert "extends" not in resolved


def test_circular_extends_is_refused(tmp_path: Path) -> None:
    (tmp_path / "a.yaml").write_text("extends: b\n")
    (tmp_path / "b.yaml").write_text("extends: a\n")

    with pytest.raises(ValueError, match="Circular 'extends'"):
        KeycloakYamlHandler(keycloak_connector=MagicMock())._load_yaml(tmp_path / "a.yaml")
