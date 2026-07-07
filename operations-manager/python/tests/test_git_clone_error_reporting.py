"""
Regression test for clone error reporting.

When every clone strategy fails, ``ensure_repo_cloned`` must report the FIRST
strategy's error (the clean attempt with the correct branch into an empty dir),
not the last fallback's noise. The fallbacks clone into the now non-empty working
dir and only emit "Cloning into '.'...", which hid a real DNS failure in prod.
"""

from unittest.mock import AsyncMock, patch

import pytest
from opi.connectors.git import GitConnector


@pytest.mark.asyncio
async def test_clone_failure_reports_primary_strategy_error(tmp_path):
    connector = GitConnector(
        repo_url="https://github.com/RijksICTGilde/rig-cluster-projects.git",
        branch="main",
        working_dir=str(tmp_path),
    )

    # Strategy 1 (correct branch) captures the real reason; the later fallbacks
    # clone into the now non-empty dir and only emit the progress line.
    dns_error = (
        "Cloning into '.'...\n"
        "fatal: unable to access 'https://github.com/RijksICTGilde/rig-cluster-projects.git/': "
        "Could not resolve host: github.com"
    )
    strategy_results = [
        ("", dns_error, 128),  # strategy 1: --single-branch --branch main --depth 1
        ("", "Cloning into '.'...", 128),  # strategy 3: --depth 1, no branch
        ("", "Cloning into '.'...", 128),  # strategy 4: no depth
    ]

    with (
        patch.object(connector, "_get_remote_default_branch", new=AsyncMock(return_value="main")),
        patch.object(connector, "_run_git_command", new=AsyncMock(side_effect=strategy_results)),
        pytest.raises(RuntimeError) as exc_info,
    ):
        await connector.ensure_repo_cloned()

    message = str(exc_info.value)
    # The actionable reason is surfaced...
    assert "Could not resolve host: github.com" in message
    # ...on a single collapsed line, and NOT the useless fallback progress noise.
    assert "\n" not in message
    assert not message.endswith("Cloning into '.'...")
