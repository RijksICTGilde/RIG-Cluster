"""
Test project cleanup for sandbox E2E tests.

Tracks projects created during tests and cleans them up on teardown.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import Page

logger = logging.getLogger(__name__)


class ProjectCleanup:
    """Tracks and cleans up test projects created during E2E runs."""

    def __init__(self) -> None:
        self._projects: list[str] = []

    def register(self, project_name: str) -> None:
        """Register a project for cleanup on teardown."""
        self._projects.append(project_name)
        logger.info("Registered project for cleanup: %s", project_name)

    def cleanup_via_ui(self, page: Page, base_url: str) -> None:
        """Delete registered projects via the web UI delete endpoint."""
        for project_name in self._projects:
            try:
                url = f"{base_url}/api/v2/projects/{project_name}"
                response = page.request.delete(url)
                if response.ok:
                    logger.info("Cleaned up project: %s", project_name)
                else:
                    logger.warning(
                        "Failed to clean up project %s: %s",
                        project_name,
                        response.status,
                    )
            except Exception:
                logger.warning("Error cleaning up project: %s", project_name, exc_info=True)
        self._projects.clear()

    @property
    def projects(self) -> list[str]:
        return list(self._projects)
