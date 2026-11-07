"""close() mag niet opnieuw klonen als de clone zelf nooit is gelukt.

has_changes() roept ensure_repo_cloned() aan. Riep close() has_changes() ongeacht
_repo_cloned aan, dan verborg een herhaalde clonefout (DNS, auth, een task die faalde
voor de eerste clone klaar was) de werkelijke fout van de task: close() in een finally-
blok gooide dan zijn eigen RuntimeError over de oorspronkelijke heen.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from opi.connectors.git import GitConnector

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_close_does_not_reclone_when_the_repo_was_never_cloned(tmp_path: Path) -> None:
    connector = GitConnector(
        repo_url="https://github.com/RijksICTGilde/rig-cluster-projects.git",
        branch="main",
        working_dir=str(tmp_path),
    )
    assert connector._repo_cloned is False

    with patch.object(
        connector, "ensure_repo_cloned", new=AsyncMock(side_effect=RuntimeError("clone still fails"))
    ) as ensure_repo_cloned:
        await connector.close()

    ensure_repo_cloned.assert_not_called()
    assert connector._closed is True


@pytest.mark.asyncio
async def test_close_still_checks_for_uncommitted_changes_when_cloned(tmp_path: Path) -> None:
    connector = GitConnector(
        repo_url="https://github.com/RijksICTGilde/rig-cluster-projects.git",
        branch="main",
        working_dir=str(tmp_path),
    )
    connector._repo_cloned = True

    with patch.object(connector, "has_changes", new=AsyncMock(return_value=False)) as has_changes:
        await connector.close()

    has_changes.assert_called_once()
