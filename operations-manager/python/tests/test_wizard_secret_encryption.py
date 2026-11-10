"""A create-wizard step never writes a secret to disk in plain text.

The wizard keeps its state in a JSON file under ``TEMP_DIR``. Until RC-463 the project AGE
key pair was generated at submit, so every secret typed before that (a component's
environment variables, a registry token) sat in that file readable, for as long as the
session lived. The key pair now exists from the first step and the converters encrypt
straight away, so the file only ever carries ciphertext.

Driven over HTTP against the real wizard router, because that is where the order of
"generate the key" and "store the step" is decided.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from opi.core.config import settings
from opi.utils.age import decrypt_password_smart_sync, generate_age_key_pair, unwrap_project_key_sync
from opi.utils.yaml_util import load_yaml_from_string
from starlette.middleware.sessions import SessionMiddleware

if TYPE_CHECKING:
    from pathlib import Path

ENV_VAR_VALUE = "env-var-value-only-this-test-uses"
REGISTRY_TOKEN = "registry-token-only-this-test-uses"
REPO_PASSWORD = "plain:repo-password-only-this-test-uses"

SECTION_PAYLOADS: list[tuple[str, dict[str, Any]]] = [
    ("identity", {"display-name": "Secret Wizard", "description": "beschrijving", "clusters": "local"}),
    ("services", {"services": ["image-registries"]}),
    (
        "image-registries-config",
        {
            "services": {
                "image-registries": {
                    "config": {
                        "registries": [
                            {"name": "eigen", "upstream": "ghcr.io", "username": "u", "password": REGISTRY_TOKEN}
                        ]
                    }
                }
            }
        },
    ),
    ("team", {"users": [{"email": "dev@example.com", "roles": ["admin"]}]}),
    (
        "components",
        {
            "components": [
                {
                    "name": "frontend",
                    "path": [{"match": "/"}],
                    "ports": {"inbound": ["8080"], "outbound": ["80", "443"]},
                    "user-env-vars": f"SECRET_KEY={ENV_VAR_VALUE}",
                }
            ]
        },
    ),
    ("deployment", {"deployments": [{"name": "productie"}]}),
    (
        "domains",
        {
            "deployments": [
                {
                    "services": {
                        "publish-on-web": {
                            "config": {
                                "base-domain": "sandbox.rijksapp.dev",
                                "domain-format": "component-deployment-project",
                            }
                        }
                    }
                }
            ]
        },
    ),
]


class Wizard:
    """A create-wizard session: one HTTP client plus the state file it writes."""

    def __init__(self, client: TestClient, store: Path) -> None:
        self._client = client
        self._store = store
        assert client.get("/forms/wizard/create-project").status_code == 200

    def step(self, section_id: str, payload: dict[str, Any], goto: str = "next") -> str:
        response = self._client.post(f"/forms/wizard/create-project/step/{section_id}", json={**payload, "_goto": goto})
        assert response.status_code == 200, f"step {section_id} answered {response.status_code}"
        return response.text

    def walk(self) -> None:
        for section_id, payload in SECTION_PAYLOADS:
            self.step(section_id, payload)

    def submit(self) -> dict[str, Any]:
        response = self._client.post("/forms/wizard/create-project/submit", json={})
        assert response.status_code == 200, response.text
        return dict(response.headers)

    @property
    def state(self) -> str:
        """The raw state file, so an assertion sees exactly what is on disk."""
        files = list(self._store.glob("*.json"))
        assert len(files) == 1, f"expected one wizard state file, found {len(files)}"
        return files[0].read_text()

    @property
    def state_data(self) -> dict[str, Any]:
        return json.loads(self.state)


@pytest.fixture
def platform_key(monkeypatch: pytest.MonkeyPatch) -> str:
    """A throwaway platform key, the one a project key is wrapped with."""
    private_key, public_key = generate_age_key_pair()
    monkeypatch.setattr(settings, "SOPS_AGE_PRIVATE_KEY", private_key)
    monkeypatch.setattr(settings, "SOPS_AGE_PUBLIC_KEY", public_key)
    monkeypatch.setattr(settings, "PROJECT_KEK_PRIVATE_KEY", "")
    monkeypatch.setattr(settings, "PROJECT_KEK_PUBLIC_KEY", "")
    return private_key


@pytest.fixture
def wizard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform_key: str) -> Wizard:
    from opi.forms.wizard import session as wizard_session
    from opi.web.router_wizard import wizard_router

    monkeypatch.setattr(settings, "TEMP_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "PROJECT_REPO_PASSWORD", REPO_PASSWORD)
    monkeypatch.setattr(wizard_session, "_STORE_DIR", None)

    app = FastAPI()

    @app.middleware("http")
    async def seed_request_state(request: Request, call_next: Any) -> Any:
        # The two things the wizard templates and routes read off the request; the
        # middleware that normally fills them is not part of this app.
        request.state.user = {"email": "dev@example.com", "roles": ["admin"]}
        request.state.csrf_token = "test-csrf-token"
        return await call_next(request)

    app.include_router(wizard_router)
    app.add_middleware(SessionMiddleware, secret_key="test")
    return Wizard(TestClient(app), tmp_path / "wizard-sessions")


def test_the_state_file_carries_no_plain_secret_after_any_step(wizard: Wizard) -> None:
    for section_id, payload in SECTION_PAYLOADS:
        wizard.step(section_id, payload)
        state = wizard.state
        assert ENV_VAR_VALUE not in state, f"env var in plain text after step {section_id}"
        assert REGISTRY_TOKEN not in state, f"registry token in plain text after step {section_id}"
        # The base layer carries it too, and the state file holds that layer as it is.
        assert REPO_PASSWORD not in state, f"repository password in plain text after step {section_id}"


def test_the_state_file_carries_the_secrets_as_age_blocks(wizard: Wizard) -> None:
    wizard.walk()
    state = wizard.state_data
    component = state["step_data"]["components"]["components"][0]
    assert "BEGIN AGE ENCRYPTED FILE" in component["user-env-vars"]
    registry = state["step_data"]["image-registries-config"]["_services-config"][0]["config"]["registries"][0]
    assert "BEGIN AGE ENCRYPTED FILE" in registry["password"]


def test_stepping_back_shows_the_value_and_keeps_the_same_ciphertext(wizard: Wizard) -> None:
    wizard.walk()
    stored = wizard.state_data["step_data"]["components"]["components"][0]["user-env-vars"]

    html = wizard.step("domains", SECTION_PAYLOADS[-1][1], goto="components")

    assert ENV_VAR_VALUE in html, "the step no longer shows the value the user typed"
    assert wizard.state_data["step_data"]["components"]["components"][0]["user-env-vars"] == stored


def test_the_created_project_opens_with_the_key_the_wizard_made(
    wizard: Wizard, monkeypatch: pytest.MonkeyPatch
) -> None:
    import opi.http.task_helpers as task_helpers

    created = AsyncMock(return_value={"task_id": "task-1"})
    monkeypatch.setattr(task_helpers, "create_async_task", created)

    wizard.walk()
    wizard.submit()

    payload = created.await_args.kwargs["payload"]
    project = load_yaml_from_string(payload["yaml_content"])
    private_key = unwrap_project_key_sync(project["config"]["age-private-key"])

    component = project["components"][0]
    assert decrypt_password_smart_sync(component["user-env-vars"], private_key) == f"SECRET_KEY={ENV_VAR_VALUE}"
    registries = next(
        service["config"]["registries"]
        for service in project["services"]
        if isinstance(service, dict) and service.get("name") == "image-registries"
    )
    assert decrypt_password_smart_sync(registries[0]["password"], private_key) == REGISTRY_TOKEN


def test_the_created_project_carries_no_plain_value(wizard: Wizard, monkeypatch: pytest.MonkeyPatch) -> None:
    """Schema version 2.18: a project file may not carry a ``plain:`` secret at all."""
    import opi.http.task_helpers as task_helpers

    created = AsyncMock(return_value={"task_id": "task-1"})
    monkeypatch.setattr(task_helpers, "create_async_task", created)

    wizard.walk()
    wizard.submit()

    yaml_content = created.await_args.kwargs["payload"]["yaml_content"]
    assert "plain:" not in yaml_content
    assert REPO_PASSWORD not in yaml_content
