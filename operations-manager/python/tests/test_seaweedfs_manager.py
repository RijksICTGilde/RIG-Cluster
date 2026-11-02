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
        self.klonen_gemeld: list[tuple[str, str]] = []

    async def get_name(self) -> str:
        return "proj"

    def _add_secret_to_create(self, deployment_name: str, secret_type: str, secret_data: Any) -> None:
        self.secrets[(deployment_name, secret_type)] = secret_data

    def report_clone_performed(self, deployment_name: str, service_type: str, generation: int | None = None) -> None:
        self.klonen_gemeld.append((deployment_name, service_type))


@pytest.fixture(autouse=True)
def _cluster_heeft_seaweedfs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "CLUSTER_MANAGER", "sandboxed-local")
    monkeypatch.setattr(settings, "SEAWEEDFS_FILER_ADDRESS", "seaweedfs:8888")
    monkeypatch.setattr(settings, "SEAWEEDFS_MASTER_ADDRESS", "seaweedfs:9333")


def _connector(monkeypatch: pytest.MonkeyPatch, *, user_exists: bool = False, versioning: bool = False) -> AsyncMock:
    connector = AsyncMock()
    connector.user_exists.return_value = user_exists
    # Dicht, want een open S3-poort is een weigering om te provisioneren (rij G14).
    connector.anonymous_access_allowed.return_value = False
    connector.get_versioning.return_value = versioning
    connector.ensure_noncurrent_version_rule.return_value = {"status": "created"}
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
        # get_versioning als laatste: de grens op oude versies vraagt de staat van de bucket,
        # en zonder versioning krijgt hij geen regel (RC-380).
        assert namen == [
            "anonymous_access_allowed",
            "delete_user",
            "create_user",
            "create_bucket",
            "grant_bucket_access",
            "get_versioning",
        ]
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

    async def test_de_grens_op_oude_versies_alleen_op_een_bucket_met_versioning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        connector = _connector(monkeypatch, versioning=False)
        await SeaweedfsManager(NepProjectManager()).create_resources_for_deployment(PROJECT, DEPLOYMENT)
        connector.ensure_noncurrent_version_rule.assert_not_awaited()

        connector = _connector(monkeypatch, versioning=True)
        project = {
            **PROJECT,
            "services": [{"name": "seaweedfs-storage", "config": {"max-noncurrent-versions": 5}}],
        }
        await SeaweedfsManager(NepProjectManager()).create_resources_for_deployment(project, DEPLOYMENT)
        connector.ensure_noncurrent_version_rule.assert_awaited_once_with("proj-dep", 5, None)

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
        deployment = {**DEPLOYMENT, "cluster": "local"}
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

    async def test_de_dienst_van_een_deployment_afhalen_markeert(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Uitgesteld opruimen zoals bij minio: de purge in opi/jobs/reconciliation.py
        # haalt ze op na de bewaartermijn.
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
        gemarkeerd = {
            (aanroep.kwargs["resource_type"], aanroep.kwargs["resource_name"])
            for aanroep in marked.mark_resource.await_args_list
        }
        assert gemarkeerd == {
            ("seaweedfs_bucket", "proj-dep"),
            ("seaweedfs_user", "proj_dep"),
            ("seaweedfs_policy", "proj_dep-proj-dep-policy"),
        }
        connector.delete_bucket.assert_not_awaited()

    async def test_zonder_markeringsdienst_gaat_het_direct(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # De verwijderflow van een heel project geeft geen markeringsdienst mee.
        connector = _connector(monkeypatch)
        resultaat = await SeaweedfsManager(NepProjectManager()).handle_service_removal(
            project_name="proj",
            deployment_name="dep",
            deployment_data=DEPLOYMENT,
            project_data=PROJECT,
            marked_for_deletion_service=None,
        )
        assert resultaat["trigger"] == "service_removal"
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


#: De bron-deployment waar een kloon in de volgende tests naar verwijst: moet in het
#: projectbestand staan, want resolve_clone_source_deployment zoekt hem daar op (RC-361).
BRON_DEPLOYMENT = {"name": "bron", "cluster": "sandboxed-local", "namespace": "proj", "components": []}


def _project_met_kloon(clone_from: dict[str, Any]) -> dict[str, Any]:
    doel = {**DEPLOYMENT, "clone-from": clone_from}
    return {**PROJECT, "deployments": [doel, BRON_DEPLOYMENT]}, doel


class TestKlonen:
    """``clone-from`` na het provisioneren: de bucket, identiteit en policy staan al klaar."""

    def _mc(self, monkeypatch: pytest.MonkeyPatch, *, alias_ok: bool = True) -> AsyncMock:
        monkeypatch.setattr(settings, "SEAWEEDFS_ADMIN_ACCESS_KEY", "admin")
        monkeypatch.setattr(settings, "SEAWEEDFS_ADMIN_SECRET_KEY", "adminwachtwoord")
        mc = AsyncMock()
        mc.configure_alias.return_value = alias_ok
        monkeypatch.setattr("opi.manager.seaweedfs_manager.create_minio_connector", lambda: mc)
        return mc

    def _backup_items(self, monkeypatch: pytest.MonkeyPatch, items: list[dict[str, Any]]) -> AsyncMock:
        """De onderdelen van de backup-run komen van Kopia, dus hier van een mock."""
        opzoeken = AsyncMock(return_value=items)
        monkeypatch.setattr("opi.manager.seaweedfs_manager.clone_backup_items", opzoeken)
        return opzoeken

    async def test_kloon_van_een_andere_deployment_kopieert_de_bucket(self, monkeypatch: pytest.MonkeyPatch) -> None:
        connector = _connector(monkeypatch)
        connector.list_buckets.return_value = ["proj-bron"]
        mc = self._mc(monkeypatch)
        project, doel = _project_met_kloon({"type": "deployment", "reference": "bron", "mode": "once"})
        project_manager = NepProjectManager()

        await SeaweedfsManager(project_manager).create_resources_for_deployment(project, doel)

        connector.list_buckets.assert_awaited_once()
        mc.configure_alias.assert_awaited_once_with(
            alias="seaweedfs-admin",
            host="seaweedfs.rig-system.svc.cluster.local:8333",
            access_key="admin",
            secret_key="adminwachtwoord",
            secure=False,
            region=settings.MINIO_REGION,
        )
        mc.mirror_bucket_cross_alias.assert_awaited_once_with(
            "seaweedfs-admin", "proj-bron", "seaweedfs-admin", "proj-dep"
        )
        assert project_manager.klonen_gemeld == [("dep", "seaweedfs-storage")]

    async def test_kloon_van_een_niet_bestaande_bronbucket_geeft_een_fout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        connector = _connector(monkeypatch)
        connector.list_buckets.return_value = []
        self._mc(monkeypatch)
        project, doel = _project_met_kloon({"type": "deployment", "reference": "bron", "mode": "once"})

        with pytest.raises(RuntimeError, match="bestaat niet"):
            await SeaweedfsManager(NepProjectManager()).create_resources_for_deployment(project, doel)

    async def test_kloon_van_zichzelf_geeft_de_validatiefout_door(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _connector(monkeypatch)
        self._mc(monkeypatch)
        project, doel = _project_met_kloon({"type": "deployment", "reference": "dep", "mode": "once"})

        with pytest.raises(ValueError, match="clones from itself"):
            await SeaweedfsManager(NepProjectManager()).create_resources_for_deployment(project, doel)

    async def test_afgeronde_once_kloon_wordt_overgeslagen(self, monkeypatch: pytest.MonkeyPatch) -> None:
        connector = _connector(monkeypatch)
        mc = self._mc(monkeypatch)
        project, doel = _project_met_kloon(
            {"type": "deployment", "reference": "bron", "mode": "once", "status": {"completed": True}}
        )

        await SeaweedfsManager(NepProjectManager()).create_resources_for_deployment(project, doel)

        connector.list_buckets.assert_not_awaited()
        mc.configure_alias.assert_not_awaited()

    async def test_force_clone_draait_een_afgeronde_once_kloon_opnieuw(self, monkeypatch: pytest.MonkeyPatch) -> None:
        connector = _connector(monkeypatch)
        connector.list_buckets.return_value = ["proj-bron"]
        self._mc(monkeypatch)
        project, doel = _project_met_kloon(
            {"type": "deployment", "reference": "bron", "mode": "once", "status": {"completed": True}}
        )

        await SeaweedfsManager(NepProjectManager()).create_resources_for_deployment(project, doel, force_clone=True)

        connector.list_buckets.assert_awaited_once()

    async def test_clone_from_remote_source_wordt_geweigerd_als_migratie(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _connector(monkeypatch)
        self._mc(monkeypatch)
        project, doel = _project_met_kloon({"type": "remote-source", "reference": "iets", "mode": "once"})

        with pytest.raises(RuntimeError, match="migratie"):
            await SeaweedfsManager(NepProjectManager()).create_resources_for_deployment(project, doel)

    async def test_onbekend_clone_from_type_geeft_een_fout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _connector(monkeypatch)
        self._mc(monkeypatch)
        project, doel = _project_met_kloon({"type": "weet-ik-veel", "mode": "once"})

        with pytest.raises(ValueError, match="onbekend clone-from type"):
            await SeaweedfsManager(NepProjectManager()).create_resources_for_deployment(project, doel)

    async def test_clone_from_dat_geen_dict_is_geeft_een_typefout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _connector(monkeypatch)
        self._mc(monkeypatch)
        doel = {**DEPLOYMENT, "clone-from": "bron"}
        project = {**PROJECT, "deployments": [doel, BRON_DEPLOYMENT]}

        with pytest.raises(TypeError, match="moet een dict"):
            await SeaweedfsManager(NepProjectManager()).create_resources_for_deployment(project, doel)

    async def test_zonder_admin_sleutelpaar_kan_de_kloon_niet(self, monkeypatch: pytest.MonkeyPatch) -> None:
        connector = _connector(monkeypatch)
        connector.list_buckets.return_value = ["proj-bron"]
        monkeypatch.setattr(settings, "SEAWEEDFS_ADMIN_ACCESS_KEY", "")
        monkeypatch.setattr(settings, "SEAWEEDFS_ADMIN_SECRET_KEY", "")
        project, doel = _project_met_kloon({"type": "deployment", "reference": "bron", "mode": "once"})

        with pytest.raises(RuntimeError, match="SEAWEEDFS_ADMIN_ACCESS_KEY"):
            await SeaweedfsManager(NepProjectManager()).create_resources_for_deployment(project, doel)

    async def test_een_mislukte_mc_alias_geeft_een_fout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        connector = _connector(monkeypatch)
        connector.list_buckets.return_value = ["proj-bron"]
        self._mc(monkeypatch, alias_ok=False)
        project, doel = _project_met_kloon({"type": "deployment", "reference": "bron", "mode": "once"})

        with pytest.raises(RuntimeError, match="mc-alias"):
            await SeaweedfsManager(NepProjectManager()).create_resources_for_deployment(project, doel)

    async def test_kloon_uit_backup_zonder_bucket_item_doet_niets(self, monkeypatch: pytest.MonkeyPatch) -> None:
        connector = _connector(monkeypatch)
        self._mc(monkeypatch)
        self._backup_items(monkeypatch, [])
        project, doel = _project_met_kloon({"type": "backup", "reference": "bron", "mode": "once", "run": "run-1"})

        await SeaweedfsManager(NepProjectManager()).create_resources_for_deployment(project, doel)

        connector.list_buckets.assert_not_awaited()

    async def test_kloon_uit_backup_herstelt_de_bucket(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _connector(monkeypatch)
        monkeypatch.setattr(settings, "SEAWEEDFS_ADMIN_ACCESS_KEY", "admin")
        monkeypatch.setattr(settings, "SEAWEEDFS_ADMIN_SECRET_KEY", "adminwachtwoord")
        restore_manager = AsyncMock()
        restore_manager.restore_bucket.return_value = type("R", (), {"success": True, "error": None})()
        monkeypatch.setattr("opi.manager.backup.bucket_backup.create_bucket_backup_manager", lambda: restore_manager)
        opzoeken = self._backup_items(
            monkeypatch, [{"resource_type": "bucket", "reference_name": "ref-1", "snapshot_id": "snap-1"}]
        )
        clone_from = {"type": "backup", "reference": "bron", "mode": "once", "run": "run-1"}
        project, doel = _project_met_kloon(clone_from)
        project_manager = NepProjectManager()

        await SeaweedfsManager(project_manager).create_resources_for_deployment(project, doel)

        restore_manager.restore_bucket.assert_awaited_once()
        kwargs = restore_manager.restore_bucket.await_args.kwargs
        assert kwargs["reference_name"] == "ref-1"
        assert kwargs["snapshot_id"] == "snap-1"
        assert kwargs["target_bucket_name"] == "proj-dep"
        assert kwargs["target_access_key"] == "admin"
        assert project_manager.klonen_gemeld == [("dep", "seaweedfs-storage")]
        opzoeken.assert_awaited_once_with(project, "proj", doel, clone_from)

    async def test_kloon_uit_backup_geeft_de_restorefout_door(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _connector(monkeypatch)
        restore_manager = AsyncMock()
        restore_manager.restore_bucket.return_value = type("R", (), {"success": False, "error": "kapot"})()
        monkeypatch.setattr("opi.manager.backup.bucket_backup.create_bucket_backup_manager", lambda: restore_manager)
        self._backup_items(monkeypatch, [{"resource_type": "bucket", "reference_name": "ref-1"}])
        project, doel = _project_met_kloon({"type": "backup", "reference": "bron", "mode": "once", "run": "run-1"})

        with pytest.raises(RuntimeError, match="kapot"):
            await SeaweedfsManager(NepProjectManager()).create_resources_for_deployment(project, doel)
