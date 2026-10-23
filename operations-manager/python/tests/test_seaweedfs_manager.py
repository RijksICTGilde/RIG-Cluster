"""Wat de manager per deployment aanmaakt en opruimt, met een nep-connector.

De volgorde doet mee: een identiteit zonder policy komt nergens bij, en een bucket zonder
eigenaar is bij SeaweedFS niet hetzelfde ding.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from opi.core.config import settings
from opi.handlers.project_file_handler import ProjectFileHandler
from opi.manager.seaweedfs_manager import SeaweedfsManager
from opi.utils.secrets import SeaweedFSSecret

PROJECT = {
    "name": "proj",
    "services": ["seaweedfs-storage"],
    "components": [{"name": "web", "services": ["seaweedfs-storage"]}],
    "deployments": [
        {"name": "dep", "cluster": "sandboxed-local", "namespace": "proj", "components": [{"reference": "web"}]}
    ],
}
DEPLOYMENT = PROJECT["deployments"][0]


class NepProjectManager:
    """Zo veel van ProjectManager als de manager aanraakt."""

    def __init__(self, bestaand_secret: dict[str, str] | None = None) -> None:
        self._project_file_handler = ProjectFileHandler()
        self._kubectl_connector = AsyncMock()
        self._kubectl_connector.get_secret.return_value = bestaand_secret
        self.secrets: dict[str, Any] = {}

    async def get_name(self) -> str:
        return "proj"

    def _add_secret_to_create(self, deployment_name: str, secret_type: str, secret_data: Any) -> None:
        self.secrets[(deployment_name, secret_type)] = secret_data


@pytest.fixture(autouse=True)
def _cluster_heeft_seaweedfs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "CLUSTER_MANAGER", "sandboxed-local")
    monkeypatch.setattr(settings, "SEAWEEDFS_FILER_ADDRESS", "seaweedfs:8888")
    monkeypatch.setattr(settings, "SEAWEEDFS_MASTER_ADDRESS", "seaweedfs:9333")


def _connector(monkeypatch: pytest.MonkeyPatch, *, user_exists: bool = False) -> AsyncMock:
    connector = AsyncMock()
    connector.user_exists.return_value = user_exists
    monkeypatch.setattr("opi.manager.seaweedfs_manager.create_seaweedfs_connector", lambda: connector)
    return connector


class TestAanmaken:
    async def test_nieuwe_deployment_krijgt_alles_in_de_juiste_volgorde(self, monkeypatch: pytest.MonkeyPatch) -> None:
        connector = _connector(monkeypatch)
        project_manager = NepProjectManager()
        await SeaweedfsManager(project_manager).create_resources_for_deployment(PROJECT, DEPLOYMENT)

        # Zonder bestaand secret is user_exists niet nodig; delete_user ervoor, want
        # SeaweedFS kent geen "wachtwoord vervangen" (rij G10).
        namen = [aanroep[0] for aanroep in connector.method_calls]
        assert namen == ["delete_user", "create_user", "create_bucket", "grant_bucket_access"]
        connector.create_user.assert_awaited_once()
        gebruiker, access_key, secret_key = connector.create_user.await_args.args
        assert gebruiker == access_key == "proj_dep"
        # De bucket krijgt de identiteit als eigenaar (rij G2).
        connector.create_bucket.assert_awaited_once_with("proj-dep", owner="proj_dep")
        connector.grant_bucket_access.assert_awaited_once_with("proj_dep", "proj-dep", "proj_dep-proj-dep-policy")

        secret = project_manager.secrets[("dep", "seaweedfs")]
        assert isinstance(secret, SeaweedFSSecret)
        assert (secret.host, secret.port, secret.bucket_name) == (
            "seaweedfs.rig-system.svc.cluster.local",
            8333,
            "proj-dep",
        )
        assert secret.secret_key == secret_key

    async def test_versioning_alleen_als_iemand_het_koos(self, monkeypatch: pytest.MonkeyPatch) -> None:
        connector = _connector(monkeypatch)
        await SeaweedfsManager(NepProjectManager()).create_resources_for_deployment(PROJECT, DEPLOYMENT)
        connector.set_versioning.assert_not_awaited()

        connector = _connector(monkeypatch)
        project = {**PROJECT, "services": [{"name": "seaweedfs-storage", "config": {"enable-versioning": True}}]}
        await SeaweedfsManager(NepProjectManager()).create_resources_for_deployment(project, DEPLOYMENT)
        connector.set_versioning.assert_awaited_once_with("proj-dep", True)

    async def test_een_geldig_bestaand_secret_maakt_niets_opnieuw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        connector = _connector(monkeypatch, user_exists=True)
        bestaand = SeaweedFSSecret(
            host="seaweedfs.rig-system.svc.cluster.local",
            port=8333,
            access_key="proj_dep",
            secret_key="BlijftStaan123",
            bucket_name="proj-dep",
        ).to_k8s_secret_data()
        project_manager = NepProjectManager(bestaand_secret=bestaand)
        await SeaweedfsManager(project_manager).create_resources_for_deployment(PROJECT, DEPLOYMENT)

        connector.create_user.assert_not_awaited()
        connector.delete_user.assert_not_awaited()
        assert project_manager.secrets[("dep", "seaweedfs")].secret_key == "BlijftStaan123"

    async def test_een_secret_zonder_identiteit_erachter_wordt_vervangen(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Een verse SeaweedFS naast een blijvend secret: precies waar
        # _validate_and_fix_credentials bij MinIO voor bestaat (rij G10).
        connector = _connector(monkeypatch, user_exists=False)
        bestaand = SeaweedFSSecret(
            host="h", port=8333, access_key="proj_dep", secret_key="Verlopen123", bucket_name="proj-dep"
        ).to_k8s_secret_data()
        project_manager = NepProjectManager(bestaand_secret=bestaand)
        await SeaweedfsManager(project_manager).create_resources_for_deployment(PROJECT, DEPLOYMENT)

        connector.create_user.assert_awaited_once()
        assert project_manager.secrets[("dep", "seaweedfs")].secret_key != "Verlopen123"

    async def test_een_deployment_zonder_de_dienst_krijgt_niets(self, monkeypatch: pytest.MonkeyPatch) -> None:
        connector = _connector(monkeypatch)
        project = {**PROJECT, "components": [{"name": "web", "services": ["minio-storage"]}]}
        project_manager = NepProjectManager()
        await SeaweedfsManager(project_manager).create_resources_for_deployment(project, DEPLOYMENT)
        assert connector.method_calls == []
        assert project_manager.secrets == {}

    async def test_een_cluster_zonder_seaweedfs_geeft_een_fout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _connector(monkeypatch)
        deployment = {**DEPLOYMENT, "cluster": "odcn-production"}
        project = {**PROJECT, "deployments": [deployment]}
        with pytest.raises(RuntimeError, match="levert seaweedfs-storage niet"):
            await SeaweedfsManager(NepProjectManager()).create_resources_for_deployment(project, deployment)


class TestOpruimen:
    async def test_alles_gaat_weg_in_de_omgekeerde_volgorde(self, monkeypatch: pytest.MonkeyPatch) -> None:
        connector = _connector(monkeypatch)
        resultaat = await SeaweedfsManager(NepProjectManager()).delete_resources_for_deployment(PROJECT, DEPLOYMENT)

        assert [aanroep[0] for aanroep in connector.method_calls] == [
            "detach_policy",
            "delete_policy",
            "delete_user",
            "delete_bucket",
        ]
        assert resultaat["success"] is True
        assert [op["type"] for op in resultaat["operations"]] == [
            "seaweedfs_policy_detach",
            "seaweedfs_policy",
            "seaweedfs_user",
            "seaweedfs_bucket",
        ]

    async def test_de_dienst_van_een_deployment_afhalen_ruimt_direct_op(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Geen uitgestelde verwijdering in de proef, ook niet met een
        # marked_for_deletion_service.
        connector = _connector(monkeypatch)
        marked = AsyncMock()
        resultaat = await SeaweedfsManager(NepProjectManager()).handle_service_removal(
            project_name="proj",
            deployment_name="dep",
            deployment_data=DEPLOYMENT,
            project_data=PROJECT,
            marked_for_deletion_service=marked,
        )
        assert resultaat["trigger"] == "service_removal"
        marked.mark_resource.assert_not_awaited()
        connector.delete_bucket.assert_awaited_once_with("proj-dep")

    async def test_een_project_zonder_de_dienst_wordt_overgeslagen(self, monkeypatch: pytest.MonkeyPatch) -> None:
        connector = _connector(monkeypatch)
        project = {**PROJECT, "services": [], "components": [{"name": "web", "services": []}]}
        resultaat = await SeaweedfsManager(NepProjectManager()).delete_resources_for_deployment(project, DEPLOYMENT)
        assert resultaat["operations"][0]["status"] == "skipped"
        assert connector.method_calls == []

    async def test_een_mislukte_stap_wordt_gemeld_en_stopt_de_rest_niet(self, monkeypatch: pytest.MonkeyPatch) -> None:
        connector = _connector(monkeypatch)
        connector.delete_policy.side_effect = RuntimeError("boem")
        resultaat = await SeaweedfsManager(NepProjectManager()).delete_resources_for_deployment(PROJECT, DEPLOYMENT)
        assert resultaat["success"] is False
        assert "boem" in resultaat["errors"][0]
        connector.delete_bucket.assert_awaited_once()
