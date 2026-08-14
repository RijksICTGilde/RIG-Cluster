"""Tests for the report-first service-orphan sweep.

The sweep must never offer anything actively used for deletion:
- expected/system/unknown are never confirmable
- orphan candidates with active connections become in_use_anomaly
"""

from unittest.mock import AsyncMock, patch

import pytest
from opi.jobs.service_orphan_sweep import (
    CONFIRMABLE,
    _classify_database,
    _classify_project_realm_client,
    _live_deployments,
    sweep,
)

PROJECTS = {"regel-k4c": ["regelrecht", "pr781"], "waggl-9et": ["productie"]}
EXPECTED = {
    "postgresql_database": {
        ("regel_k4c_regelrecht", "odcn-production"),
        ("regel_k4c_pr781", "odcn-production"),
        ("waggl_9et_productie", "odcn-production"),
    },
}


class TestClassifyDatabase:
    def test_system_database(self) -> None:
        classification, _ = _classify_database("keycloak", "odcn-production", EXPECTED, PROJECTS)
        assert classification == "system"

    def test_expected_database(self) -> None:
        classification, _ = _classify_database("waggl_9et_productie", "odcn-production", EXPECTED, PROJECTS)
        assert classification == "expected"

    def test_dead_pr_database_is_orphan_candidate(self) -> None:
        classification, reason = _classify_database("regel_k4c_pr104", "odcn-production", EXPECTED, PROJECTS)
        assert classification == CONFIRMABLE
        assert "regel-k4c" in reason

    def test_clone_generation_remnant_is_orphan_candidate(self) -> None:
        classification, _ = _classify_database("regel_k4c_pr748_v1", "odcn-production", EXPECTED, PROJECTS)
        assert classification == CONFIRMABLE

    def test_unrecognized_name_is_unknown(self) -> None:
        classification, _ = _classify_database("some_random_db", "odcn-production", EXPECTED, PROJECTS)
        assert classification == "unknown"

    def test_expected_on_other_cluster_is_not_expected_here(self) -> None:
        """Cluster dimension must hold: same name on another cluster stays candidate."""
        classification, _ = _classify_database("waggl_9et_productie", "other-cluster", EXPECTED, PROJECTS)
        assert classification == CONFIRMABLE


class TestClassifyProjectRealmClient:
    def test_builtin_client_is_system(self) -> None:
        classification, _ = _classify_project_realm_client("realm-management", "regel-k4c", ["regelrecht"])
        assert classification == "system"

    def test_invites_client_is_system(self) -> None:
        classification, _ = _classify_project_realm_client("operations-manager-invites", "regel-k4c", ["regelrecht"])
        assert classification == "system"

    def test_live_deployment_client_is_expected(self) -> None:
        classification, _ = _classify_project_realm_client("regel-k4c-regelrecht", "regel-k4c", ["regelrecht"])
        assert classification == "expected"

    def test_live_deployment_public_client_is_expected(self) -> None:
        classification, _ = _classify_project_realm_client("regel-k4c-regelrecht-public", "regel-k4c", ["regelrecht"])
        assert classification == "expected"

    def test_dead_pr_client_is_orphan_candidate(self) -> None:
        classification, _ = _classify_project_realm_client("regel-k4c-pr250-public", "regel-k4c", ["regelrecht"])
        assert classification == CONFIRMABLE

    def test_foreign_client_is_unknown(self) -> None:
        classification, _ = _classify_project_realm_client("custom-integration", "regel-k4c", ["regelrecht"])
        assert classification == "unknown"


class TestLiveDeployments:
    def test_filters_by_cluster(self) -> None:
        yamls = [
            {
                "name": "proj",
                "deployments": [
                    {"name": "prod", "cluster": "odcn-production"},
                    {"name": "dev", "cluster": "sandboxed-local"},
                ],
            }
        ]
        result = _live_deployments(yamls, "odcn-production")
        assert result == {"proj": ["prod"]}


class TestPurgeKeycloakClient:
    @pytest.mark.asyncio
    async def test_purges_client_and_mark(self) -> None:
        from opi.jobs.reconciliation import _purge_keycloak_client

        mark = {
            "id": "mark-1",
            "resource_type": "keycloak_client",
            "resource_name": "regel-k4c-pr250-public",
            "metadata": {"realm": "regel-k4c-odcn-production"},
        }
        mock_service = AsyncMock()
        mock_keycloak = AsyncMock()
        mock_keycloak.delete_client_by_client_id = AsyncMock(return_value=True)
        results: dict = {"purged": [], "errors": []}

        with patch(
            "opi.connectors.keycloak.create_keycloak_connector",
            AsyncMock(return_value=mock_keycloak),
        ):
            await _purge_keycloak_client(mark, mock_service, results, dry_run=False)

        mock_keycloak.delete_client_by_client_id.assert_awaited_once_with(
            "regel-k4c-odcn-production", "regel-k4c-pr250-public"
        )
        mock_service.delete_mark.assert_awaited_once_with("mark-1")
        assert results["purged"] == [
            {"type": "keycloak_client", "name": "regel-k4c-pr250-public", "realm": "regel-k4c-odcn-production"}
        ]

    @pytest.mark.asyncio
    async def test_missing_realm_keeps_mark(self) -> None:
        from opi.jobs.reconciliation import _purge_keycloak_client

        mark = {
            "id": "mark-2",
            "resource_type": "keycloak_client",
            "resource_name": "some-client",
            "metadata": {},
        }
        mock_service = AsyncMock()
        results: dict = {"purged": [], "errors": []}

        await _purge_keycloak_client(mark, mock_service, results, dry_run=False)

        mock_service.delete_mark.assert_not_called()
        assert results["purged"] == []
        assert len(results["errors"]) == 1

    @pytest.mark.asyncio
    async def test_dry_run_does_not_touch_keycloak(self) -> None:
        from opi.jobs.reconciliation import _purge_keycloak_client

        mark = {
            "id": "mark-3",
            "resource_type": "keycloak_client",
            "resource_name": "regel-k4c-pr250",
            "metadata": {"realm": "regel-k4c-odcn-production"},
        }
        mock_service = AsyncMock()
        results: dict = {"purged": [], "errors": []}

        await _purge_keycloak_client(mark, mock_service, results, dry_run=True)

        mock_service.delete_mark.assert_not_called()
        assert len(results["purged"]) == 1


class TestConfirmEndpointSafety:
    """POST /orphans/confirm must reject everything that is not currently
    an orphan_candidate in a fresh sweep."""

    def _canned_report(self) -> dict:
        return {
            "databases": [
                {"name": "regel_k4c_pr104", "classification": "orphan_candidate", "reason": "dead PR"},
                {"name": "waggl_9et_productie", "classification": "expected", "reason": "live"},
                {"name": "ghost_db", "classification": "in_use_anomaly", "reason": "has connections"},
            ],
            "minio_buckets": [],
            "keycloak_clients": [
                {
                    "realm": "regel-k4c-odcn-production",
                    "client_id": "regel-k4c-pr250-public",
                    "public": True,
                    "classification": "orphan_candidate",
                    "reason": "dead PR",
                },
            ],
        }

    @pytest.mark.asyncio
    async def test_only_current_orphan_candidates_accepted(self, monkeypatch) -> None:
        import opi.core.config
        from opi.api import admin_router as admin_module
        from opi.api import endpoint_util
        from opi.api.admin_router import confirm_orphans

        # admin_router and endpoint_util both bind ``settings`` by reference at
        # import time. In the full suite they may have been first-imported while
        # another test had opi.core.config.settings patched (mock_settings) or
        # reloaded (test_secret_key_failclosed), leaving their module-level
        # reference pointing at a stale mock object -- which then surfaces as a
        # 501 (ADMIN_API_KEY) or a MagicMock in the JSON response
        # (DELETION_GRACE_PERIOD_DAYS). Re-point both to the live settings, then
        # patch the attributes the endpoint reads.
        real_settings = opi.core.config.settings
        monkeypatch.setattr(admin_module, "settings", real_settings)
        monkeypatch.setattr(endpoint_util, "settings", real_settings)
        monkeypatch.setattr(real_settings, "ADMIN_API_KEY", "test-admin-key")
        monkeypatch.setattr(real_settings, "CLUSTER_MANAGER", "odcn-production")

        mock_request = AsyncMock()
        mock_request.headers = {"X-API-Key": "test-admin-key"}
        mock_request.json = AsyncMock(
            return_value={
                "items": [
                    {"type": "postgresql_database", "name": "regel_k4c_pr104"},
                    {"type": "postgresql_database", "name": "waggl_9et_productie"},
                    {"type": "postgresql_database", "name": "ghost_db"},
                    {"type": "postgresql_database", "name": "not_in_report"},
                    {
                        "type": "keycloak_client",
                        "name": "regel-k4c-pr250-public",
                        "realm": "regel-k4c-odcn-production",
                    },
                ]
            }
        )

        mock_service = AsyncMock()
        mock_project_service = AsyncMock()
        mock_project_service.get_all_projects = dict

        with (
            patch("opi.jobs.service_orphan_sweep.sweep", AsyncMock(return_value=self._canned_report())),
            patch("opi.services.project_service.get_project_service", return_value=mock_project_service),
            patch.object(admin_module, "_get_marked_for_deletion_service", return_value=mock_service),
        ):
            response = await confirm_orphans(request=mock_request)

        import json

        body = json.loads(response.body)
        accepted_names = {a["name"] for a in body["accepted"]}
        rejected_names = {r["name"] for r in body["rejected"]}

        assert accepted_names == {"regel_k4c_pr104", "regel-k4c-pr250-public"}
        assert rejected_names == {"waggl_9et_productie", "ghost_db", "not_in_report"}
        assert mock_service.mark_resource.await_count == 2

        # Keycloak client mark must carry the realm in metadata
        keycloak_call = next(
            c for c in mock_service.mark_resource.await_args_list if c.kwargs["resource_type"] == "keycloak_client"
        )
        assert keycloak_call.kwargs["metadata"]["realm"] == "regel-k4c-odcn-production"


class TestGitopsFolderInventory:
    """De GitOps-mappen horen in het rapport.

    Deze categorie ontbrak, en daardoor meldde niets dat er vijf verwijderde projecten
    hun map in zad-argo-user-applications hadden laten staan. De root-application maakte
    hun Application telkens opnieuw aan, die faalde op 'app path does not exist', en met
    retry limit -1 herhaalde dat zich elke 30 seconden - eindeloos, en met kubectl niet
    weg te krijgen omdat de app-of-apps hem meteen terugzette.
    """

    @pytest.mark.asyncio
    async def test_folder_without_project_is_an_orphan_candidate(self, tmp_path) -> None:
        cluster = "sandboxed-local"
        cluster_dir = tmp_path / cluster
        (cluster_dir / "leeft-abc").mkdir(parents=True)
        (cluster_dir / "weg-xyz").mkdir(parents=True)

        gitops = AsyncMock()
        gitops.refresh_working_tree = AsyncMock()
        gitops.get_working_dir = AsyncMock(return_value=str(tmp_path))

        with patch(
            "opi.jobs.service_orphan_sweep.create_git_connector_for_argocd",
            AsyncMock(return_value=gitops),
        ):
            report = await sweep([{"name": "leeft-abc", "deployments": []}], cluster=cluster)

        mappen = {entry["project"]: entry for entry in report["gitops_folders"]}
        assert mappen["leeft-abc"]["classification"] == "expected"
        assert mappen["weg-xyz"]["classification"] == CONFIRMABLE
        assert mappen["weg-xyz"]["path"] == f"{cluster}/weg-xyz"

    @pytest.mark.asyncio
    async def test_working_tree_is_refreshed_before_listing(self, tmp_path) -> None:
        """Zonder verversen wordt er geoordeeld op een checkout die willekeurig oud kan zijn."""
        (tmp_path / "sandboxed-local").mkdir()

        gitops = AsyncMock()
        gitops.refresh_working_tree = AsyncMock()
        gitops.get_working_dir = AsyncMock(return_value=str(tmp_path))

        with patch(
            "opi.jobs.service_orphan_sweep.create_git_connector_for_argocd",
            AsyncMock(return_value=gitops),
        ):
            await sweep([], cluster="sandboxed-local")

        gitops.refresh_working_tree.assert_awaited()
