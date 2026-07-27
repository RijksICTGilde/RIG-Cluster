"""Unit tests for sleep-mode state transitions, wake-token compare, and the secret."""

from datetime import UTC, datetime, timedelta

from opi.services.catalog.sleep_mode import service, token
from opi.services.catalog.sleep_mode.secret import WakeTokenSecret
from opi.services.catalog.sleep_mode.state import STATE_AWAKE, STATE_SLEEPING, STATE_WAKING, read

NOW = datetime(2026, 7, 28, 12, 0, 0, tzinfo=UTC)


def _project() -> dict:
    return {"deployments": [{"name": "PR-1"}]}


class TestToSleeping:
    def test_awake_to_sleeping_stores_token(self) -> None:
        project = _project()
        assert service.to_sleeping(project, "PR-1", encrypted_token="enc-tok")
        state = read(project, "PR-1")
        assert state.state == STATE_SLEEPING
        assert state.wake_token == "enc-tok"

    def test_no_token_still_sleeps(self) -> None:
        project = _project()
        assert service.to_sleeping(project, "PR-1", encrypted_token=None)
        assert read(project, "PR-1").state == STATE_SLEEPING
        assert read(project, "PR-1").wake_token is None

    def test_non_awake_is_noop(self) -> None:
        project = {"deployments": [{"name": "PR-1", "sleep": {"state": "sleeping"}}]}
        assert not service.to_sleeping(project, "PR-1", encrypted_token="x")


class TestBeginWake:
    def test_sleeping_to_waking_sets_deadline(self) -> None:
        project = {"deployments": [{"name": "PR-1", "sleep": {"state": "sleeping", "wake-token": "t"}}]}
        assert service.begin_wake(project, "PR-1", NOW, timedelta(minutes=10))
        state = read(project, "PR-1")
        assert state.state == STATE_WAKING
        assert state.expires_at == (NOW + timedelta(minutes=10)).isoformat()
        assert state.wake_token == "t"  # token preserved through waking

    def test_idempotent_second_call_is_noop(self) -> None:
        project = {"deployments": [{"name": "PR-1", "sleep": {"state": "sleeping"}}]}
        assert service.begin_wake(project, "PR-1", NOW, timedelta(minutes=10))
        assert not service.begin_wake(project, "PR-1", NOW, timedelta(minutes=10))

    def test_awake_cannot_wake(self) -> None:
        assert not service.begin_wake(_project(), "PR-1", NOW, timedelta(minutes=10))


class TestToAwake:
    def test_waking_to_awake_sets_deadline_and_clears_token(self) -> None:
        project = {"deployments": [{"name": "PR-1", "sleep": {"state": "waking", "wake-token": "t"}}]}
        assert service.to_awake(project, "PR-1", NOW, timedelta(hours=1))
        state = read(project, "PR-1")
        assert state.state == STATE_AWAKE
        assert state.expires_at == (NOW + timedelta(hours=1)).isoformat()
        assert state.wake_token is None

    def test_already_awake_is_noop(self) -> None:
        assert not service.to_awake(_project(), "PR-1", NOW, timedelta(hours=1))

    def test_sweeper_can_revert_stuck_waking(self) -> None:
        project = {"deployments": [{"name": "PR-1", "sleep": {"state": "waking"}}]}
        assert service.to_awake(project, "PR-1", NOW, timedelta(hours=1))
        assert read(project, "PR-1").state == STATE_AWAKE


class TestSetSleepDeadline:
    def test_sets_awake_with_deadline(self) -> None:
        project = {"deployments": [{"name": "PR-1", "sleep": {"state": "sleeping", "wake-token": "t"}}]}
        assert service.set_sleep_deadline(project, "PR-1", NOW, timedelta(hours=48))
        state = read(project, "PR-1")
        assert state.state == STATE_AWAKE
        assert state.expires_at == (NOW + timedelta(hours=48)).isoformat()
        assert state.wake_token is None


class TestTokenCompare:
    def test_equal(self) -> None:
        assert token.compare("abc123", "abc123")

    def test_unequal(self) -> None:
        assert not token.compare("abc123", "xyz789")

    def test_empty_never_matches(self) -> None:
        assert not token.compare("", "")
        assert not token.compare("x", "")
        assert not token.compare("", "x")


class TestWakeTokenSecret:
    def test_secret_name(self) -> None:
        assert WakeTokenSecret.get_secret_name("PR-1-frontend") == "PR-1-frontend-waker-token"

    def test_secret_data_is_the_token_env(self) -> None:
        assert WakeTokenSecret(token="s3cr3t").to_k8s_secret_data() == {"ZAD_WAKE_TOKEN": "s3cr3t"}
