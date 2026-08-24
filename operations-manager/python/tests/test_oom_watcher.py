"""Tests for the deployment health watcher (formerly OOM watcher)."""

import asyncio
import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.services.oom_watcher import (
    ComponentFailure,
    DeploymentHealthError,
    PodHealthResult,
    _describe_pod_waiting,
    _queue_refresh_task,
    _run_oom_check,
    check_all_components_health,
    check_pod_health,
    create_health_check_callback,
    schedule_oom_check,
)

# ---------------------------------------------------------------------------
# Helper: build pod JSON
# ---------------------------------------------------------------------------


def _make_pods_json(
    *,
    oom: bool = False,
    image_pull: str | None = None,
    crash_loop: bool = False,
    pod_name: str = "prod-api-abc",
    container_name: str = "app",
    pod_created: str = "",
    oom_finished: str = "",
    terminating: bool = False,
) -> str:
    container_statuses = []
    cs: dict = {"name": container_name, "lastState": {}, "state": {}}

    if oom:
        terminated = {"reason": "OOMKilled"}
        if oom_finished:
            terminated["finishedAt"] = oom_finished
        cs["lastState"] = {"terminated": terminated}

    if image_pull:
        cs["state"] = {"waiting": {"reason": image_pull, "message": "Back-off pulling image"}}
    elif crash_loop:
        cs["state"] = {"waiting": {"reason": "CrashLoopBackOff", "message": "back-off 5m0s"}}

    container_statuses.append(cs)

    metadata: dict = {"name": pod_name}
    if pod_created:
        metadata["creationTimestamp"] = pod_created
    if terminating:
        metadata["deletionTimestamp"] = "2026-07-09T22:00:00Z"

    pod = {"metadata": metadata, "status": {"containerStatuses": container_statuses}}
    return json.dumps({"items": [pod]})


# ---------------------------------------------------------------------------
# Helper: pods and replicasets across generations
# ---------------------------------------------------------------------------


def _generation_pod(*, pod_name: str, pod_template_hash: str, crash_loop: bool) -> dict:
    """One pod of a given generation, either crash-looping or healthy and Ready."""
    state = {"waiting": {"reason": "CrashLoopBackOff", "message": "back-off 5m0s"}} if crash_loop else {"running": {}}
    return {
        "metadata": {"name": pod_name, "labels": {"app": "prod-api", "pod-template-hash": pod_template_hash}},
        "status": {"containerStatuses": [{"name": "app", "ready": not crash_loop, "lastState": {}, "state": state}]},
    }


def _replicaset(*, pod_template_hash: str, revision: str, deployment_name: str = "prod-api") -> dict:
    return {
        "metadata": {
            "name": f"{deployment_name}-{pod_template_hash}",
            "labels": {"app": deployment_name, "pod-template-hash": pod_template_hash},
            "annotations": {"deployment.kubernetes.io/revision": revision},
            "ownerReferences": [{"kind": "Deployment", "name": deployment_name}],
        }
    }


def _kubectl_returning(pods: list[dict], replicasets: list[dict] | None) -> AsyncMock:
    """kubectl mock that answers 'get pods' and 'get replicasets' separately.

    ``replicasets=None`` simulates a cluster where the replicaset lookup fails,
    which must trigger the fallback path.
    """

    async def _run(args, *_a, **_kw):
        if args[1] == "replicasets":
            if replicasets is None:
                return "", "the server could not find the requested resource", 1
            return json.dumps({"items": replicasets}), "", 0
        return json.dumps({"items": pods}), "", 0

    return AsyncMock(side_effect=_run)


# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_oom_watcher_state():
    """Wipe the watcher's module dicts around every test in this file.

    Both dicts are deliberately NOT reset by the code under test (a counter that
    resets per round is no brake at all), so without this they leak from one test
    into the next and make the file order-dependent -- exactly what the
    ``pytest-randomly`` plugin in ``pyproject.toml`` is there to expose. Module-wide
    and autouse rather than per class: every class here shares the key
    ``myproject/production``.
    """
    from opi.services.oom_watcher import _last_tuned_pod_template_hash, _oom_tune_attempts

    _oom_tune_attempts.clear()
    _last_tuned_pod_template_hash.clear()
    yield
    _oom_tune_attempts.clear()
    _last_tuned_pod_template_hash.clear()


# ---------------------------------------------------------------------------
# check_pod_health
# ---------------------------------------------------------------------------


class TestCheckPodHealth:
    """Tests for the unified pod health check."""

    @patch("opi.services.oom_watcher.KubectlConnector")
    @pytest.mark.asyncio
    async def test_no_issues_detected(self, mock_kubectl_cls):
        mock_kubectl = MagicMock()
        mock_kubectl_cls.return_value = mock_kubectl
        mock_kubectl_cls.isConnected = True
        mock_kubectl.run_command = AsyncMock(return_value=(_make_pods_json(), "", 0))

        result = await check_pod_health("rig-prd-ns", "prod-api")

        assert result.component_name == "prod-api"
        assert result.oom_detected is False
        assert result.image_pull_error is None
        assert result.crash_loop_detected is False

    @patch("opi.services.oom_watcher.KubectlConnector")
    @pytest.mark.asyncio
    async def test_oom_detected(self, mock_kubectl_cls):
        mock_kubectl = MagicMock()
        mock_kubectl_cls.return_value = mock_kubectl
        mock_kubectl_cls.isConnected = True
        mock_kubectl.run_command = AsyncMock(return_value=(_make_pods_json(oom=True), "", 0))

        result = await check_pod_health("rig-prd-ns", "prod-api")

        assert result.oom_detected is True
        assert result.image_pull_error is None
        assert result.crash_loop_detected is False

    @patch("opi.services.oom_watcher.KubectlConnector")
    @pytest.mark.asyncio
    async def test_oom_on_terminating_pod_is_ignored(self, mock_kubectl_cls):
        # A pod being replaced during a rollout carries a stale OOM lastState; it must
        # not be treated as a live OOM (which would fail the deploy for a phantom).
        mock_kubectl = MagicMock()
        mock_kubectl_cls.return_value = mock_kubectl
        mock_kubectl_cls.isConnected = True
        mock_kubectl.run_command = AsyncMock(return_value=(_make_pods_json(oom=True, terminating=True), "", 0))

        result = await check_pod_health("rig-prd-ns", "prod-api")

        assert result.oom_detected is False

    @patch("opi.services.oom_watcher.KubectlConnector")
    @pytest.mark.asyncio
    async def test_crash_loop_on_superseded_generation_is_ignored(self, mock_kubectl_cls):
        # Production case: after a config fix the new ReplicaSet's pod runs 1/1, while
        # the old ReplicaSet's crash-looping pod is not yet reaped. It has NO
        # deletionTimestamp, so only the pod-template-hash tells the generations apart.
        mock_kubectl = MagicMock()
        mock_kubectl_cls.return_value = mock_kubectl
        mock_kubectl_cls.isConnected = True
        mock_kubectl.run_command = _kubectl_returning(
            pods=[
                _generation_pod(pod_name="prod-api-54475c7986-xcw7r", pod_template_hash="54475c7986", crash_loop=True),
                _generation_pod(pod_name="prod-api-6c444ccf9c-kl7l8", pod_template_hash="6c444ccf9c", crash_loop=False),
            ],
            replicasets=[
                _replicaset(pod_template_hash="54475c7986", revision="7"),
                _replicaset(pod_template_hash="6c444ccf9c", revision="8"),
            ],
        )

        result = await check_pod_health("rig-prd-ns", "prod-api")

        assert result.crash_loop_detected is False
        assert result.oom_detected is False
        assert result.image_pull_error is None

    @patch("opi.services.oom_watcher.KubectlConnector")
    @pytest.mark.asyncio
    async def test_crash_loop_on_current_generation_is_reported(self, mock_kubectl_cls):
        # The generation filter must not blind the detection: a pod of the current
        # ReplicaSet that crash-loops is a real failure.
        mock_kubectl = MagicMock()
        mock_kubectl_cls.return_value = mock_kubectl
        mock_kubectl_cls.isConnected = True
        mock_kubectl.run_command = _kubectl_returning(
            pods=[
                _generation_pod(pod_name="prod-api-54475c7986-xcw7r", pod_template_hash="54475c7986", crash_loop=False),
                _generation_pod(pod_name="prod-api-6c444ccf9c-kl7l8", pod_template_hash="6c444ccf9c", crash_loop=True),
            ],
            replicasets=[
                _replicaset(pod_template_hash="54475c7986", revision="7"),
                _replicaset(pod_template_hash="6c444ccf9c", revision="8"),
            ],
        )

        result = await check_pod_health("rig-prd-ns", "prod-api")

        assert result.crash_loop_detected is True
        assert result.crash_loop_message is not None
        assert "CrashLoopBackOff" in result.crash_loop_message

    @patch("opi.services.oom_watcher.KubectlConnector")
    @pytest.mark.asyncio
    async def test_falls_back_to_all_pods_when_hash_undeterminable(self, mock_kubectl_cls, caplog):
        # No determinable current generation (kubectl error, no Deployment-owned
        # ReplicaSet, ...): keep the previous behaviour and say so at WARNING.
        mock_kubectl = MagicMock()
        mock_kubectl_cls.return_value = mock_kubectl
        mock_kubectl_cls.isConnected = True
        mock_kubectl.run_command = _kubectl_returning(
            pods=[
                _generation_pod(pod_name="prod-api-54475c7986-xcw7r", pod_template_hash="54475c7986", crash_loop=True),
                _generation_pod(pod_name="prod-api-6c444ccf9c-kl7l8", pod_template_hash="6c444ccf9c", crash_loop=False),
            ],
            replicasets=None,
        )

        with caplog.at_level(logging.WARNING, logger="opi.services.oom_watcher"):
            result = await check_pod_health("rig-prd-ns", "prod-api")

        assert result.crash_loop_detected is True
        assert "pod-template-hash" in caplog.text

    @patch("opi.services.oom_watcher.KubectlConnector")
    @pytest.mark.asyncio
    async def test_image_pull_backoff_detected(self, mock_kubectl_cls):
        mock_kubectl = MagicMock()
        mock_kubectl_cls.return_value = mock_kubectl
        mock_kubectl_cls.isConnected = True
        mock_kubectl.run_command = AsyncMock(return_value=(_make_pods_json(image_pull="ImagePullBackOff"), "", 0))

        result = await check_pod_health("rig-prd-ns", "prod-api")

        assert result.oom_detected is False
        assert result.image_pull_error is not None
        assert "ImagePullBackOff" in result.image_pull_error

    @patch("opi.services.oom_watcher.KubectlConnector")
    @pytest.mark.asyncio
    async def test_err_image_pull_detected(self, mock_kubectl_cls):
        mock_kubectl = MagicMock()
        mock_kubectl_cls.return_value = mock_kubectl
        mock_kubectl_cls.isConnected = True
        mock_kubectl.run_command = AsyncMock(return_value=(_make_pods_json(image_pull="ErrImagePull"), "", 0))

        result = await check_pod_health("rig-prd-ns", "prod-api")
        assert result.image_pull_error is not None
        assert "ErrImagePull" in result.image_pull_error

    @patch("opi.services.oom_watcher.KubectlConnector")
    @pytest.mark.asyncio
    async def test_err_image_never_pull_detected(self, mock_kubectl_cls):
        # imagePullPolicy: Never + image not on node (kind/sandbox). Same user-facing
        # outcome as ImagePullBackOff and terminal, so it must be detected too.
        mock_kubectl = MagicMock()
        mock_kubectl_cls.return_value = mock_kubectl
        mock_kubectl_cls.isConnected = True
        mock_kubectl.run_command = AsyncMock(return_value=(_make_pods_json(image_pull="ErrImageNeverPull"), "", 0))

        result = await check_pod_health("rig-prd-ns", "prod-api")
        assert result.oom_detected is False
        assert result.image_pull_error is not None
        assert "ErrImageNeverPull" in result.image_pull_error

    @staticmethod
    def _two_container_pod(app_waiting: dict | None, sidecar_waiting: dict | None) -> str:
        """Pod with the component's own 'app' container plus an injected sidecar."""

        def cs(name: str, image: str, waiting: dict | None) -> dict:
            return {"name": name, "image": image, "lastState": {}, "state": {"waiting": waiting} if waiting else {}}

        pod = {
            "metadata": {"name": "prod-api-abc"},
            "status": {
                "containerStatuses": [
                    cs("app", "registry.example/prod-api:1.0", app_waiting),
                    cs("authorization-wall", "quay.io/oauth2-proxy/oauth2-proxy:v7.7.1", sidecar_waiting),
                ]
            },
        }
        return json.dumps({"items": [pod]})

    @patch("opi.services.oom_watcher.KubectlConnector")
    @pytest.mark.asyncio
    async def test_sidecar_image_pull_attributed_to_sidecar_not_main(self, mock_kubectl_cls):
        # Regression: a sidecar (authorization-wall) that cannot pull its image must
        # be attributed to that sidecar container and image, NOT the component's own
        # 'app' image (which is healthy here).
        mock_kubectl = MagicMock()
        mock_kubectl_cls.return_value = mock_kubectl
        mock_kubectl_cls.isConnected = True
        pods_json = self._two_container_pod(
            app_waiting=None,
            sidecar_waiting={"reason": "ImagePullBackOff", "message": "Back-off pulling image"},
        )
        mock_kubectl.run_command = AsyncMock(return_value=(pods_json, "", 0))

        result = await check_pod_health("rig-prd-ns", "prod-api")

        assert result.image_pull_error is not None
        assert result.image_pull_container == "authorization-wall"
        assert result.image_pull_image == "quay.io/oauth2-proxy/oauth2-proxy:v7.7.1"

    @patch("opi.services.oom_watcher.KubectlConnector")
    @pytest.mark.asyncio
    async def test_main_container_takes_precedence_over_sidecar(self, mock_kubectl_cls):
        # When both the main 'app' image and a sidecar fail, the component's own
        # image wins: it drives auto-disable and is the user's actual problem.
        mock_kubectl = MagicMock()
        mock_kubectl_cls.return_value = mock_kubectl
        mock_kubectl_cls.isConnected = True
        pods_json = self._two_container_pod(
            app_waiting={"reason": "ImagePullBackOff", "message": "Back-off pulling image"},
            sidecar_waiting={"reason": "ImagePullBackOff", "message": "Back-off pulling image"},
        )
        mock_kubectl.run_command = AsyncMock(return_value=(pods_json, "", 0))

        result = await check_pod_health("rig-prd-ns", "prod-api")

        assert result.image_pull_container == "app"
        assert result.image_pull_image == "registry.example/prod-api:1.0"

    @patch("opi.services.oom_watcher.KubectlConnector")
    @pytest.mark.asyncio
    async def test_crash_loop_detected(self, mock_kubectl_cls):
        mock_kubectl = MagicMock()
        mock_kubectl_cls.return_value = mock_kubectl
        mock_kubectl_cls.isConnected = True
        mock_kubectl.run_command = AsyncMock(return_value=(_make_pods_json(crash_loop=True), "", 0))

        result = await check_pod_health("rig-prd-ns", "prod-api")

        assert result.oom_detected is False
        assert result.image_pull_error is None
        assert result.crash_loop_detected is True
        assert result.crash_loop_message is not None
        assert "CrashLoopBackOff" in result.crash_loop_message

    @patch("opi.services.oom_watcher.KubectlConnector")
    @pytest.mark.asyncio
    async def test_kubectl_not_connected(self, mock_kubectl_cls):
        mock_kubectl_cls.isConnected = False

        result = await check_pod_health("rig-prd-ns", "prod-api")

        assert result.oom_detected is False
        assert result.image_pull_error is None
        assert result.crash_loop_detected is False

    @patch("opi.services.oom_watcher.KubectlConnector")
    @pytest.mark.asyncio
    async def test_kubectl_command_fails(self, mock_kubectl_cls):
        mock_kubectl = MagicMock()
        mock_kubectl_cls.return_value = mock_kubectl
        mock_kubectl_cls.isConnected = True
        mock_kubectl.run_command = AsyncMock(return_value=("", "error", 1))

        result = await check_pod_health("rig-prd-ns", "prod-api")
        assert result.oom_detected is False

    @patch("opi.services.oom_watcher.KubectlConnector")
    @pytest.mark.asyncio
    async def test_no_pods_found(self, mock_kubectl_cls):
        mock_kubectl = MagicMock()
        mock_kubectl_cls.return_value = mock_kubectl
        mock_kubectl_cls.isConnected = True
        mock_kubectl.run_command = AsyncMock(return_value=('{"items": []}', "", 0))

        result = await check_pod_health("rig-prd-ns", "prod-api")
        assert result.oom_detected is False

    @patch("opi.services.oom_watcher.KubectlConnector")
    @pytest.mark.asyncio
    async def test_stale_oom_ignored(self, mock_kubectl_cls):
        """OOM from before pod creation should be ignored."""
        mock_kubectl = MagicMock()
        mock_kubectl_cls.return_value = mock_kubectl
        mock_kubectl_cls.isConnected = True
        mock_kubectl.run_command = AsyncMock(
            return_value=(
                _make_pods_json(
                    oom=True,
                    pod_created="2026-01-02T00:00:00Z",
                    oom_finished="2026-01-01T00:00:00Z",
                ),
                "",
                0,
            )
        )

        result = await check_pod_health("rig-prd-ns", "prod-api")
        assert result.oom_detected is False

    @patch("opi.services.oom_watcher.KubectlConnector")
    @pytest.mark.asyncio
    async def test_non_oom_termination_ignored(self, mock_kubectl_cls):
        """Terminated with a non-OOM reason should not set oom_detected."""
        mock_kubectl = MagicMock()
        mock_kubectl_cls.return_value = mock_kubectl
        mock_kubectl_cls.isConnected = True

        pods_json = json.dumps(
            {
                "items": [
                    {
                        "metadata": {"name": "prod-api-abc"},
                        "status": {
                            "containerStatuses": [
                                {
                                    "name": "app",
                                    "lastState": {"terminated": {"reason": "Error"}},
                                    "state": {},
                                }
                            ]
                        },
                    }
                ]
            }
        )
        mock_kubectl.run_command = AsyncMock(return_value=(pods_json, "", 0))

        result = await check_pod_health("rig-prd-ns", "prod-api")
        assert result.oom_detected is False

    @patch("opi.services.oom_watcher.KubectlConnector")
    @pytest.mark.asyncio
    async def test_exit_137_without_oomkilled_not_oom(self, mock_kubectl_cls):
        """exit code 137 with reason!=OOMKilled (e.g. a startup-probe SIGKILL)
        must NOT be flagged as OOM. 137 is 128+SIGKILL and is produced by probe
        kills and evictions too, so the cgroup OOMKilled reason is required."""
        mock_kubectl = MagicMock()
        mock_kubectl_cls.return_value = mock_kubectl
        mock_kubectl_cls.isConnected = True

        pods_json = json.dumps(
            {
                "items": [
                    {
                        "metadata": {"name": "prod-api-abc"},
                        "status": {
                            "containerStatuses": [
                                {
                                    "name": "app",
                                    "lastState": {"terminated": {"reason": "Error", "exitCode": 137}},
                                    "state": {},
                                }
                            ]
                        },
                    }
                ]
            }
        )
        mock_kubectl.run_command = AsyncMock(return_value=(pods_json, "", 0))

        result = await check_pod_health("rig-prd-ns", "prod-api")
        assert result.oom_detected is False


# ---------------------------------------------------------------------------
# check_all_components_health
# ---------------------------------------------------------------------------


class TestCheckAllComponentsHealth:
    """Tests for multi-component health check."""

    @patch("opi.services.oom_watcher.check_pod_health", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_returns_only_unhealthy(self, mock_check):
        mock_check.side_effect = [
            PodHealthResult("comp-a", oom_detected=True),
            PodHealthResult("comp-b"),  # healthy
            PodHealthResult("comp-c", crash_loop_detected=True, crash_loop_message="CrashLoopBackOff"),
        ]

        results = await check_all_components_health("rig-prd-ns", ["comp-a", "comp-b", "comp-c"])

        assert len(results) == 2
        assert results[0].component_name == "comp-a"
        assert results[1].component_name == "comp-c"

    @patch("opi.services.oom_watcher.check_pod_health", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_returns_empty_when_all_healthy(self, mock_check):
        mock_check.return_value = PodHealthResult("comp-a")

        results = await check_all_components_health("rig-prd-ns", ["comp-a", "comp-b"])
        assert results == []


# ---------------------------------------------------------------------------
# create_health_check_callback
# ---------------------------------------------------------------------------


class TestCreateHealthCheckCallback:
    """Tests for the on_progressing callback factory."""

    @patch("opi.services.oom_watcher.check_all_components_health", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_checks_crashloop_before_grace_but_skips_oom(self, mock_check):
        """CrashLoop/ImagePull are checked immediately, OOM waits for grace period."""
        # Return OOM only — should be skipped before grace
        mock_check.return_value = [PodHealthResult("comp-a", oom_detected=True)]
        callback = create_health_check_callback("myproject", "production", "rig-prd-ns", ["comp-a"], grace_seconds=30)

        # Should check but not raise (OOM ignored before grace)
        await callback(10)
        mock_check.assert_called_once()

    @patch("opi.services.oom_watcher.check_all_components_health", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_raises_crashloop_before_grace(self, mock_check):
        """CrashLoopBackOff should raise immediately, no grace period needed."""
        mock_check.return_value = [
            PodHealthResult("comp-a", crash_loop_detected=True, crash_loop_message="CrashLoopBackOff")
        ]
        callback = create_health_check_callback("myproject", "production", "rig-prd-ns", ["comp-a"], grace_seconds=30)

        with pytest.raises(DeploymentHealthError) as exc_info:
            await callback(10)

        assert exc_info.value.failures[0].failure_type == "crash_loop"

    @patch("opi.services.oom_watcher.check_all_components_health", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_raises_on_oom_after_grace(self, mock_check):
        mock_check.return_value = [PodHealthResult("comp-a", oom_detected=True)]
        callback = create_health_check_callback("myproject", "production", "rig-prd-ns", ["comp-a"], grace_seconds=5)

        with pytest.raises(DeploymentHealthError) as exc_info:
            await callback(10)

        assert len(exc_info.value.failures) == 1
        assert exc_info.value.failures[0].failure_type == "oom"
        assert exc_info.value.failures[0].component_name == "comp-a"

    @patch("opi.services.oom_watcher.check_all_components_health", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_raises_on_crash_loop_after_grace(self, mock_check):
        mock_check.return_value = [
            PodHealthResult("comp-a", crash_loop_detected=True, crash_loop_message="CrashLoopBackOff: back-off")
        ]
        callback = create_health_check_callback("myproject", "production", "rig-prd-ns", ["comp-a"], grace_seconds=5)

        with pytest.raises(DeploymentHealthError) as exc_info:
            await callback(10)

        assert exc_info.value.failures[0].failure_type == "crash_loop"

    @patch("opi.services.oom_watcher.check_all_components_health", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_raises_on_image_pull_after_grace(self, mock_check):
        mock_check.return_value = [
            PodHealthResult("comp-a", image_pull_error="ImagePullBackOff: Back-off pulling image")
        ]
        callback = create_health_check_callback("myproject", "production", "rig-prd-ns", ["comp-a"], grace_seconds=5)

        with pytest.raises(DeploymentHealthError) as exc_info:
            await callback(10)

        assert exc_info.value.failures[0].failure_type == "image_pull"

    @patch("opi.services.oom_watcher.check_all_components_health", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_multiple_failure_types_in_one_error(self, mock_check):
        """Different components with different failures should all be in one error."""
        mock_check.return_value = [
            PodHealthResult("comp-a", oom_detected=True),
            PodHealthResult("comp-b", image_pull_error="ImagePullBackOff: bad image"),
        ]
        callback = create_health_check_callback(
            "myproject", "production", "rig-prd-ns", ["comp-a", "comp-b"], grace_seconds=5
        )

        with pytest.raises(DeploymentHealthError) as exc_info:
            await callback(10)

        failures = exc_info.value.failures
        assert len(failures) == 2
        types = {f.failure_type for f in failures}
        assert types == {"oom", "image_pull"}

    @patch("opi.services.oom_watcher.check_all_components_health", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_oom_with_crashloop_pre_grace_emits_oom_failure(self, mock_check):
        """When OOM and CrashLoopBackOff are co-detected, emit the OOM failure even
        before the grace period. The crash loop proves the lastState.terminated data
        is current, so the grace period's stale-data concern doesn't apply. Without
        this, pods that OOM on boot (tiny memory limit) get reported only as
        CrashLoopBackOff and auto-tune never runs.
        """
        mock_check.return_value = [
            PodHealthResult(
                "comp-a",
                oom_detected=True,
                crash_loop_detected=True,
                crash_loop_message="CrashLoopBackOff: back-off 5m",
            )
        ]
        callback = create_health_check_callback("myproject", "production", "rig-prd-ns", ["comp-a"], grace_seconds=30)

        with pytest.raises(DeploymentHealthError) as exc_info:
            await callback(5)  # well under grace period

        types = {f.failure_type for f in exc_info.value.failures}
        assert "oom" in types, "OOM must be reported so auto-tune runs"
        assert "crash_loop" in types

    @patch("opi.services.oom_watcher.check_all_components_health", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_no_raise_when_healthy(self, mock_check):
        mock_check.return_value = []
        callback = create_health_check_callback("myproject", "production", "rig-prd-ns", ["comp-a"], grace_seconds=5)

        # Should not raise
        await callback(10)

    @patch("opi.services.oom_watcher.check_all_components_health", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_checks_every_poll_after_grace(self, mock_check):
        mock_check.return_value = []
        callback = create_health_check_callback("myproject", "production", "rig-prd-ns", ["comp-a"], grace_seconds=5)

        await callback(10)
        await callback(15)
        await callback(20)

        assert mock_check.call_count == 3

    @patch("opi.services.oom_watcher.check_all_components_health", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_stops_after_max_elapsed(self, mock_check):
        mock_check.return_value = []
        callback = create_health_check_callback("myproject", "production", "rig-prd-ns", ["comp-a"], grace_seconds=5)

        await callback(10)  # check
        await callback(130)  # > 120s max, should skip

        mock_check.assert_called_once()

    @patch("opi.services.oom_watcher.check_all_components_health", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_image_pull_still_detected_when_oom_budget_exhausted(self, mock_check):
        """Regression: at the OOM cap the callback must stay non-None and still
        raise on ImagePullBackOff. Previously it returned None, blinding all
        inline detection (production symptom: broken image hung in Progressing).
        """
        from opi.services.oom_watcher import OOM_MAX_TUNE_ATTEMPTS, _oom_tune_attempts

        _oom_tune_attempts["myproject/production"] = OOM_MAX_TUNE_ATTEMPTS
        mock_check.return_value = [
            PodHealthResult("comp-a", image_pull_error="ImagePullBackOff: Back-off pulling image")
        ]

        callback = create_health_check_callback("myproject", "production", "rig-prd-ns", ["comp-a"], grace_seconds=5)
        assert callback is not None

        with pytest.raises(DeploymentHealthError) as exc_info:
            await callback(10)

        assert exc_info.value.failures[0].failure_type == "image_pull"

    @patch("opi.services.oom_watcher.check_all_components_health", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_crash_loop_still_detected_when_oom_budget_exhausted(self, mock_check):
        """At the OOM cap, CrashLoopBackOff must still raise."""
        from opi.services.oom_watcher import OOM_MAX_TUNE_ATTEMPTS, _oom_tune_attempts

        _oom_tune_attempts["myproject/production"] = OOM_MAX_TUNE_ATTEMPTS
        mock_check.return_value = [
            PodHealthResult("comp-a", crash_loop_detected=True, crash_loop_message="CrashLoopBackOff")
        ]

        callback = create_health_check_callback("myproject", "production", "rig-prd-ns", ["comp-a"], grace_seconds=5)
        assert callback is not None

        with pytest.raises(DeploymentHealthError) as exc_info:
            await callback(10)

        assert exc_info.value.failures[0].failure_type == "crash_loop"

    @patch("opi.services.oom_watcher.check_all_components_health", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_oom_only_suppressed_when_budget_exhausted(self, mock_check):
        """At the OOM cap, an OOM-only condition does not raise and does not
        increment the counter further (OOM auto-tune is suppressed)."""
        from opi.services.oom_watcher import OOM_MAX_TUNE_ATTEMPTS, _oom_tune_attempts

        _oom_tune_attempts["myproject/production"] = OOM_MAX_TUNE_ATTEMPTS
        mock_check.return_value = [PodHealthResult("comp-a", oom_detected=True)]

        callback = create_health_check_callback("myproject", "production", "rig-prd-ns", ["comp-a"], grace_seconds=0)
        assert callback is not None

        # OOM-only: must not raise now that the budget is spent
        await callback(10)

        # No OOM increment happened: the counter still sits exactly at the cap. It is
        # no longer popped at creation, so "absent" would hide a reset instead of
        # proving the budget held.
        assert _oom_tune_attempts["myproject/production"] == OOM_MAX_TUNE_ATTEMPTS

    @patch("opi.services.oom_watcher.check_all_components_health", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_image_pull_raises_but_oom_suppressed_in_same_batch(self, mock_check):
        """At the OOM cap, an image-pull failure in the same batch as an OOM
        component still raises — but the error carries only the image_pull
        failure, not the suppressed OOM."""
        from opi.services.oom_watcher import OOM_MAX_TUNE_ATTEMPTS, _oom_tune_attempts

        _oom_tune_attempts["myproject/production"] = OOM_MAX_TUNE_ATTEMPTS
        mock_check.return_value = [
            PodHealthResult("comp-a", oom_detected=True),
            PodHealthResult("comp-b", image_pull_error="ImagePullBackOff: bad image"),
        ]

        callback = create_health_check_callback(
            "myproject", "production", "rig-prd-ns", ["comp-a", "comp-b"], grace_seconds=5
        )
        assert callback is not None

        with pytest.raises(DeploymentHealthError) as exc_info:
            await callback(10)

        types = {f.failure_type for f in exc_info.value.failures}
        assert types == {"image_pull"}
        # OOM suppressed: no OOM increment, counter still exactly at the cap.
        assert _oom_tune_attempts["myproject/production"] == OOM_MAX_TUNE_ATTEMPTS

    @patch("opi.services.oom_watcher.check_all_components_health", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_oom_cap_bounds_tuning_across_rounds(self, mock_check):
        """The OOM cap bounds auto-tune ACROSS rounds, not within one round.

        Each OOM->tune->refresh round re-creates the callback. The counter carries
        over those rounds (creating a callback no longer clears it), so the cap is
        reached after OOM_MAX_TUNE_ATTEMPTS rounds and stays reached: the OOM raise
        path stops while the callback stays alive for other failure types.
        """
        from opi.services.oom_watcher import OOM_MAX_TUNE_ATTEMPTS, _oom_tune_attempts

        mock_check.return_value = [PodHealthResult("comp-a", oom_detected=True)]

        # First OOM_MAX_TUNE_ATTEMPTS rounds each raise and bump the counter.
        for expected in range(1, OOM_MAX_TUNE_ATTEMPTS + 1):
            callback = create_health_check_callback(
                "myproject", "production", "rig-prd-ns", ["comp-a"], grace_seconds=0
            )
            with pytest.raises(DeploymentHealthError):
                await callback(5)
            assert _oom_tune_attempts["myproject/production"] == expected

        # Next round: budget exhausted -> OOM no longer raises, no further bump.
        callback = create_health_check_callback("myproject", "production", "rig-prd-ns", ["comp-a"], grace_seconds=0)
        await callback(5)
        assert _oom_tune_attempts["myproject/production"] == OOM_MAX_TUNE_ATTEMPTS

    @patch("opi.services.oom_watcher.check_all_components_health", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_fresh_deploy_resets_oom_budget(self, mock_check):
        """A genuinely fresh deploy (explicit counter reset) lets a later
        legitimate OOM auto-tune again."""
        from opi.services.oom_watcher import (
            OOM_MAX_TUNE_ATTEMPTS,
            _oom_tune_attempts,
            reset_inline_oom_attempts,
        )

        _oom_tune_attempts["myproject/production"] = OOM_MAX_TUNE_ATTEMPTS
        reset_inline_oom_attempts("myproject", "production")

        mock_check.return_value = [PodHealthResult("comp-a", oom_detected=True)]
        callback = create_health_check_callback("myproject", "production", "rig-prd-ns", ["comp-a"], grace_seconds=0)

        with pytest.raises(DeploymentHealthError) as exc_info:
            await callback(5)

        assert exc_info.value.failures[0].failure_type == "oom"
        assert _oom_tune_attempts["myproject/production"] == 1

    @patch("opi.services.oom_watcher.check_all_components_health", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_increments_oom_attempt_counter(self, mock_check):
        from opi.services.oom_watcher import _oom_tune_attempts

        mock_check.return_value = [PodHealthResult("comp-a", oom_detected=True)]
        callback = create_health_check_callback("myproject", "production", "rig-prd-ns", ["comp-a"], grace_seconds=0)

        with pytest.raises(DeploymentHealthError):
            await callback(5)

        assert _oom_tune_attempts["myproject/production"] == 1

    @patch("opi.services.oom_watcher.check_all_components_health", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_crash_loop_does_not_increment_oom_counter(self, mock_check):
        """CrashLoopBackOff should not count as an OOM attempt."""
        from opi.services.oom_watcher import _oom_tune_attempts

        mock_check.return_value = [
            PodHealthResult("comp-a", crash_loop_detected=True, crash_loop_message="CrashLoopBackOff")
        ]
        callback = create_health_check_callback("myproject", "production", "rig-prd-ns", ["comp-a"], grace_seconds=0)

        with pytest.raises(DeploymentHealthError):
            await callback(5)

        assert "myproject/production" not in _oom_tune_attempts

    @patch("opi.services.oom_watcher.check_all_components_health", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_two_callbacks_share_one_budget(self, mock_check):
        """Two callbacks built for the same deployment share one live budget.

        The production loop builds a callback per sync wait, and several can be alive
        at once (six tasks worked on pr-494 simultaneously). When each carries its own
        snapshot of the counter, every one of them starts at zero and the cap never
        binds. Reading the shared dict on every call is what makes the brake real.
        """
        from opi.services.oom_watcher import OOM_MAX_TUNE_ATTEMPTS, _oom_tune_attempts

        assert OOM_MAX_TUNE_ATTEMPTS == 3, "this test spreads exactly 3 detections over two callbacks"
        mock_check.return_value = [PodHealthResult("comp-a", oom_detected=True)]

        # Both callbacks are built BEFORE anything is detected: with a snapshot both
        # would carry current_attempts=0 for their whole lifetime.
        first = create_health_check_callback("myproject", "production", "rig-prd-ns", ["comp-a"], grace_seconds=0)
        second = create_health_check_callback("myproject", "production", "rig-prd-ns", ["comp-a"], grace_seconds=0)

        for callback in (first, second, first):
            with pytest.raises(DeploymentHealthError):
                await callback(5)

        assert _oom_tune_attempts["myproject/production"] == OOM_MAX_TUNE_ATTEMPTS

        # The fourth detection, on either callback, must no longer report an OOM.
        await second(5)
        await first(5)
        assert _oom_tune_attempts["myproject/production"] == OOM_MAX_TUNE_ATTEMPTS

    @patch("opi.services.oom_watcher.check_all_components_health", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_warns_when_the_brake_closes(self, mock_check, caplog):
        """The exhaustion warning is logged when the brake actually closes.

        It used to be logged while BUILDING the callback. Now that the budget is read
        live, that moment no longer coincides with the brake closing, so the warning
        moved into the callback -- otherwise it would disappear from the logs of
        exactly the deployment that needs manual attention.
        """
        from opi.services.oom_watcher import OOM_MAX_TUNE_ATTEMPTS, _oom_tune_attempts

        mock_check.return_value = [PodHealthResult("comp-a", oom_detected=True)]
        callback = create_health_check_callback("myproject", "production", "rig-prd-ns", ["comp-a"], grace_seconds=0)

        with caplog.at_level(logging.WARNING, logger="opi.services.oom_watcher"):
            for _ in range(OOM_MAX_TUNE_ATTEMPTS):
                with pytest.raises(DeploymentHealthError):
                    await callback(5)
            assert "max OOM tune attempts" not in caplog.text, "not spent yet, nothing to warn about"

            await callback(5)
            assert "max OOM tune attempts" in caplog.text
            assert "myproject/production" in caplog.text

        assert _oom_tune_attempts["myproject/production"] == OOM_MAX_TUNE_ATTEMPTS

    def test_creation_does_not_reset_counter(self):
        """Building a callback must NOT clear the budget.

        Creating a callback is no proof of a fresh deploy: the automated refresh a
        tune queues for itself builds one too, so resetting here wiped the brake once
        per escalation round. Only an explicit reset clears the counter.
        """
        from opi.services.oom_watcher import _oom_tune_attempts

        _oom_tune_attempts["myproject/production"] = 2

        create_health_check_callback("myproject", "production", "rig-prd-ns", ["comp-a"], grace_seconds=0)

        assert _oom_tune_attempts["myproject/production"] == 2


# ---------------------------------------------------------------------------
# _run_oom_check
# ---------------------------------------------------------------------------


class TestRunOomCheck:
    """Tests for the internal health check coroutine."""

    @patch("opi.services.oom_watcher._queue_refresh_task", new_callable=AsyncMock)
    @patch("opi.services.deployment_observation.run_after_sync_observation", new_callable=AsyncMock)
    @patch("opi.services.oom_watcher.check_pod_health", new_callable=AsyncMock)
    @patch("opi.services.oom_watcher.get_project_data")
    @patch("opi.services.oom_watcher.get_prefixed_namespace", return_value="rig-prd-myproject")
    @pytest.mark.asyncio
    async def test_no_issues_does_not_tune(self, mock_prefix, mock_get_data, mock_check, mock_tune, mock_queue):
        mock_get_data.return_value = (
            {
                "deployments": [
                    {
                        "name": "production",
                        "namespace": "myproject",
                        "cluster": "local",
                        "components": [{"reference": "api"}],
                    }
                ]
            },
            "myproject.yaml",
        )
        mock_check.return_value = PodHealthResult("production-api")

        await _run_oom_check("myproject", "production", attempt=1, max_attempts=3, delay_seconds=0)

        mock_tune.assert_not_called()
        mock_queue.assert_not_called()

    @patch("opi.services.oom_watcher._queue_refresh_task", new_callable=AsyncMock)
    @patch("opi.services.deployment_observation.run_after_sync_observation", new_callable=AsyncMock)
    @patch("opi.services.oom_watcher.check_pod_health", new_callable=AsyncMock)
    @patch("opi.services.oom_watcher.get_project_data")
    @patch("opi.services.oom_watcher.get_prefixed_namespace", return_value="rig-prd-myproject")
    @pytest.mark.asyncio
    async def test_oom_detected_triggers_tune(self, mock_prefix, mock_get_data, mock_check, mock_tune, mock_queue):
        mock_get_data.return_value = (
            {
                "deployments": [
                    {
                        "name": "production",
                        "namespace": "myproject",
                        "cluster": "local",
                        "components": [{"reference": "api"}],
                    }
                ]
            },
            "myproject.yaml",
        )
        mock_check.return_value = PodHealthResult("production-api", oom_detected=True)
        mock_tune.return_value = MagicMock(requeue_refresh=True, failures=[])

        await _run_oom_check("myproject", "production", attempt=1, max_attempts=3, delay_seconds=0)

        # Routed through the generic after-sync scan with the OOM'd component's health.
        mock_tune.assert_called_once()
        args = mock_tune.call_args.args
        assert args[0] == "myproject"
        assert args[1] == "production"
        assert set(args[2]) == {"api"}
        assert args[2]["api"].oom_detected is True
        mock_queue.assert_called_once_with("myproject", "production")

    @patch("opi.services.oom_watcher._queue_refresh_task", new_callable=AsyncMock)
    @patch("opi.services.oom_watcher.disable_components_for_image_pull", new_callable=AsyncMock)
    @patch("opi.services.oom_watcher.check_pod_health", new_callable=AsyncMock)
    @patch("opi.services.oom_watcher.get_project_data")
    @patch("opi.services.oom_watcher.get_prefixed_namespace", return_value="rig-prd-myproject")
    @pytest.mark.asyncio
    async def test_image_pull_triggers_disable_and_queue(
        self, mock_prefix, mock_get_data, mock_check, mock_disable, mock_queue
    ):
        mock_get_data.return_value = (
            {
                "deployments": [
                    {
                        "name": "production",
                        "namespace": "myproject",
                        "cluster": "local",
                        "components": [{"reference": "api"}],
                    }
                ]
            },
            "myproject.yaml",
        )
        mock_check.return_value = PodHealthResult(
            "production-api", image_pull_error="ImagePullBackOff: Back-off pulling image"
        )

        await _run_oom_check("myproject", "production", attempt=1, max_attempts=3, delay_seconds=0)

        mock_disable.assert_called_once_with(
            "myproject", "production", [("api", "ImagePullBackOff: Back-off pulling image")]
        )
        mock_queue.assert_called_once_with("myproject", "production")

    @patch("opi.services.deployment_observation.run_after_sync_observation", new_callable=AsyncMock)
    @patch("opi.services.oom_watcher.check_pod_health", new_callable=AsyncMock)
    @patch("opi.services.oom_watcher.get_project_data")
    @pytest.mark.asyncio
    async def test_project_not_found_graceful(self, mock_get_data, mock_check, mock_tune):
        mock_get_data.side_effect = ValueError("Project not found")

        await _run_oom_check("missing-project", "production", attempt=1, max_attempts=3, delay_seconds=0)

        mock_check.assert_not_called()
        mock_tune.assert_not_called()

    @patch("opi.services.oom_watcher.check_pod_health", new_callable=AsyncMock)
    @patch("opi.services.oom_watcher.get_project_data")
    @patch("opi.services.oom_watcher.get_prefixed_namespace", return_value="rig-prd-myproject")
    @pytest.mark.asyncio
    async def test_disabled_components_skipped(self, mock_prefix, mock_get_data, mock_check):
        mock_get_data.return_value = (
            {
                "deployments": [
                    {
                        "name": "production",
                        "namespace": "myproject",
                        "cluster": "local",
                        "components": [{"reference": "api", "disabled": True, "disabled-reason": "ImagePullBackOff"}],
                    }
                ]
            },
            "myproject.yaml",
        )

        await _run_oom_check("myproject", "production", attempt=1, max_attempts=3, delay_seconds=0)

        mock_check.assert_not_called()


# ---------------------------------------------------------------------------
# The shared OOM tune budget across rounds
# ---------------------------------------------------------------------------


class TestOomTuneBudgetAcrossRounds:
    """One budget per deployment, and it survives the refresh a tune queues itself."""

    @staticmethod
    def _project_data():
        return (
            {
                "deployments": [
                    {
                        "name": "production",
                        "namespace": "myproject",
                        "cluster": "local",
                        "components": [{"reference": "api"}],
                    }
                ]
            },
            "myproject.yaml",
        )

    @patch("opi.services.deployment_observation.run_after_sync_observation", new_callable=AsyncMock)
    @patch("opi.services.oom_watcher.check_pod_health", new_callable=AsyncMock)
    @patch("opi.services.oom_watcher.get_project_data")
    @patch("opi.services.oom_watcher.get_prefixed_namespace", return_value="rig-prd-myproject")
    @patch("opi.services.oom_watcher.schedule_oom_check")
    @pytest.mark.asyncio
    async def test_budget_runs_out_over_automated_rounds(
        self, mock_schedule, mock_prefix, mock_get_data, mock_check, mock_observe
    ):
        """Four full OOM -> tune -> automated refresh rounds: only three may tune.

        Each round is what production did: the watcher detects an OOM, the tune
        commits, ``_queue_refresh_task`` queues a refresh carrying
        ``automated_remediation: True``, and the handler for that refresh schedules a
        new check starting at ``attempt=1``. The old code gated on that ``attempt``
        parameter, so every round handed itself a fresh budget and the ladder ran to
        the cluster ceiling. The budget must instead carry over the rounds.
        """
        from opi.services.oom_watcher import OOM_MAX_TUNE_ATTEMPTS, _oom_tune_attempts

        mock_get_data.side_effect = lambda _name: self._project_data()
        mock_observe.return_value = MagicMock(requeue_refresh=True, failures=[])

        task_service = AsyncMock()
        with patch("opi.services.oom_watcher._task_service_ref", task_service):
            for round_number in range(1, OOM_MAX_TUNE_ATTEMPTS + 2):
                # Each round the previous increase DID roll out, so the OOM comes from
                # a new pod generation. Only the budget may stop this loop here.
                mock_check.return_value = PodHealthResult(
                    "production-api", oom_detected=True, oom_pod_template_hash=f"gen-{round_number}"
                )
                # Every round starts a fresh chain at attempt=1, exactly as the
                # refresh handler does.
                await _run_oom_check("myproject", "production", attempt=1, max_attempts=3, delay_seconds=0)

                if round_number <= OOM_MAX_TUNE_ATTEMPTS:
                    assert mock_observe.call_count == round_number, f"round {round_number} must tune"
                    assert _oom_tune_attempts["myproject/production"] == round_number
                    payload = task_service.create_task.call_args.kwargs["payload"]
                    assert payload["automated_remediation"] is True, (
                        "the refresh a tune queues must be marked automated, so the handler "
                        "knows not to clear the budget"
                    )
                else:
                    assert mock_observe.call_count == OOM_MAX_TUNE_ATTEMPTS, (
                        "the round after the budget is spent must not tune"
                    )
                    assert _oom_tune_attempts["myproject/production"] == OOM_MAX_TUNE_ATTEMPTS

    @patch("opi.services.deployment_observation.run_after_sync_observation", new_callable=AsyncMock)
    @patch("opi.services.oom_watcher.check_pod_health", new_callable=AsyncMock)
    @patch("opi.services.oom_watcher.get_project_data")
    @patch("opi.services.oom_watcher.get_prefixed_namespace", return_value="rig-prd-myproject")
    @patch("opi.services.oom_watcher.schedule_oom_check")
    @pytest.mark.asyncio
    async def test_explicit_reset_gives_a_new_budget(
        self, mock_schedule, mock_prefix, mock_get_data, mock_check, mock_observe
    ):
        """A real new deploy resets the budget, so a later genuine OOM tunes again."""
        from opi.services.oom_watcher import OOM_MAX_TUNE_ATTEMPTS, _oom_tune_attempts, reset_inline_oom_attempts

        mock_get_data.side_effect = lambda _name: self._project_data()
        mock_check.return_value = PodHealthResult("production-api", oom_detected=True, oom_pod_template_hash="gen-1")
        mock_observe.return_value = MagicMock(requeue_refresh=True, failures=[])
        _oom_tune_attempts["myproject/production"] = OOM_MAX_TUNE_ATTEMPTS

        task_service = AsyncMock()
        with patch("opi.services.oom_watcher._task_service_ref", task_service):
            await _run_oom_check("myproject", "production", attempt=1, max_attempts=3, delay_seconds=0)
            mock_observe.assert_not_called()

            reset_inline_oom_attempts("myproject", "production")
            await _run_oom_check("myproject", "production", attempt=1, max_attempts=3, delay_seconds=0)

        mock_observe.assert_called_once()
        assert _oom_tune_attempts["myproject/production"] == 1


# ---------------------------------------------------------------------------
# The pod-template-hash lock
# ---------------------------------------------------------------------------


class TestPodGenerationLock:
    """An OOM on the pod generation a previous tune already answered is not new evidence.

    All twelve detections in the incident came from ONE pod
    (``pr-494-api-fb654fcc5-rcf6g``): the health error broke off the ArgoCD sync wait
    before the previous increase had rolled out, so the watcher kept re-reading the
    same unchanged pod. This lock is the net that would have stopped the escalation on
    its own, even without the counter fixes.
    """

    @staticmethod
    def _project_data():
        return (
            {
                "deployments": [
                    {
                        "name": "production",
                        "namespace": "myproject",
                        "cluster": "local",
                        "components": [{"reference": "api"}],
                    }
                ]
            },
            "myproject.yaml",
        )

    async def _two_detections(self, mock_get_data, mock_check, mock_observe, first_hash, second_hash):
        mock_get_data.side_effect = lambda _name: self._project_data()
        mock_observe.return_value = MagicMock(requeue_refresh=True, failures=[])

        task_service = AsyncMock()
        with patch("opi.services.oom_watcher._task_service_ref", task_service):
            for pod_hash in (first_hash, second_hash):
                mock_check.return_value = PodHealthResult(
                    "production-api", oom_detected=True, oom_pod_template_hash=pod_hash
                )
                await _run_oom_check("myproject", "production", attempt=1, max_attempts=3, delay_seconds=0)
        return mock_observe.call_count

    @patch("opi.services.deployment_observation.run_after_sync_observation", new_callable=AsyncMock)
    @patch("opi.services.oom_watcher.check_pod_health", new_callable=AsyncMock)
    @patch("opi.services.oom_watcher.get_project_data")
    @patch("opi.services.oom_watcher.get_prefixed_namespace", return_value="rig-prd-myproject")
    @patch("opi.services.oom_watcher.schedule_oom_check")
    @pytest.mark.asyncio
    async def test_same_generation_tunes_once(self, mock_sched, mock_prefix, mock_get_data, mock_check, mock_observe):
        """Twice the same pod-template-hash: exactly one tune, then wait for the rollout."""
        tunes = await self._two_detections(mock_get_data, mock_check, mock_observe, "fb654fcc5", "fb654fcc5")
        assert tunes == 1

    @patch("opi.services.deployment_observation.run_after_sync_observation", new_callable=AsyncMock)
    @patch("opi.services.oom_watcher.check_pod_health", new_callable=AsyncMock)
    @patch("opi.services.oom_watcher.get_project_data")
    @patch("opi.services.oom_watcher.get_prefixed_namespace", return_value="rig-prd-myproject")
    @patch("opi.services.oom_watcher.schedule_oom_check")
    @pytest.mark.asyncio
    async def test_new_generation_tunes_again(self, mock_sched, mock_prefix, mock_get_data, mock_check, mock_observe):
        """A changed hash means the increase rolled out and still OOMs: tune again."""
        tunes = await self._two_detections(mock_get_data, mock_check, mock_observe, "fb654fcc5", "7d9c1a2b4")
        assert tunes == 2

    @patch("opi.services.deployment_observation.run_after_sync_observation", new_callable=AsyncMock)
    @patch("opi.services.oom_watcher.check_pod_health", new_callable=AsyncMock)
    @patch("opi.services.oom_watcher.get_project_data")
    @patch("opi.services.oom_watcher.get_prefixed_namespace", return_value="rig-prd-myproject")
    @patch("opi.services.oom_watcher.schedule_oom_check")
    @pytest.mark.asyncio
    async def test_unknown_hash_does_not_block(self, mock_sched, mock_prefix, mock_get_data, mock_check, mock_observe):
        """A hash that cannot be determined must NOT block the tune.

        Deliberate choice: blocking on an unknown hash would silence the auto-tune the
        moment kubectl hiccups, which is worse than one tune too many.
        """
        tunes = await self._two_detections(mock_get_data, mock_check, mock_observe, None, None)
        assert tunes == 2

    @patch("opi.services.oom_watcher.check_all_components_health", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_inline_path_uses_the_same_lock(self, mock_check):
        """The inline callback observes the same rule as the background check."""
        mock_check.return_value = [PodHealthResult("comp-a", oom_detected=True, oom_pod_template_hash="fb654fcc5")]
        callback = create_health_check_callback("myproject", "production", "rig-prd-ns", ["comp-a"], grace_seconds=0)

        with pytest.raises(DeploymentHealthError) as exc_info:
            await callback(5)
        assert exc_info.value.failures[0].failure_type == "oom"

        # Same generation again: no OOM failure, so no second tune cycle.
        await callback(10)

    @patch("opi.services.oom_watcher.KubectlConnector")
    @pytest.mark.asyncio
    async def test_check_pod_health_reports_the_generation(self, mock_kubectl_cls):
        """The hash comes off the pod the OOM was actually observed on."""
        mock_kubectl = MagicMock()
        mock_kubectl_cls.return_value = mock_kubectl
        mock_kubectl_cls.isConnected = True
        oom_pod = {
            "metadata": {
                "name": "prod-api-fb654fcc5-rcf6g",
                "labels": {"app": "prod-api", "pod-template-hash": "fb654fcc5"},
            },
            "status": {
                "containerStatuses": [
                    {"name": "app", "lastState": {"terminated": {"reason": "OOMKilled", "exitCode": 137}}, "state": {}}
                ]
            },
        }
        mock_kubectl.run_command = _kubectl_returning(
            pods=[oom_pod], replicasets=[_replicaset(pod_template_hash="fb654fcc5", revision="3")]
        )

        result = await check_pod_health("rig-prd-ns", "prod-api")

        assert result.oom_detected is True
        assert result.oom_pod_template_hash == "fb654fcc5"


# ---------------------------------------------------------------------------
# schedule_oom_check
# ---------------------------------------------------------------------------


class TestScheduleOomCheck:
    """Tests for the public schedule_oom_check function."""

    @patch("opi.services.oom_watcher.settings")
    def test_disabled_returns_none(self, mock_settings):
        mock_settings.OOM_WATCHER_ENABLED = False

        result = schedule_oom_check("myproject", "production")
        assert result is None

    @patch("opi.services.oom_watcher.settings")
    def test_max_attempts_exceeded_returns_none(self, mock_settings):
        mock_settings.OOM_WATCHER_ENABLED = True
        mock_settings.OOM_WATCHER_DELAY_SECONDS = 10
        mock_settings.OOM_WATCHER_MAX_ATTEMPTS = 3

        result = schedule_oom_check("myproject", "production", attempt=4)
        assert result is None

    @patch("opi.services.oom_watcher.settings")
    @pytest.mark.asyncio
    async def test_schedules_task(self, mock_settings):
        mock_settings.OOM_WATCHER_ENABLED = True
        mock_settings.OOM_WATCHER_DELAY_SECONDS = 0
        mock_settings.OOM_WATCHER_MAX_ATTEMPTS = 3

        with patch("opi.services.oom_watcher._run_oom_check", new_callable=AsyncMock) as mock_run:
            task = schedule_oom_check("myproject", "production", delay_seconds=0, attempt=1)
            assert task is not None
            assert isinstance(task, asyncio.Task)

            await task

            mock_run.assert_called_once_with("myproject", "production", 1, 3, 0)


# ---------------------------------------------------------------------------
# DeploymentHealthError
# ---------------------------------------------------------------------------


class TestDeploymentHealthError:
    """Tests for the unified error type."""

    def test_contains_failures(self):
        failures = [
            ComponentFailure("comp-a", "oom", "OOM kill detected"),
            ComponentFailure("comp-b", "crash_loop", "CrashLoopBackOff"),
        ]
        error = DeploymentHealthError(failures, "rig-prd-ns")

        assert len(error.failures) == 2
        assert error.namespace == "rig-prd-ns"
        assert "comp-a: oom" in str(error)
        assert "comp-b: crash_loop" in str(error)


# ---------------------------------------------------------------------------
# _queue_refresh_task
# ---------------------------------------------------------------------------


class TestQueueRefreshTask:
    """Tests for the task queue helper."""

    @pytest.mark.asyncio
    async def test_queues_task_when_service_available(self):
        mock_service = AsyncMock()
        with patch("opi.services.oom_watcher._task_service_ref", mock_service):
            await _queue_refresh_task("myproject", "production")

        mock_service.create_task.assert_called_once()
        call_kwargs = mock_service.create_task.call_args
        assert call_kwargs.kwargs["task_type"] == "refresh_deployment"
        assert call_kwargs.kwargs["project_name"] == "myproject"
        assert call_kwargs.kwargs["deployment_name"] == "production"

    @pytest.mark.asyncio
    async def test_logs_warning_when_service_unavailable(self):
        with patch("opi.services.oom_watcher._task_service_ref", None):
            # Should not raise
            await _queue_refresh_task("myproject", "production")


# ---------------------------------------------------------------------------
# _run_oom_check — tune failure error path
# ---------------------------------------------------------------------------


class TestRunOomCheckTuneFailure:
    """Tests for error handling when tune_deployment_resources raises."""

    @patch("opi.services.oom_watcher._queue_refresh_task", new_callable=AsyncMock)
    @patch("opi.services.deployment_observation.run_after_sync_observation", new_callable=AsyncMock)
    @patch("opi.services.oom_watcher.check_pod_health", new_callable=AsyncMock)
    @patch("opi.services.oom_watcher.get_project_data")
    @patch("opi.services.oom_watcher.get_prefixed_namespace", return_value="rig-prd-myproject")
    @pytest.mark.asyncio
    async def test_tune_failure_does_not_crash(self, mock_prefix, mock_get_data, mock_check, mock_tune, mock_queue):
        """When tune raises an exception, the watcher should not crash."""
        mock_get_data.return_value = (
            {
                "deployments": [
                    {
                        "name": "production",
                        "namespace": "myproject",
                        "cluster": "local",
                        "components": [{"reference": "api"}],
                    }
                ]
            },
            "myproject.yaml",
        )
        mock_check.return_value = PodHealthResult("production-api", oom_detected=True)
        mock_tune.side_effect = RuntimeError("Metrics unavailable")

        # Should not raise
        await _run_oom_check("myproject", "production", attempt=1, max_attempts=3, delay_seconds=0)

        mock_queue.assert_not_called()


# ---------------------------------------------------------------------------
# _describe_pod_waiting: plain-language "what are we waiting on" reasons
# ---------------------------------------------------------------------------


def _pod(*, phase="Running", conditions=None, container_statuses=None) -> dict:
    return {
        "status": {
            "phase": phase,
            "conditions": conditions or [],
            "containerStatuses": container_statuses or [],
        }
    }


class TestDescribePodWaiting:
    def test_unschedulable_pending_pod(self):
        pod = _pod(
            phase="Pending",
            conditions=[{"type": "PodScheduled", "status": "False", "message": "Insufficient memory"}],
        )
        reason = _describe_pod_waiting(pod)
        assert reason is not None
        assert "kan niet worden ingepland" in reason
        assert "Insufficient memory" in reason

    def test_image_pull_passes_raw_reason_and_message(self):
        pod = _pod(
            container_statuses=[
                {"name": "app", "state": {"waiting": {"reason": "ImagePullBackOff", "message": "not found"}}}
            ]
        )
        reason = _describe_pod_waiting(pod)
        assert reason is not None
        assert "image ophalen mislukt" in reason
        assert "ImagePullBackOff" in reason
        assert "not found" in reason

    def test_crash_loop(self):
        pod = _pod(
            container_statuses=[
                {"name": "app", "state": {"waiting": {"reason": "CrashLoopBackOff", "message": "back-off 5m"}}}
            ]
        )
        reason = _describe_pod_waiting(pod)
        assert reason is not None
        assert "blijft herstarten" in reason

    def test_container_creating(self):
        pod = _pod(container_statuses=[{"name": "app", "state": {"waiting": {"reason": "ContainerCreating"}}}])
        assert _describe_pod_waiting(pod) == "container wordt aangemaakt"

    def test_unknown_waiting_reason_is_passed_through(self):
        pod = _pod(
            container_statuses=[
                {
                    "name": "app",
                    "state": {"waiting": {"reason": "CreateContainerConfigError", "message": "secret missing"}},
                }
            ]
        )
        reason = _describe_pod_waiting(pod)
        assert reason == "CreateContainerConfigError: secret missing"

    def test_running_but_not_ready(self):
        pod = _pod(container_statuses=[{"name": "app", "ready": False, "state": {"running": {}}}])
        reason = _describe_pod_waiting(pod)
        assert reason is not None
        assert "readiness-check" in reason

    def test_no_container_statuses_yet(self):
        pod = _pod(container_statuses=[])
        assert _describe_pod_waiting(pod) == "bezig met opstarten"

    def test_ready_pod_returns_none(self):
        pod = _pod(container_statuses=[{"name": "app", "ready": True, "state": {"running": {}}}])
        assert _describe_pod_waiting(pod) is None
