"""
Forgejo verification helper for sandbox E2E tests.

The Operations Manager commits every project definition to the `zad-projects`
git repo hosted in Forgejo. That committed file is the primary "did it actually
work?" signal for the sandbox flows: after a UI create the file must appear,
after an API component-add it must contain the new component, and after a UI
delete it must be gone.

This client talks to the Forgejo HTTP API with basic auth. It is intentionally
small and read-only; it does not clone the repo.
"""

from __future__ import annotations

import io
import time

import httpx
from ruamel.yaml import YAML


class ForgejoClient:
    """Read-only client for verifying project files in the Forgejo zad-projects repo."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        repo: str,
        *,
        branch: str = "main",
        verify_ssl: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.repo = repo.strip("/")
        self.branch = branch
        self._auth = (username, password)
        self._verify = verify_ssl

    def _project_path(self, project_name: str) -> str:
        return f"projects/{project_name}.yaml"

    def _contents_url(self, project_name: str) -> str:
        return f"{self.base_url}/api/v1/repos/{self.repo}/raw/{self._project_path(project_name)}?ref={self.branch}"

    def _projects_dir_url(self) -> str:
        return f"{self.base_url}/api/v1/repos/{self.repo}/contents/projects?ref={self.branch}"

    def list_project_names(self) -> set[str]:
        """Return the set of project names (file basenames without .yaml) in the repo."""
        with httpx.Client(verify=self._verify, timeout=30.0) as client:
            response = client.get(self._projects_dir_url(), auth=self._auth)
        if response.status_code == 404:
            return set()
        response.raise_for_status()
        entries = response.json()
        return {
            entry["name"].removesuffix(".yaml")
            for entry in entries
            if isinstance(entry, dict) and entry.get("name", "").endswith(".yaml")
        }

    def wait_for_new_project(self, before: set[str], *, timeout: float = 180.0, interval: float = 3.0) -> str | None:
        """Poll until a project name appears that was not in `before`, returning it.

        Project creation derives a random technical name from the display name, so
        the resulting file name cannot be predicted; we detect it by diffing the
        repo listing. Returns the new name, or None on timeout.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            new = self.list_project_names() - before
            if new:
                return sorted(new)[0]
            time.sleep(interval)
        new = self.list_project_names() - before
        return sorted(new)[0] if new else None

    def get_project_file(self, project_name: str) -> str | None:
        """Return the raw YAML text of the project file, or None if it does not exist."""
        with httpx.Client(verify=self._verify, timeout=30.0) as client:
            response = client.get(self._contents_url(project_name), auth=self._auth)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.text

    def project_file_exists(self, project_name: str) -> bool:
        return self.get_project_file(project_name) is not None

    def get_project_yaml(self, project_name: str) -> dict | None:
        """Return the parsed project file as a dict, or None if it does not exist."""
        text = self.get_project_file(project_name)
        if text is None:
            return None
        yaml = YAML(typ="safe")
        return yaml.load(io.StringIO(text))

    def get_first_deployment_name(self, project_name: str) -> str:
        """Return the name of the first deployment in the project file."""
        data = self.get_project_yaml(project_name)
        if not data:
            raise AssertionError(f"Project file for '{project_name}' not found in Forgejo")
        deployments = data.get("deployments") or []
        if not deployments:
            raise AssertionError(f"Project '{project_name}' has no deployments in its file")
        return deployments[0]["name"]

    def component_names(self, project_name: str) -> list[str]:
        """Return the top-level component names declared in the project file."""
        data = self.get_project_yaml(project_name)
        if not data:
            return []
        return [c.get("name") for c in (data.get("components") or []) if c.get("name")]

    def wait_for_project_file(self, project_name: str, *, timeout: float = 120.0, interval: float = 3.0) -> bool:
        """Poll until the project file exists, returning True, or False on timeout.

        Project creation is an async task, so the commit lands a few seconds after
        the wizard submit returns.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.project_file_exists(project_name):
                return True
            time.sleep(interval)
        return self.project_file_exists(project_name)

    def wait_for_project_gone(self, project_name: str, *, timeout: float = 120.0, interval: float = 3.0) -> bool:
        """Poll until the project file no longer exists, returning True, or False on timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.project_file_exists(project_name):
                return True
            time.sleep(interval)
        return not self.project_file_exists(project_name)

    def wait_for_component(
        self, project_name: str, component_name: str, *, timeout: float = 120.0, interval: float = 3.0
    ) -> bool:
        """Poll until the named component appears in the project file, returning True, or False on timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if component_name in self.component_names(project_name):
                return True
            time.sleep(interval)
        return component_name in self.component_names(project_name)
