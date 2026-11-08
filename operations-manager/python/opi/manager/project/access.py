"""What a mutation group needs from its host, and nothing more."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable

    from opi.handlers.project_file_handler import ProjectFileHandler


class ProjectFileAccess(Protocol):
    """What the mutations reach from their host: reading and writing the project file.

    Breaks the import cycle: groups depend on this protocol rather than on
    ``ProjectManager``. Narrower by design: the groups need only what they pass, so a
    protocol that promised more would invite ``enforce_validation=False``. The
    compare-and-swap base stays on the host, where the retry on ``process_project_from_git``
    happens.
    """

    _project_file_handler: ProjectFileHandler

    async def get_contents(self) -> dict[str, Any]: ...

    async def get_name(self) -> str: ...

    async def get_deployments(self, cluster_filter: bool = True) -> list[dict[str, Any]]: ...

    async def save_and_commit_project(self, project_data: dict[str, Any], commit_message: str) -> None: ...

    async def mutate_and_commit_project(
        self, mutator: Callable[[dict[str, Any]], dict[str, Any] | None], commit_message: str
    ) -> bool: ...
