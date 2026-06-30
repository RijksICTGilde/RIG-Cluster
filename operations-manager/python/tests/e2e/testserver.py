"""
Test server for UI development and E2E testing.

Starts the real FastAPI app with mocked external services and seeded
project data from local YAML files. No database, Git, Keycloak, or
Kubernetes cluster required.

Usage:
    # Interactive development (with hot-reload):
    cd operations-manager/python
    uv run python -m tests.e2e.testserver

    # From pytest (via app_server fixture):
    Used automatically by tests/e2e/conftest.py
"""

import logging
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import yaml

logger = logging.getLogger(__name__)

SECRET_KEY = "e2e-test-secret-key-padded-to-32-chars-minimum"

# Fixed test AGE keypair for E2E testing (DO NOT use in production)
TEST_AGE_PUBLIC_KEY = "age10uegg2n4sxnsmpd00xjqh8e80hhrs9983yhy673gp8k0aevn4dtsn9d8xj"
TEST_AGE_PRIVATE_KEY = "AGE-SECRET-KEY-1P9VAE6J5J7FK0LF2TH0FG7HMNS8XC9T4GTJQNGWRJAS40DYAGULQTCCAMK"

TEST_USER_EMAIL = "test@example.com"

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "projects"


class InMemoryUserAdminService:
    """In-memory stub for UserAdminService used in E2E tests.

    Replaces the database-backed service so that user admin pages can be
    tested without a running PostgreSQL instance.
    """

    def __init__(self) -> None:
        import uuid
        from datetime import UTC, datetime

        self._users: dict[str, dict] = {}
        # Seed a couple of users so the list page isn't empty
        for email, name in [
            ("jan@example.nl", "Jan de Vries"),
            ("maria@example.nl", "Maria Jansen"),
        ]:
            uid = str(uuid.uuid4())
            now = datetime.now(UTC).isoformat()
            self._users[uid] = {
                "id": uid,
                "email": email,
                "full_name": name,
                "created_at": now,
                "updated_at": now,
            }

    async def list_users(self) -> list[dict]:
        return sorted(self._users.values(), key=lambda u: u["full_name"])

    async def get_user(self, user_id: str) -> dict | None:
        return self._users.get(user_id)

    async def get_user_by_email(self, email: str) -> dict | None:
        for u in self._users.values():
            if u["email"] == email:
                return u
        return None

    async def create_user(self, email: str, full_name: str) -> dict:
        import uuid
        from datetime import UTC, datetime

        # Check uniqueness (raise same error as asyncpg would)
        for u in self._users.values():
            if u["email"] == email:
                from asyncpg import UniqueViolationError

                raise UniqueViolationError
        uid = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        user = {"id": uid, "email": email, "full_name": full_name, "created_at": now, "updated_at": now}
        self._users[uid] = user
        return user

    async def update_user(self, user_id: str, email: str, full_name: str) -> dict | None:
        from datetime import UTC, datetime

        if user_id not in self._users:
            return None
        # Check uniqueness against other users
        for uid, u in self._users.items():
            if u["email"] == email and uid != user_id:
                from asyncpg import UniqueViolationError

                raise UniqueViolationError
        self._users[user_id]["email"] = email
        self._users[user_id]["full_name"] = full_name
        self._users[user_id]["updated_at"] = datetime.now(UTC).isoformat()
        return self._users[user_id]

    async def delete_user(self, user_id: str) -> bool:
        return self._users.pop(user_id, None) is not None


# Singleton so the test server and tests share the same instance
_in_memory_user_service: InMemoryUserAdminService | None = None


def get_in_memory_user_service() -> InMemoryUserAdminService:
    """Get (or create) the shared in-memory user admin service."""
    global _in_memory_user_service
    if _in_memory_user_service is None:
        _in_memory_user_service = InMemoryUserAdminService()
    return _in_memory_user_service


def _mock_get_service() -> InMemoryUserAdminService:
    """Drop-in replacement for router_user_admin._get_service."""
    return get_in_memory_user_service()


def _load_fixture_projects() -> list[dict]:
    """Load all YAML project files from the fixtures directory."""
    projects = []
    if not FIXTURE_DIR.exists():
        logger.warning("Fixture directory does not exist: %s", FIXTURE_DIR)
        return projects

    for yaml_file in sorted(FIXTURE_DIR.glob("*.yaml")):
        with open(yaml_file) as f:
            data = yaml.safe_load(f)
        if data and isinstance(data, dict) and "name" in data:
            projects.append(data)
            logger.info("Loaded fixture project: %s from %s", data["name"], yaml_file.name)
    return projects


def _seed_projects(projects: list[dict]) -> None:
    """Register fixture projects into ProjectService and UserService."""
    from opi.services.project_service import get_project_service, initialize_project_service
    from opi.services.user_service import get_user_service

    initialize_project_service()
    project_service = get_project_service()
    user_service = get_user_service()

    # Add test user to allowlist and grant admin access
    user_service.add_allowed_emails([TEST_USER_EMAIL])
    project_service.add_admin_emails([TEST_USER_EMAIL])

    for project_data in projects:
        project_name = project_data["name"]
        config = project_data.get("config", {})
        api_key = config.get("api-key", f"test-key-{project_name}")
        filename = f"{project_name}.yaml"
        users = project_data.get("users", [])

        project_service.register(project_name, str(api_key), filename, users, project_data)

        # Add project user emails to allowlist
        project_emails = [u.get("email") for u in users if u.get("email")]
        if project_emails:
            user_service.add_allowed_emails(project_emails)

    logger.info("Seeded %d fixture projects", len(projects))


def create_test_app():
    """Create the FastAPI app with mocked externals and seeded test data.

    Returns a tuple of (app, patches_context) where patches_context is a
    contextmanager that must remain active while the app is running.
    """
    import contextlib

    @contextlib.contextmanager
    def patched_app():
        with (
            patch("opi.core.startup.run_startup_tasks", new_callable=AsyncMock),
            patch("opi.core.config.settings.SECRET_KEY", SECRET_KEY),
            patch("opi.core.config.settings.ENABLE_GIT_MONITOR", False),
            patch("opi.core.startup.ensure_projects_fresh", new_callable=AsyncMock),
            patch(
                "opi.connectors.subdomain.SubdomainConnector.get_by_subdomain",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("opi.core.config.settings.SOPS_AGE_PRIVATE_KEY", TEST_AGE_PRIVATE_KEY),
            patch(
                "opi.connectors.prometheus.get_metrics_connector",
                return_value=SimpleNamespace(is_connected=False),
            ),
            patch(
                "opi.connectors.argo.create_argo_connector",
                return_value=MagicMock(auth_token=None),
            ),
            patch("opi.handlers.project_file_handler.save_project_file"),
            patch("opi.web.router_user_admin._get_service", _mock_get_service),
            patch(
                "opi.manager.backup.BackupManager",
                return_value=MagicMock(
                    list_snapshots=AsyncMock(return_value=[]),
                ),
            ),
        ):
            # Mark all readiness services as ready
            import opi.core.readiness as readiness_module

            readiness_module._state = None
            state = readiness_module.get_readiness_state()
            state.database.mark_ready()
            state.keycloak.mark_ready()
            state.oauth.mark_ready()
            state.projects.mark_ready()

            from opi.server import create_app

            app = create_app()

            # Seed fixture projects
            projects = _load_fixture_projects()
            _seed_projects(projects)

            yield app

    return patched_app


def run_standalone() -> None:
    """Run the test server standalone for interactive UI development."""
    import uvicorn

    port = int(os.environ.get("TEST_SERVER_PORT", "8111"))

    # Disable OIDC for standalone mode - no login needed
    os.environ.setdefault("OIDC_DISABLED", "true")

    print(f"""
========================================
  UI Test Server
========================================
  URL:     http://127.0.0.1:{port}
  Wizard:  http://127.0.0.1:{port}/forms/wizard/start
  Reload:  watching opi/ directory
  Auth:    disabled (OIDC_DISABLED=true)
========================================
""")

    ctx = create_test_app()
    with ctx() as app:
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="info",
            reload=False,  # reload doesn't work well with patched app
        )
        server = uvicorn.Server(config)
        server.run()


if __name__ == "__main__":
    run_standalone()
