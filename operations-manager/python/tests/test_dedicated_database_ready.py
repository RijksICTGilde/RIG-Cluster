"""A dedicated PostgreSQL cluster is only done once it accepts connections.

ArgoCD calls a CNPG cluster healthy as soon as the pod is Ready. On 3 November 2026
provisioning of pocpl-0g8 ran straight into a ConnectionRefusedError on the -rw service
right after that signal, and the task failed permanently. These tests pin the wait that
sits in between, and the warning for a cluster the operator itself cannot reach.
"""

import inspect
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.core import progress_steps
from opi.manager import project_manager as project_manager_module
from opi.manager.project_manager import DATABASE_CONNECT_TIMEOUT_SECONDS, ProjectManager
from opi.services.postgres_scope import project_uses_dedicated_postgres
from opi.utils.connect_retry import retry_on_connect_error

HOST = "demo-db-rw.rig-prd-demo-infrastructure.svc.cluster.local"

# The status of pocpl-0g8 on 4 November 2026, 14 hours after creation: the pod ran and the
# application connected, but the operator never reached it.
NIET_READY_STATUS: dict[str, Any] = {
    "phase": "Instance Status Extraction Error: HTTP communication issue",
    "phaseReason": "The operator was unable to receive the status from all the ready instances.",
    "conditions": [
        {"type": "Ready", "status": "False", "reason": "ClusterIsNotReady"},
        {"type": "ConsistentSystemID", "status": "False"},
    ],
}


def _manager(status: dict[str, Any] | None = None) -> ProjectManager:
    manager = ProjectManager.__new__(ProjectManager)
    cluster = {"status": status} if status is not None else None
    manager._kubectl_connector = MagicMock(get_namespaced_resource=AsyncMock(return_value=cluster))
    return manager


class TestRetryOnOwnExceptionType:
    """``retry_on_connect_error`` rides out a connect failure on another protocol too."""

    @pytest.mark.asyncio
    async def test_retries_the_given_type_and_returns_the_success(self) -> None:
        attempts: list[int] = []

        async def operation() -> str:
            attempts.append(1)
            if len(attempts) < 3:
                raise ConnectionRefusedError("[Errno 111] Connect call failed")
            return "ok"

        with patch("opi.utils.connect_retry.asyncio.sleep", new=AsyncMock()) as sleep:
            assert await retry_on_connect_error(operation, 30, "db", connect_errors=(OSError,)) == "ok"

        assert len(attempts) == 3
        assert sleep.await_count == 2

    @pytest.mark.asyncio
    async def test_raises_the_same_type_once_the_budget_is_spent(self) -> None:
        async def operation() -> None:
            raise ConnectionRefusedError("refused")

        with (
            patch("opi.utils.connect_retry.asyncio.sleep", new=AsyncMock()),
            pytest.raises(ConnectionRefusedError),
        ):
            await retry_on_connect_error(operation, 3, "db", connect_errors=(OSError,))

    @pytest.mark.asyncio
    async def test_does_not_retry_another_error(self) -> None:
        attempts: list[int] = []

        async def operation() -> None:
            attempts.append(1)
            raise ValueError("not a connect failure")

        with pytest.raises(ValueError, match="not a connect failure"):
            await retry_on_connect_error(operation, 30, "db", connect_errors=(OSError,))
        assert len(attempts) == 1


class TestWaitUntilDatabaseAcceptsConnections:
    @pytest.mark.asyncio
    async def test_a_refused_connection_is_retried_until_it_is_accepted(self) -> None:
        manager = _manager()
        conn = MagicMock(fetchval=AsyncMock(return_value=1), close=AsyncMock())
        connect = AsyncMock(side_effect=[ConnectionRefusedError("refused"), ConnectionRefusedError("refused"), conn])

        with (
            patch("opi.manager.project_manager.asyncpg.connect", new=connect),
            patch("opi.utils.connect_retry.asyncio.sleep", new=AsyncMock()),
        ):
            await manager._wait_until_database_accepts_connections(
                project_name="demo", cluster_name="odcn-production", username="postgres", password="geheim"
            )

        assert connect.await_count == 3
        conn.fetchval.assert_awaited_once_with("SELECT 1")
        conn.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_deadline_without_a_connection_fails_the_task(self) -> None:
        manager = _manager(NIET_READY_STATUS)
        connect = AsyncMock(side_effect=ConnectionRefusedError("[Errno 111] Connect call failed"))

        with (
            patch("opi.manager.project_manager.asyncpg.connect", new=connect),
            patch("opi.utils.connect_retry.asyncio.sleep", new=AsyncMock()),
            pytest.raises(RuntimeError) as fout,
        ):
            await manager._wait_until_database_accepts_connections(
                project_name="demo", cluster_name="odcn-production", username="postgres", password="geheim"
            )

        melding = str(fout.value)
        assert HOST in melding
        assert str(DATABASE_CONNECT_TIMEOUT_SECONDS) in melding
        assert "Instance Status Extraction Error" in melding  # wat CNPG er zelf over zegt
        assert isinstance(fout.value.__cause__, ConnectionRefusedError)

    def test_only_the_dedicated_path_waits(self) -> None:
        """The shared rig-db never reaches this wait: it only lives in the dedicated flow."""
        source = inspect.getsource(project_manager_module)
        aanroepen = source.count("_wait_until_database_accepts_connections(")
        definitie_en_aanroep = 2  # the method itself plus the single call site
        assert aanroepen == definitie_en_aanroep
        assert "_wait_until_database_accepts_connections" in inspect.getsource(
            ProjectManager._create_infrastructure_resources
        )
        assert not project_uses_dedicated_postgres({"services": ["postgresql-database"]})


class TestClusterReadyWarning:
    @pytest.mark.asyncio
    async def test_a_cluster_the_operator_cannot_reach_warns_once_and_continues(self) -> None:
        manager = _manager(NIET_READY_STATUS)
        progress = MagicMock(add_subtask=MagicMock(return_value="sub"))

        with patch("opi.manager.project_manager.logger") as log:
            await manager._warn_when_database_cluster_not_ready(
                project_name="demo", cluster_name="odcn-production", progress_manager=progress, infra_task="taak"
            )

        log.warning.assert_called_once()
        assert "unable to receive the status" in log.warning.call_args.args[0]
        progress.add_subtask.assert_called_once()
        assert progress.add_subtask.call_args.args[1].startswith(progress_steps.database_not_ready(""))
        progress.complete_task.assert_called_once_with("sub")

    @pytest.mark.asyncio
    async def test_a_ready_cluster_is_silent(self) -> None:
        manager = _manager({"phase": "Cluster in healthy state", "conditions": [{"type": "Ready", "status": "True"}]})
        progress = MagicMock()

        with patch("opi.manager.project_manager.logger") as log:
            await manager._warn_when_database_cluster_not_ready(
                project_name="demo", cluster_name="odcn-production", progress_manager=progress, infra_task="taak"
            )

        log.warning.assert_not_called()
        progress.add_subtask.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_unreadable_cluster_stays_silent(self) -> None:
        """Absent and forbidden look the same through kubectl, and neither proves unhealthy."""
        manager = _manager(None)
        progress = MagicMock()

        with patch("opi.manager.project_manager.logger") as log:
            await manager._warn_when_database_cluster_not_ready(
                project_name="demo", cluster_name="odcn-production", progress_manager=progress, infra_task="taak"
            )

        log.warning.assert_not_called()
        progress.add_subtask.assert_not_called()


class TestSopsRetryLogLevels:
    @pytest.mark.asyncio
    async def test_transient_rounds_before_a_success_produce_no_error(self, caplog: pytest.LogCaptureFixture) -> None:
        manager = ProjectManager.__new__(ProjectManager)
        manager._kubectl_connector = MagicMock(
            get_sops_secret_from_namespace=AsyncMock(return_value=None), delete_resource=AsyncMock()
        )
        manager._sops_handler = MagicMock(
            store_project_sops_key_in_namespace=AsyncMock(side_effect=[False, False, False, True])
        )

        with (
            patch("opi.manager.project_manager.get_project_public_key", return_value="age1x"),
            patch("opi.manager.project_manager.get_decoded_project_private_key", new=AsyncMock(return_value="k")),
            patch("opi.manager.project_manager.asyncio.sleep", new=AsyncMock()),
            caplog.at_level("WARNING"),
        ):
            await manager._ensure_sops_secret_in_namespace("rig-prd-demo", {"name": "demo"})

        assert [r for r in caplog.records if r.levelname == "ERROR"] == []
        assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 3

    @pytest.mark.asyncio
    async def test_ten_failed_rounds_log_one_error_and_raise(self, caplog: pytest.LogCaptureFixture) -> None:
        manager = ProjectManager.__new__(ProjectManager)
        manager._kubectl_connector = MagicMock(
            get_sops_secret_from_namespace=AsyncMock(return_value=None), delete_resource=AsyncMock()
        )
        manager._sops_handler = MagicMock(store_project_sops_key_in_namespace=AsyncMock(return_value=False))

        with (
            patch("opi.manager.project_manager.get_project_public_key", return_value="age1x"),
            patch("opi.manager.project_manager.get_decoded_project_private_key", new=AsyncMock(return_value="k")),
            patch("opi.manager.project_manager.asyncio.sleep", new=AsyncMock()),
            caplog.at_level("WARNING"),
            pytest.raises(RuntimeError),
        ):
            await manager._ensure_sops_secret_in_namespace("rig-prd-demo", {"name": "demo"})

        errors = [r for r in caplog.records if r.levelname == "ERROR"]
        assert len(errors) == 1
        assert "after 10 attempts" in errors[0].getMessage()
