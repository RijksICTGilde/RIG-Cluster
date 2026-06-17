"""Regression test: _wait_for_pod must tolerate transient image-pull backoff.

ImagePullBackOff/ErrImagePull only appear after a pull attempt failed, but kubelet
keeps retrying and a transient registry/network hiccup recovers on retry. The old
code treated the first ImagePullBackOff as fatal and gave up - killing otherwise-fine
backups (always 'productie' at ~00:00). Now: retry-backoff is tolerated until a grace
period elapses; truly-fatal states (InvalidImageName, ...) still fail immediately.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest
from opi.manager.backup.base import BaseBackupManager


def _pod_json(reason: str | None = None, phase: str = "Pending") -> str:
    statuses = [{"state": {"waiting": {"reason": reason, "message": "x"}}}] if reason else []
    return json.dumps({"status": {"phase": phase, "containerStatuses": statuses}})


def _manager(status_sequence: list[str]) -> BaseBackupManager:
    mgr = BaseBackupManager.__new__(BaseBackupManager)
    mgr.kubectl = type("K", (), {})()
    mgr.kubectl.run_command = AsyncMock(side_effect=[(s, "", 0) for s in status_sequence])
    return mgr


class _Clock:
    """Monotonic-ish fake clock; each .time() call advances by `step`."""

    def __init__(self, step: float) -> None:
        self.t = 0.0
        self.step = step

    def time(self) -> float:
        v = self.t
        self.t += self.step
        return v


@pytest.mark.asyncio
async def test_transient_imagepullbackoff_recovers() -> None:
    # Two backoff polls then success - must NOT give up, must return True.
    mgr = _manager([_pod_json("ImagePullBackOff"), _pod_json("ImagePullBackOff"), _pod_json(phase="Succeeded")])
    with patch("opi.manager.backup.base.asyncio.sleep", new=AsyncMock()):
        assert await mgr._wait_for_pod("ns", "pod", timeout=600) is True


@pytest.mark.asyncio
async def test_invalid_image_name_fails_immediately() -> None:
    mgr = _manager([_pod_json("InvalidImageName")])
    with patch("opi.manager.backup.base.asyncio.sleep", new=AsyncMock()):
        assert await mgr._wait_for_pod("ns", "pod", timeout=600) is False


@pytest.mark.asyncio
async def test_persistent_imagepullbackoff_fails_after_grace() -> None:
    mgr = _manager([_pod_json("ImagePullBackOff")] * 3)
    mgr.IMAGE_PULL_GRACE_SECONDS = 50  # instance override
    clock = _Clock(step=100)  # elapsed jumps 100 each poll -> exceeds grace by 2nd poll
    with (
        patch("opi.manager.backup.base.asyncio.sleep", new=AsyncMock()),
        patch("opi.manager.backup.base.asyncio.get_event_loop", return_value=clock),
    ):
        assert await mgr._wait_for_pod("ns", "pod", timeout=100000) is False
