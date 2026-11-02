"""Het wektoken hoort bij de deployment, niet bij de slaapronde.

Measured on 16-10-2026 (wies/pr-460, wies/pr-689): previous rounds minted new tokens, but
the waker pod reads once via envFrom and doesn't restart, so it kept sending the old one (401s).
These tests verify two sleep rounds yield the same token, via sweeper and via manual sleep.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from opi.services.catalog.sleep_mode import flow, service
from opi.services.catalog.sleep_mode import state as sleep_state
from opi.services.catalog.sleep_mode import token as token_mod
from opi.services.catalog.sleep_mode.scheduler import SLEEP, SleepModeScheduler

pytestmark = pytest.mark.skipif(
    shutil.which("age") is None or shutil.which("age-keygen") is None,
    reason="requires the age and age-keygen binaries",
)

PROJECT = "wies"
DEPLOYMENT = "pr-689"
CLUSTER = "local"
NOW = datetime(2026, 10, 16, 12, 0, 0, tzinfo=UTC)


def _project_data(public_key: str) -> dict[str, Any]:
    """Een projectbestand met sleep-mode aan, een wekbare frontend en een projectsleutel."""
    return {
        "name": PROJECT,
        "services": [{"name": "sleep-mode", "config": {"enabled": True, "match": ["pr-*"]}}],
        "config": {"age-public-key": public_key},
        "components": [{"name": "frontend", "services": ["publish-on-web"]}],
        "deployments": [
            {
                "name": DEPLOYMENT,
                "cluster": CLUSTER,
                "namespace": PROJECT,
                "components": [{"reference": "frontend"}],
                "sleep": {"state": "awake", "expires-at": (NOW - timedelta(minutes=1)).isoformat()},
            }
        ],
    }


class _Munten:
    """Telt hoe vaak er echt een token gemunt is."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.aantal = 0
        echte_mint = token_mod.mint

        def _mint() -> str:
            self.aantal += 1
            return echte_mint()

        monkeypatch.setattr(token_mod, "mint", _mint)


def _git(monkeypatch: pytest.MonkeyPatch, project_data: dict[str, Any]) -> None:
    """Zet de ProjectManager, de store en de reprocess stil; het ene projectbestand blijft."""
    manager = AsyncMock()
    manager.get_contents = AsyncMock(return_value=project_data)
    manager.save_and_commit_project = AsyncMock(return_value=None)
    manager.close = AsyncMock(return_value=None)
    monkeypatch.setattr("opi.manager.project_manager.ProjectManager", lambda **kwargs: manager)

    project = MagicMock()
    project.name = PROJECT
    project.filename = f"{PROJECT}.yaml"
    project.data = project_data
    store = MagicMock()
    store.get.return_value = project
    monkeypatch.setattr("opi.services.project_store.get_project_store", lambda: store)

    monkeypatch.setattr("opi.services.resource_tuning_service.trigger_reprocessing", AsyncMock(return_value=True))


def _wakker(project_data: dict[str, Any]) -> None:
    """Wekken zoals elk wekpad eindigt, zodat de volgende ronde weer kan slapen."""
    assert service.to_awake(project_data, DEPLOYMENT, NOW, timedelta(hours=1))


@pytest.mark.asyncio
async def test_de_sweeper_hergebruikt_het_token_over_twee_slaaprondes(
    monkeypatch: pytest.MonkeyPatch, age_keypair: tuple[str, str]
) -> None:
    _, public_key = age_keypair
    project_data = _project_data(public_key)
    _git(monkeypatch, project_data)
    munten = _Munten(monkeypatch)
    sweeper = SleepModeScheduler(CLUSTER)

    assert await sweeper._apply(PROJECT, f"{PROJECT}.yaml", [(DEPLOYMENT, SLEEP)], NOW)
    eerste = sleep_state.read(project_data, DEPLOYMENT).wake_token
    assert eerste

    _wakker(project_data)
    assert await sweeper._apply(PROJECT, f"{PROJECT}.yaml", [(DEPLOYMENT, SLEEP)], NOW)

    assert sleep_state.read(project_data, DEPLOYMENT).wake_token == eerste
    assert munten.aantal == 1, "de tweede slaapronde hergebruikt het token van de eerste"


@pytest.mark.asyncio
async def test_de_slaapknop_hergebruikt_het_token_over_twee_slaaprondes(
    monkeypatch: pytest.MonkeyPatch, age_keypair: tuple[str, str]
) -> None:
    _, public_key = age_keypair
    project_data = _project_data(public_key)
    _git(monkeypatch, project_data)
    munten = _Munten(monkeypatch)

    assert (await flow.sleep(PROJECT, DEPLOYMENT)).changed
    eerste = sleep_state.read(project_data, DEPLOYMENT).wake_token
    assert eerste

    _wakker(project_data)
    assert (await flow.sleep(PROJECT, DEPLOYMENT)).changed

    assert sleep_state.read(project_data, DEPLOYMENT).wake_token == eerste
    assert munten.aantal == 1, "de tweede slaapronde hergebruikt het token van de eerste"


def test_een_deployment_zonder_token_krijgt_er_een(age_keypair: tuple[str, str]) -> None:
    _, public_key = age_keypair
    project_data = _project_data(public_key)

    gemunt = token_mod.reuse_or_mint(project_data, DEPLOYMENT)
    assert gemunt.startswith("-----BEGIN AGE ENCRYPTED FILE-----")

    assert service.to_sleeping(project_data, DEPLOYMENT, gemunt)
    assert token_mod.reuse_or_mint(project_data, DEPLOYMENT) == gemunt
