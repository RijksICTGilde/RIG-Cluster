"""Tests for the /version metadata (opi/core/version.py).

The endpoint exists to answer "which build is serving me". It could not answer that
during a rolling update: two pods serve one Service, consecutive calls report two
different commits, and the answer gave no way to tell that apart from drift. So the
answer names the pod and the image it runs.
"""

import json

import pytest
from opi.core.version import get_version_info, set_running_image


@pytest.fixture(autouse=True)
def clean_version_env(monkeypatch: pytest.MonkeyPatch):
    """Start every test from a known environment and a known image."""
    for name in ("ZAD_VERSION", "ZAD_GIT_COMMIT", "ZAD_GIT_BRANCH", "ZAD_BUILD_DATE", "POD_NAME", "ZAD_IMAGE"):
        monkeypatch.delenv(name, raising=False)
    set_running_image("")
    yield
    set_running_image("")


def test_defaults_without_environment_or_file(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("opi.core.version._VERSION_FILE", tmp_path / "absent.json")

    info = get_version_info()

    assert info["name"] == "ZAD"
    assert info["version"] == "0.1.0"
    assert info["pod"] == ""
    assert info["image"] == ""


def test_environment_is_reported_when_there_is_no_file(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("opi.core.version._VERSION_FILE", tmp_path / "absent.json")
    monkeypatch.setenv("ZAD_VERSION", "2e8e25fc")
    monkeypatch.setenv("ZAD_GIT_BRANCH", "main")

    info = get_version_info()

    assert info["version"] == "2e8e25fc"
    assert info["branch"] == "main"


def test_version_file_wins_over_the_environment(tmp_path, monkeypatch: pytest.MonkeyPatch):
    version_file = tmp_path / "version.json"
    version_file.write_text(json.dumps({"version": "abc1234", "commit": "abc1234def", "dirty": True}))
    monkeypatch.setattr("opi.core.version._VERSION_FILE", version_file)
    monkeypatch.setenv("ZAD_VERSION", "oud1111")

    info = get_version_info()

    assert info["version"] == "abc1234"
    assert info["commit"] == "abc1234def"
    assert info["dirty"] is True


def test_pod_name_comes_from_the_downward_api(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("opi.core.version._VERSION_FILE", tmp_path / "absent.json")
    monkeypatch.setenv("POD_NAME", "operations-manager-64884cd948-ngwjz")

    assert get_version_info()["pod"] == "operations-manager-64884cd948-ngwjz"


def test_image_is_what_the_cluster_reported(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("opi.core.version._VERSION_FILE", tmp_path / "absent.json")
    set_running_image("operations-manager:rc-77")

    assert get_version_info()["image"] == "operations-manager:rc-77"


def test_image_falls_back_to_the_build_time_value(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Outside Kubernetes there is no pod to ask; a baked ZAD_IMAGE still answers."""
    monkeypatch.setattr("opi.core.version._VERSION_FILE", tmp_path / "absent.json")
    monkeypatch.setenv("ZAD_IMAGE", "ghcr.io/minbzk/base-images/operations-manager:2026.08.12")

    assert get_version_info()["image"] == "ghcr.io/minbzk/base-images/operations-manager:2026.08.12"


def test_unreadable_version_file_does_not_break_the_answer(tmp_path, monkeypatch: pytest.MonkeyPatch):
    broken = tmp_path / "version.json"
    broken.write_text("{ not json")
    monkeypatch.setattr("opi.core.version._VERSION_FILE", broken)
    monkeypatch.setenv("ZAD_VERSION", "envonly")

    assert get_version_info()["version"] == "envonly"
