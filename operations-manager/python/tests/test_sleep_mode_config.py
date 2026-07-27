"""Unit tests for the sleep-mode service config, cluster-default merge, and state."""

from datetime import timedelta

import pytest
from opi.services.catalog.sleep_mode import config as sleep_config
from opi.services.catalog.sleep_mode.config import SleepModeConfigError, load
from opi.services.catalog.sleep_mode.config_model import SleepModeConfig, parse_duration
from opi.services.catalog.sleep_mode.state import (
    STATE_AWAKE,
    STATE_SLEEPING,
    SleepState,
    read,
    write,
)


class TestParseDuration:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("48h", timedelta(hours=48)),
            ("90m", timedelta(minutes=90)),
            ("30s", timedelta(seconds=30)),
            ("2d", timedelta(days=2)),
            (" 1h ", timedelta(hours=1)),
        ],
    )
    def test_valid(self, value: str, expected: timedelta) -> None:
        assert parse_duration(value) == expected

    @pytest.mark.parametrize("value", ["48", "h", "48hh", "1w", "", "-1h", "1.5h"])
    def test_invalid_raises(self, value: str) -> None:
        with pytest.raises(ValueError, match="Invalid duration"):
            parse_duration(value)


class TestMatcher:
    def test_glob_match(self) -> None:
        config = SleepModeConfig(enabled=True, match=["PR-*"])
        assert config.matches("PR-123")
        assert config.matches("PR-")
        assert not config.matches("main")
        assert not config.matches("pr-123")  # case-sensitive

    def test_empty_match_matches_nothing(self) -> None:
        assert not SleepModeConfig(enabled=True).matches("PR-1")

    def test_multiple_patterns(self) -> None:
        config = SleepModeConfig(enabled=True, match=["PR-*", "preview-*"])
        assert config.matches("preview-9")
        assert config.matches("PR-9")


class TestLoad:
    def test_project_config_enabled(self) -> None:
        project = {"services": [{"name": "sleep-mode", "config": {"enabled": True, "match": ["PR-*"]}}]}
        config = load(project, cluster="local")
        assert config is not None
        assert config.enabled
        assert config.match == ["PR-*"]
        assert config.sleep_after_deploy_delta == timedelta(hours=48)  # model default

    def test_disabled_returns_none(self) -> None:
        project = {"services": [{"name": "sleep-mode", "config": {"enabled": False}}]}
        assert load(project, cluster="local") is None

    def test_no_entry_and_cluster_off_returns_none(self) -> None:
        assert load({"services": []}, cluster="local") is None

    def test_cluster_default_enables_without_project_entry(self) -> None:
        # sandboxed-local ships enabled by the service-owned cluster default.
        config = load({"services": []}, cluster="sandboxed-local")
        assert config is not None
        assert config.enabled
        assert config.match == []  # no project match -> nothing actually sleeps

    def test_project_overrides_cluster_default_per_key(self) -> None:
        project = {"services": [{"name": "sleep-mode", "config": {"enabled": False}}]}
        # Cluster default would enable, but the project turns it off.
        assert load(project, cluster="sandboxed-local") is None

    def test_legacy_single_key_dict_form(self) -> None:
        project = {"services": [{"sleep-mode": {"config": {"enabled": True, "match": ["x-*"]}}}]}
        config = load(project, cluster="local")
        assert config is not None
        assert config.match == ["x-*"]

    def test_invalid_duration_fails_loud(self) -> None:
        project = {"services": [{"name": "sleep-mode", "config": {"enabled": True, "sleep-after-deploy": "soon"}}]}
        with pytest.raises(SleepModeConfigError):
            load(project, cluster="local")

    def test_invalid_wake_mode_fails_loud(self) -> None:
        project = {"services": [{"name": "sleep-mode", "config": {"enabled": True, "wake-mode": "eventually"}}]}
        with pytest.raises(SleepModeConfigError):
            load(project, cluster="local")

    def test_unknown_config_key_fails_loud(self) -> None:
        project = {"services": [{"name": "sleep-mode", "config": {"enabled": True, "slep": True}}]}
        with pytest.raises(SleepModeConfigError):
            load(project, cluster="local")

    def test_waker_component_must_exist(self) -> None:
        project = {
            "services": [{"name": "sleep-mode", "config": {"enabled": True, "waker-component": "ghost"}}],
            "components": [{"name": "frontend"}],
        }
        with pytest.raises(SleepModeConfigError):
            load(project, cluster="local")

    def test_waker_component_existing_ok(self) -> None:
        project = {
            "services": [{"name": "sleep-mode", "config": {"enabled": True, "waker-component": "frontend"}}],
            "components": [{"name": "frontend"}],
        }
        config = load(project, cluster="local")
        assert config is not None
        assert config.waker_component == "frontend"

    def test_cluster_defaults_map_is_service_owned(self) -> None:
        # The default lives in the service package, not core/cluster_config.py.
        assert "sandboxed-local" in sleep_config._CLUSTER_DEFAULTS


class TestState:
    def test_read_default_is_awake(self) -> None:
        project = {"deployments": [{"name": "PR-1"}]}
        assert read(project, "PR-1").state == STATE_AWAKE

    def test_read_missing_deployment_is_awake(self) -> None:
        assert read({"deployments": []}, "PR-1").state == STATE_AWAKE

    def test_write_and_read_roundtrip(self) -> None:
        project = {"deployments": [{"name": "PR-1"}]}
        ok = write(
            project,
            "PR-1",
            SleepState(state=STATE_SLEEPING, expires_at="2026-07-28T14:03:00+02:00", wake_token="tok"),
        )
        assert ok
        stored = project["deployments"][0]["sleep"]
        assert stored == {
            "state": "sleeping",
            "expires-at": "2026-07-28T14:03:00+02:00",
            "wake-token": "tok",
        }
        state = read(project, "PR-1")
        assert state.state == STATE_SLEEPING
        assert state.wake_token == "tok"

    def test_write_awake_clears_block(self) -> None:
        project = {"deployments": [{"name": "PR-1", "sleep": {"state": "sleeping"}}]}
        write(project, "PR-1", SleepState(state=STATE_AWAKE))
        assert "sleep" not in project["deployments"][0]

    def test_write_missing_deployment_returns_false(self) -> None:
        assert not write({"deployments": []}, "PR-1", SleepState(state=STATE_SLEEPING))
