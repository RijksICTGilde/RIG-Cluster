"""What a mutation group needs from its host, and nothing more."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable

    from opi.services.project_file_handler import ProjectFileHandler
    from opi.services.project_store import MutationResult


class ProjectFileAccess(Protocol):
    """What the mutations reach from their host: reading and writing the project file.

    The groups depend on this protocol and not on ``ProjectManager``, which would be an
    import cycle.
    """

    _project_file_handler: ProjectFileHandler

    async def get_contents(self) -> dict[str, Any]: ...

    async def get_name(self) -> str: ...

    async def get_deployments(self, cluster_filter: bool = True) -> list[dict[str, Any]]: ...

    async def save_and_commit_project(self, project_data: dict[str, Any], commit_message: str) -> MutationResult: ...

    async def mutate_and_commit_project(
        self, mutator: Callable[[dict[str, Any]], dict[str, Any] | None], commit_message: str
    ) -> bool: ...
