"""Tests for the deployment health watcher (formerly OOM watcher)."""

import asyncio
import json
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

    pod = {"metadata": metadata, "status": {"containerStatuses": container_statuses}}
    return json.dumps({"items": [pod]})


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

    def setup_method(self):
        from opi.services.oom_watcher import _inline_oom_attempts

        _inline_oom_attempts.clear()

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
        from opi.services.oom_watcher import OOM_INLINE_MAX_ATTEMPTS, _inline_oom_attempts

        _inline_oom_attempts["myproject/production"] = OOM_INLINE_MAX_ATTEMPTS
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
        from opi.services.oom_watcher import OOM_INLINE_MAX_ATTEMPTS, _inline_oom_attempts

        _inline_oom_attempts["myproject/production"] = OOM_INLINE_MAX_ATTEMPTS
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
        from opi.services.oom_watcher import OOM_INLINE_MAX_ATTEMPTS, _inline_oom_attempts

        _inline_oom_attempts["myproject/production"] = OOM_INLINE_MAX_ATTEMPTS
        mock_check.return_value = [PodHealthResult("comp-a", oom_detected=True)]

        callback = create_health_check_callback("myproject", "production", "rig-prd-ns", ["comp-a"], grace_seconds=0)
        assert callback is not None

        # OOM-only: must not raise now that the budget is spent
        await callback(10)

        # No OOM increment happened. The fresh-deploy pop runs at creation, so the
        # key is absent — what matters is the counter was not pushed past the cap.
        assert "myproject/production" not in _inline_oom_attempts

    @patch("opi.services.oom_watcher.check_all_components_health", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_image_pull_raises_but_oom_suppressed_in_same_batch(self, mock_check):
        """At the OOM cap, an image-pull failure in the same batch as an OOM
        component still raises — but the error carries only the image_pull
        failure, not the suppressed OOM."""
        from opi.services.oom_watcher import OOM_INLINE_MAX_ATTEMPTS, _inline_oom_attempts

        _inline_oom_attempts["myproject/production"] = OOM_INLINE_MAX_ATTEMPTS
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
        # OOM suppressed: no OOM increment, counter not pushed past the cap.
        assert "myproject/production" not in _inline_oom_attempts

    @patch("opi.services.oom_watcher.check_all_components_health", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_oom_cap_bounds_tuning_within_a_deploy(self, mock_check):
        """The OOM cap still bounds OOM auto-tune within a single deploy: each
        OOM raise increments the counter, and once it reaches the cap the OOM
        raise path stops (while the callback stays alive for other failures).

        Mirrors the production loop: each OOM->tune->refresh re-creates the
        callback, which reads the prior count before its fresh-deploy pop.
        """
        from opi.services.oom_watcher import OOM_INLINE_MAX_ATTEMPTS, _inline_oom_attempts

        mock_check.return_value = [PodHealthResult("comp-a", oom_detected=True)]

        # First OOM_INLINE_MAX_ATTEMPTS cycles each raise and bump the counter.
        for expected in range(1, OOM_INLINE_MAX_ATTEMPTS + 1):
            callback = create_health_check_callback(
                "myproject", "production", "rig-prd-ns", ["comp-a"], grace_seconds=0
            )
            with pytest.raises(DeploymentHealthError):
                await callback(5)
            assert _inline_oom_attempts["myproject/production"] == expected

        # Next cycle: budget exhausted -> OOM no longer raises, no further bump.
        # The creation pop clears the key; the cap was read from it beforehand.
        callback = create_health_check_callback("myproject", "production", "rig-prd-ns", ["comp-a"], grace_seconds=0)
        await callback(5)
        assert "myproject/production" not in _inline_oom_attempts

    @patch("opi.services.oom_watcher.check_all_components_health", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_fresh_deploy_resets_oom_budget(self, mock_check):
        """A genuinely fresh deploy (explicit counter reset) lets a later
        legitimate OOM auto-tune again."""
        from opi.services.oom_watcher import (
            OOM_INLINE_MAX_ATTEMPTS,
            _inline_oom_attempts,
            reset_inline_oom_attempts,
        )

        _inline_oom_attempts["myproject/production"] = OOM_INLINE_MAX_ATTEMPTS
        reset_inline_oom_attempts("myproject", "production")

        mock_check.return_value = [PodHealthResult("comp-a", oom_detected=True)]
        callback = create_health_check_callback("myproject", "production", "rig-prd-ns", ["comp-a"], grace_seconds=0)

        with pytest.raises(DeploymentHealthError) as exc_info:
            await callback(5)

        assert exc_info.value.failures[0].failure_type == "oom"
        assert _inline_oom_attempts["myproject/production"] == 1

    @patch("opi.services.oom_watcher.check_all_components_health", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_increments_oom_attempt_counter(self, mock_check):
        from opi.services.oom_watcher import _inline_oom_attempts

        mock_check.return_value = [PodHealthResult("comp-a", oom_detected=True)]
        callback = create_health_check_callback("myproject", "production", "rig-prd-ns", ["comp-a"], grace_seconds=0)

        with pytest.raises(DeploymentHealthError):
            await callback(5)

        assert _inline_oom_attempts["myproject/production"] == 1

    @patch("opi.services.oom_watcher.check_all_components_health", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_crash_loop_does_not_increment_oom_counter(self, mock_check):
        """CrashLoopBackOff should not count as an OOM attempt."""
        from opi.services.oom_watcher import _inline_oom_attempts

        mock_check.return_value = [
            PodHealthResult("comp-a", crash_loop_detected=True, crash_loop_message="CrashLoopBackOff")
        ]
        callback = create_health_check_callback("myproject", "production", "rig-prd-ns", ["comp-a"], grace_seconds=0)

        with pytest.raises(DeploymentHealthError):
            await callback(5)

        assert "myproject/production" not in _inline_oom_attempts

    def test_resets_counter_on_creation(self):
        from opi.services.oom_watcher import _inline_oom_attempts

        _inline_oom_attempts["myproject/production"] = 2

        create_health_check_callback("myproject", "production", "rig-prd-ns", ["comp-a"], grace_seconds=0)

        assert "myproject/production" not in _inline_oom_attempts


# ---------------------------------------------------------------------------
# _run_oom_check
# ---------------------------------------------------------------------------


class TestRunOomCheck:
    """Tests for the internal health check coroutine."""

    @patch("opi.services.oom_watcher._queue_refresh_task", new_callable=AsyncMock)
    @patch("opi.services.oom_watcher.tune_deployment_resources", new_callable=AsyncMock)
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
    @patch("opi.services.oom_watcher.tune_deployment_resources", new_callable=AsyncMock)
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
        mock_tune.return_value = MagicMock(changes=[{"component": "api"}])

        await _run_oom_check("myproject", "production", attempt=1, max_attempts=3, delay_seconds=0)

        mock_tune.assert_called_once_with("myproject", "production", skip_reprocessing=True)
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

    @patch("opi.services.oom_watcher.tune_deployment_resources", new_callable=AsyncMock)
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
    @patch("opi.services.oom_watcher.tune_deployment_resources", new_callable=AsyncMock)
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
