"""Elke back-up- en restorepod krijgt een S3-account dat alleen bij zijn eigen bucket mag.

Het beheeraccount van de bestemming (``BACKUP_S3_ACCESS_KEY``) blijft bij OPI: een sleutel
in een projectnamespace kon tot RC-340 bij de buckets van alle projecten. Deze toetsen
houden vier dingen vast:

- het account per bucket bestaat, is idempotent te zetten en mag alleen bij die bucket;
- het geheim is afgeleid van het beheergeheim, dus OPI hoeft het niet te bewaren;
- elke pod krijgt dat account, en het beheeraccount alleen nog waar OPI zelf werkt;
- in single-modus blijft het beheeraccount in gebruik, met een waarschuwing.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from opi.connectors.minio_mc import MinioConnector
from opi.manager.backup.base import (
    _MINIO_SECRET_MAX_LENGTH,
    backup_bucket_account_name,
    derive_bucket_account_secret,
    get_backup_bucket_name,
)
from opi.manager.backup.pvc_backup import PVCBackupManager
from tests.test_backup_pod_service_account import POD_CREATORS, PROJECT, _manager

ADMIN_ACCESS_KEY = "a"
ADMIN_SECRET_KEY = "s"

_BACKUP_PACKAGE = Path(__file__).parent.parent / "opi" / "manager" / "backup"

# De plekken waar OPI zelf met de bestemming praat: bucket aanmaken, en Kopia vanuit OPI
# aanroepen om snapshots te lezen of te verwijderen. Alleen daar mag het beheeraccount nog.
ADMIN_KEY_IS_ALLOWED_IN = frozenset(
    {
        "_pod_s3_credentials",
        "_ensure_backup_bucket_exists",
        "list_snapshots",
        "list_snapshots_or_raise",
        "delete_snapshots",
        "list_bucket_snapshots",
        "list_database_snapshots",
    }
)


def _minio_connector(bucket_status: str = "created") -> MagicMock:
    connector = MagicMock(spec=MinioConnector)
    connector.configure_alias = AsyncMock(return_value=True)
    connector.create_bucket = AsyncMock(return_value={"status": bucket_status})
    connector.create_user = AsyncMock(return_value={"status": "created"})
    connector.grant_bucket_access = AsyncMock(return_value={"status": "granted"})
    return connector


class TestHetAccountPerBucket:
    @pytest.mark.asyncio
    async def test_twee_aanroepen_leveren_hetzelfde_account_met_dezelfde_rechten(self) -> None:
        """Idempotent: de tweede aanroep zet hetzelfde account en dezelfde policy terug."""
        manager = _manager(PVCBackupManager)
        connector = _minio_connector()

        with patch("opi.manager.backup.base.create_minio_connector", return_value=connector):
            first = await manager._ensure_backup_bucket_exists(project_name=PROJECT, cluster="sandboxed-local")
            connector.create_bucket.return_value = {"status": "exists"}
            second = await manager._ensure_backup_bucket_exists(project_name=PROJECT, cluster="sandboxed-local")

        assert first == second
        account = backup_bucket_account_name(first)
        assert [call.args[1] for call in connector.create_user.await_args_list] == [account, account]
        assert [call.args[1:] for call in connector.grant_bucket_access.await_args_list] == [
            (account, first, ["read", "write", "delete", "list"]),
            (account, first, ["read", "write", "delete", "list"]),
        ]

    @pytest.mark.asyncio
    async def test_de_policy_noemt_alleen_de_bucket_van_dit_project(self) -> None:
        """Getoetst op het policydocument dat de connector bouwt, niet op de aanroep ernaartoe."""
        connector = MinioConnector()
        connector.create_policy = AsyncMock(return_value={"status": "created"})  # type: ignore[method-assign]
        connector.attach_policy = AsyncMock(return_value={"status": "attached"})  # type: ignore[method-assign]

        await connector.grant_bucket_access(
            "backup-destination", "backup-eigen-bucket", "eigen-bucket", ["read", "write", "delete", "list"]
        )

        policy = connector.create_policy.await_args.args[2]
        resources = {resource for statement in policy["Statement"] for resource in statement["Resource"]}
        assert resources == {"arn:aws:s3:::eigen-bucket", "arn:aws:s3:::eigen-bucket/*"}
        actions = {action for statement in policy["Statement"] for action in statement["Action"]}
        assert {"s3:ListBucket", "s3:GetObject", "s3:PutObject", "s3:DeleteObject"} <= actions

    @pytest.mark.asyncio
    async def test_een_policy_die_niet_vastgezet_wordt_laat_de_backup_niet_doorgaan(self) -> None:
        manager = _manager(PVCBackupManager)
        connector = _minio_connector()
        connector.grant_bucket_access = AsyncMock(return_value={"status": "error", "message": "mc weigert"})

        with (
            patch("opi.manager.backup.base.create_minio_connector", return_value=connector),
            pytest.raises(RuntimeError, match="scope backup account"),
        ):
            await manager._ensure_backup_bucket_exists(project_name=PROJECT, cluster="sandboxed-local")


class TestHetGeheimWordtAfgeleid:
    def test_dezelfde_invoer_geeft_hetzelfde_geheim(self) -> None:
        assert derive_bucket_account_secret(ADMIN_SECRET_KEY, "bucket-a") == derive_bucket_account_secret(
            ADMIN_SECRET_KEY, "bucket-a"
        )

    def test_een_andere_bucket_of_een_ander_beheergeheim_geeft_een_ander_geheim(self) -> None:
        eigen = derive_bucket_account_secret(ADMIN_SECRET_KEY, "bucket-a")

        assert derive_bucket_account_secret(ADMIN_SECRET_KEY, "bucket-b") != eigen
        assert derive_bucket_account_secret("ander-beheergeheim", "bucket-a") != eigen

    def test_het_geheim_past_binnen_de_lengtes_die_minio_toestaat(self) -> None:
        secret = derive_bucket_account_secret(ADMIN_SECRET_KEY, "bucket-a")

        # create_user weigert een geheim korter dan 8 tekens, MinIO zelf een langer dan 40.
        assert 8 <= len(secret) <= _MINIO_SECRET_MAX_LENGTH
        assert secret.isascii()
        assert "/" not in secret


class TestElkePodKrijgtHetAccountVanZijnEigenBucket:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", POD_CREATORS)
    async def test_het_secret_draagt_het_bucketaccount_en_niet_het_beheeraccount(self, kind: str) -> None:
        cls, create = POD_CREATORS[kind]
        manager = _manager(cls)

        await create(manager, PROJECT)

        secret = next(
            yaml.safe_load(call.kwargs["stdin_input"])
            for call in manager.kubectl.run_command.await_args_list
            if call.kwargs.get("stdin_input") and yaml.safe_load(call.kwargs["stdin_input"])["kind"] == "Secret"
        )
        bucket = get_backup_bucket_name(PROJECT, None)
        account = backup_bucket_account_name(bucket)
        carried = " ".join(secret["stringData"].values())

        assert account in carried, secret["stringData"]
        assert derive_bucket_account_secret(ADMIN_SECRET_KEY, bucket) in carried
        assert f"{ADMIN_ACCESS_KEY}:{ADMIN_SECRET_KEY}" not in carried
        assert ADMIN_ACCESS_KEY not in secret["stringData"].values()


class TestHetBeheeraccountBlijftBijOpi:
    def _admin_key_uses(self) -> dict[str, set[str]]:
        """Per bestand de functies die ``self.config.s3_access_key`` of ``-secret_key`` lezen."""
        uses: dict[str, set[str]] = {}
        for path in sorted(_BACKUP_PACKAGE.glob("*.py")):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                for inner in ast.walk(node):
                    if (
                        isinstance(inner, ast.Attribute)
                        and inner.attr in ("s3_access_key", "s3_secret_key")
                        and isinstance(inner.value, ast.Attribute)
                        and inner.value.attr == "config"
                    ):
                        uses.setdefault(path.name, set()).add(node.name)
        return uses

    def test_alleen_de_toegestane_functies_lezen_de_beheersleutel(self) -> None:
        found = {name for functions in self._admin_key_uses().values() for name in functions}

        assert found, "de toets vindt geen enkel gebruik, dus hij meet niets"
        assert found <= ADMIN_KEY_IS_ALLOWED_IN, found - ADMIN_KEY_IS_ALLOWED_IN

    def test_de_toets_kijkt_in_het_hele_pakket(self) -> None:
        assert set(self._admin_key_uses()) >= {"base.py", "pvc_backup.py", "bucket_backup.py", "database_backup.py"}


class TestSingleModus:
    def test_de_pod_krijgt_het_beheeraccount_met_een_waarschuwing(self, caplog: pytest.LogCaptureFixture) -> None:
        manager = _manager(PVCBackupManager)

        with (
            patch("opi.manager.backup.base.settings.BACKUP_S3_BUCKET_MODE", "single"),
            caplog.at_level(logging.WARNING, logger="opi.manager.backup.base"),
        ):
            credentials = manager._kopia_credentials("kp", "rig-backups")

        assert credentials["AWS_ACCESS_KEY_ID"] == ADMIN_ACCESS_KEY
        assert credentials["AWS_SECRET_ACCESS_KEY"] == ADMIN_SECRET_KEY
        assert "admin account" in caplog.text

    @pytest.mark.asyncio
    async def test_zonder_per_project_buckets_wordt_er_geen_account_aangemaakt(self) -> None:
        manager = _manager(PVCBackupManager)
        connector = _minio_connector()

        with (
            patch("opi.manager.backup.base.create_minio_connector", return_value=connector),
            patch("opi.manager.backup.base.settings.BACKUP_S3_BUCKET_MODE", "single"),
        ):
            await manager._ensure_backup_bucket_exists(project_name=PROJECT, cluster="sandboxed-local")

        connector.create_user.assert_not_awaited()
        connector.grant_bucket_access.assert_not_awaited()
