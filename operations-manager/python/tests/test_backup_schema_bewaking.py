"""Een geplande back-up die uitblijft wordt een metriek en een ERROR-regel.

Een mislukte back-uptaak laat een ``failed``-rij en een ERROR-regel achter. Een back-up
die helemaal niet start liet tot nu toe niets achter: de scheduler sloeg de tick over en
niemand merkte het. Deze tests meten de drie schakels van de ketting die dat dicht:

1. de scheduler legt per deployment vast hoe oud de laatste geslaagde geplande snapshot is;
2. de collector maakt daar twee reeksen van;
3. uitblijven levert een ERROR op die de log watcher doorlaat, hoogstens eens per dag.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from freezegun import freeze_time
from opi.core.backup_scheduler import BackupScheduler
from opi.services.backup_schedule_report import backup_schedule_states, forget_missing, record_schedule_check

_AMS = ZoneInfo("Europe/Amsterdam")

# 02:30 Amsterdam, binnen het inhaalvenster van een 02:00-doel, net als in
# tests/test_backup_scheduler.py.
_FROZEN_TIME = datetime(2026, 5, 20, 0, 30, 0, tzinfo=UTC)

_DAILY = "FREQ=DAILY;BYHOUR=2;BYMINUTE=0"


@pytest.fixture(autouse=True)
def _lege_tabel():
    """De tabel is procesintern, dus elke test begint en eindigt leeg."""
    forget_missing(set())
    yield
    forget_missing(set())


def _scheduler() -> BackupScheduler:
    return BackupScheduler(task_service=AsyncMock(), cluster="local")


def _snapshot(deployment: str, ts: datetime, trigger: str = "scheduled"):
    from opi.manager.backup.base import SnapshotInfo

    return SnapshotInfo(
        snapshot_id="abc",
        pvc_name="data",
        timestamp=ts.isoformat(),
        deployment_name=deployment,
        trigger=trigger,
    )


def _project(name: str, deployments: list[str], schedule: str = _DAILY) -> MagicMock:
    project = MagicMock()
    project.data = {
        "name": name,
        "backup": {"enabled": True},
        "deployments": [
            {"name": d, "cluster": "local", "namespace": "test", "backup": {"schedule": schedule}} for d in deployments
        ],
    }
    return project


def _tick(scheduler: BackupScheduler, projects: list, snapshots: list | None) -> None:
    """Draai een scheduler-tick met een gemockte Kopia-lijst (None: niet te bevragen)."""

    async def fake_get(self_, project_name, cluster, namespace, cache):
        return snapshots

    scheduler._get_namespace_snapshots = fake_get.__get__(scheduler, BackupScheduler)  # type: ignore[method-assign]
    with patch("opi.core.backup_scheduler.get_project_store") as store:
        store.return_value.get_all.return_value = projects
        asyncio.run(scheduler._check_and_schedule())


@freeze_time(_FROZEN_TIME)
class TestDeTabelNaEenTick:
    def test_het_tijdstip_van_de_laatste_geplande_snapshot_staat_erin(self) -> None:
        gisteren = datetime(2026, 5, 19, 0, 0, 0, tzinfo=UTC)
        _tick(_scheduler(), [_project("proj", ["prod"])], [_snapshot("prod", gisteren)])

        (state,) = backup_schedule_states()
        assert (state.project, state.deployment) == ("proj", "prod")
        assert state.last_success == gisteren.astimezone(_AMS)
        assert state.unknown_streak == 0

    def test_elk_deployment_krijgt_zijn_eigen_rij(self) -> None:
        gisteren = datetime(2026, 5, 19, 0, 0, 0, tzinfo=UTC)
        _tick(
            _scheduler(),
            [_project("proj", ["prod", "acc"])],
            [_snapshot("prod", gisteren)],
        )

        per_deployment = {s.deployment: s for s in backup_schedule_states()}
        assert set(per_deployment) == {"prod", "acc"}
        assert per_deployment["prod"].last_success == gisteren.astimezone(_AMS)
        assert per_deployment["acc"].last_success is None, "acc heeft geen snapshot, dat is geen fout"

    def test_een_onbevraagbare_kopia_is_onbekend_en_geen_nul(self) -> None:
        _tick(_scheduler(), [_project("proj", ["prod"])], None)

        (state,) = backup_schedule_states()
        assert state.unknown_streak == 1
        assert state.last_success is None

    def test_onbekend_laat_een_eerder_bekend_tijdstip_staan(self) -> None:
        """Niet kunnen kijken mag geen geslaagde back-up uitwissen."""
        gisteren = datetime(2026, 5, 19, 0, 0, 0, tzinfo=UTC)
        scheduler = _scheduler()
        _tick(scheduler, [_project("proj", ["prod"])], [_snapshot("prod", gisteren)])
        _tick(scheduler, [_project("proj", ["prod"])], None)

        (state,) = backup_schedule_states()
        assert state.last_success == gisteren.astimezone(_AMS)
        assert state.unknown_streak == 1

    def test_een_deployment_zonder_schema_verdwijnt_uit_de_tabel(self) -> None:
        """Anders blijft een verwijderd deployment een reeks en een dagelijkse ERROR geven."""
        scheduler = _scheduler()
        _tick(scheduler, [_project("proj", ["prod", "acc"])], [])
        _tick(scheduler, [_project("proj", ["prod"])], [])

        assert [s.deployment for s in backup_schedule_states()] == ["prod"]


class TestDeTweeReeksen:
    def _families(self) -> dict:
        from opi.core.metrics import OPICollector

        return {familie.name: familie for familie in OPICollector().collect()}

    def test_een_gevulde_tabel_geeft_beide_reeksen(self) -> None:
        nu = datetime(2026, 5, 20, 2, 30, tzinfo=_AMS)
        geslaagd = datetime(2026, 5, 20, 2, 5, tzinfo=_AMS)
        record_schedule_check(
            project="proj",
            deployment="prod",
            schedule=_DAILY,
            status="found",
            last_success=geslaagd,
            now=nu,
        )

        families = self._families()
        (laatste,) = families["opi_backup_last_scheduled_success_timestamp_seconds"].samples
        assert laatste.labels == {"project": "proj", "deployment": "prod"}
        assert laatste.value == geslaagd.timestamp()
        (onbekend,) = families["opi_backup_schedule_unknown"].samples
        assert onbekend.value == 0

    def test_onbekend_staat_op_een_en_de_tijdstempel_op_nul(self) -> None:
        nu = datetime(2026, 5, 20, 2, 30, tzinfo=_AMS)
        record_schedule_check(
            project="proj",
            deployment="prod",
            schedule=_DAILY,
            status="error",
            last_success=None,
            now=nu,
        )

        families = self._families()
        (laatste,) = families["opi_backup_last_scheduled_success_timestamp_seconds"].samples
        assert laatste.value == 0.0, "nog nooit een geplande back-up gezien"
        (onbekend,) = families["opi_backup_schedule_unknown"].samples
        assert onbekend.value == 1

    def test_een_lege_tabel_geeft_geen_reeksen(self) -> None:
        families = self._families()

        assert families["opi_backup_last_scheduled_success_timestamp_seconds"].samples == []
        assert families["opi_backup_schedule_unknown"].samples == []


class TestDeErrorRegelBijUitblijven:
    """Vaste klok: de ouderdom wordt gemeten, niet geraden."""

    _nu: ClassVar[datetime] = datetime(2026, 5, 20, 8, 0, tzinfo=_AMS)

    def _zie(
        self,
        *,
        last_success: datetime | None,
        now: datetime,
        status: str = "found",
        schedule: str = _DAILY,
    ) -> None:
        record_schedule_check(
            project="algor-odc",
            deployment="prod",
            schedule=schedule,
            status=status,
            last_success=last_success,
            now=now,
        )

    def _fouten(self, scheduler: BackupScheduler, now: datetime, caplog) -> list[str]:
        with caplog.at_level(logging.ERROR, logger="opi.core.backup_scheduler"):
            caplog.clear()
            scheduler._log_overdue_backups(now)
        return [r.getMessage() for r in caplog.records]

    def test_een_dagelijkse_backup_van_dertig_uur_oud_geeft_een_error(self, caplog) -> None:
        self._zie(last_success=self._nu - timedelta(hours=30), now=self._nu)

        (melding,) = self._fouten(_scheduler(), self._nu, caplog)
        assert "algor-odc/prod" in melding
        assert _DAILY in melding
        assert "30 hours ago" in melding

    def test_binnen_de_periode_plus_inhaalvenster_geen_error(self, caplog) -> None:
        """26 uur is voorbij de 24u periode, maar nog binnen het inhaalvenster van 4u."""
        self._zie(last_success=self._nu - timedelta(hours=26), now=self._nu)

        assert self._fouten(_scheduler(), self._nu, caplog) == []

    def test_voorbij_periode_plus_inhaalvenster_geeft_een_error(self, caplog) -> None:
        """29 uur is voorbij de 24u periode en het inhaalvenster van 4u samen."""
        self._zie(last_success=self._nu - timedelta(hours=29), now=self._nu)

        (melding,) = self._fouten(_scheduler(), self._nu, caplog)
        assert "29 hours ago" in melding

    def test_een_tweede_tick_binnen_vierentwintig_uur_zwijgt(self, caplog) -> None:
        scheduler = _scheduler()
        self._zie(last_success=self._nu - timedelta(hours=30), now=self._nu)

        assert len(self._fouten(scheduler, self._nu, caplog)) == 1
        assert self._fouten(scheduler, self._nu + timedelta(hours=23), caplog) == []
        assert len(self._fouten(scheduler, self._nu + timedelta(hours=25), caplog)) == 1

    def test_een_net_ingesteld_schema_zonder_snapshot_zwijgt_de_eerste_periode(self, caplog) -> None:
        self._zie(last_success=None, status="none", now=self._nu - timedelta(hours=3))

        assert self._fouten(_scheduler(), self._nu, caplog) == []

    def test_een_schema_dat_na_een_volle_periode_nog_niets_opleverde_geeft_een_error(self, caplog) -> None:
        self._zie(last_success=None, status="none", now=self._nu - timedelta(hours=30))

        (melding,) = self._fouten(_scheduler(), self._nu, caplog)
        assert "no scheduled backup yet" in melding

    def test_twee_ticks_op_rij_onbekend_geeft_een_error(self, caplog) -> None:
        scheduler = _scheduler()
        self._zie(last_success=None, status="error", now=self._nu)

        assert self._fouten(scheduler, self._nu, caplog) == [], "een enkele mislukte Kopia-vraag is geen alarm"

        self._zie(last_success=None, status="error", now=self._nu)
        (melding,) = self._fouten(scheduler, self._nu, caplog)
        assert "could not be queried" in melding

    def test_een_weekschema_krijgt_een_week_de_tijd(self, caplog) -> None:
        weekly = "FREQ=WEEKLY;BYDAY=SU;BYHOUR=3;BYMINUTE=0"
        self._zie(last_success=self._nu - timedelta(days=3), now=self._nu, schedule=weekly)

        assert self._fouten(_scheduler(), self._nu, caplog) == []

        self._zie(last_success=self._nu - timedelta(days=8), now=self._nu, schedule=weekly)
        assert len(self._fouten(_scheduler(), self._nu, caplog)) == 1


class TestDeLogWatcherLaatHemDoor:
    """De melding loopt via de log watcher: ERROR uit Loki, door de ignore-lijst, dan ntfy."""

    _regel: ClassVar[str] = (
        "2026-10-16 08:00:01,123 - opi.core.backup_scheduler - ERROR - [-] "
        "Scheduled backup overdue for algor-odc/prod (schedule FREQ=DAILY;BYHOUR=2;BYMINUTE=0): "
        "last successful scheduled backup was 30 hours ago"
    )

    def test_de_regel_wordt_als_los_record_herkend(self) -> None:
        from opi.services.log_watcher import _LOG_RECORD_RE

        assert _LOG_RECORD_RE.search(self._regel), "anders telt hij als vervolgregel van een traceback"

    def test_de_ignore_lijst_gooit_hem_niet_weg(self) -> None:
        from opi.services.log_watcher import DEFAULT_IGNORE_FILE, load_ignore_patterns

        patronen = [p.pattern for p in load_ignore_patterns(DEFAULT_IGNORE_FILE) if p.search(self._regel)]
        assert patronen == []

    def test_project_en_deployment_overleven_de_verkorting_naar_ntfy(self) -> None:
        from opi.services.log_watcher import human

        kort = human(self._regel)
        assert kort.startswith("backup_scheduler: Scheduled backup overdue for algor-odc/prod")

    def test_de_dedup_sleutel_is_stabiel_over_de_uren_heen(self) -> None:
        """Zonder dit zou elke dagelijkse herhaling een nieuwe melding zijn."""
        from opi.services.log_watcher import signature

        later = self._regel.replace("30 hours ago", "54 hours ago")
        assert signature(self._regel) == signature(later)


class TestHetRequestPad:
    """``trigger_check`` hangt aan het opslaan van een backup-schema in de wizard en
    wordt in het request ge-await, dus daar mag de bewaking geen listing kosten."""

    @staticmethod
    def _tel_listings(scheduler: BackupScheduler) -> list[str]:
        aanroepen: list[str] = []

        async def fake_get(self_, project_name, cluster, namespace, cache):
            aanroepen.append(namespace)
            return []

        scheduler._get_namespace_snapshots = fake_get.__get__(scheduler, BackupScheduler)  # type: ignore[method-assign]
        return aanroepen

    # 14:00 Amsterdam: ruim buiten het inhaalvenster van een 02:00-doel, dus de
    # due-controle zelf bevraagt Kopia niet.
    @freeze_time(datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC))
    def test_trigger_check_bevraagt_kopia_niet_en_legt_niets_vast(self) -> None:
        scheduler = _scheduler()
        aanroepen = self._tel_listings(scheduler)
        with patch("opi.core.backup_scheduler.get_project_store") as store:
            store.return_value.get_all.return_value = [_project("proj", ["prod"])]
            asyncio.run(scheduler.trigger_check())

        assert aanroepen == []
        assert backup_schedule_states() == []

    @freeze_time(datetime(2026, 5, 20, 12, 0, 0, tzinfo=UTC))
    def test_een_gewone_tick_buiten_het_venster_betaalt_de_listing_wel(self) -> None:
        """De prijs van een reeks die ook buiten het back-upvenster iets kan zeggen."""
        scheduler = _scheduler()
        aanroepen = self._tel_listings(scheduler)
        with patch("opi.core.backup_scheduler.get_project_store") as store:
            store.return_value.get_all.return_value = [_project("proj", ["prod"])]
            asyncio.run(scheduler._check_and_schedule())

        assert aanroepen == ["rig-test"]
        assert len(backup_schedule_states()) == 1


_ONBEREIKBAAR = (
    'can\'t connect to storage: error retrieving storage config from bucket "backup-rig-proj": '
    'Get "http://minio.rig-geen-bestemming.svc:9000/": dial tcp: no such host'
)


def _repo_config():
    from opi.connectors.kopia import KopiaRepositoryConfig

    return KopiaRepositoryConfig(
        s3_endpoint="minio.rig-geen-bestemming.svc:9000",
        s3_bucket="backup-rig-proj",
        s3_access_key="sleutel",
        s3_secret_key="geheim",
        s3_prefix="local/rig-test",
        password="wachtwoord",
    )


class TestEenOnbereikbareBestemmingIsNietLeeg:
    """Gemeten op de sandbox: de onbekend-tak kwam nooit aan de beurt.

    Een onbereikbare bestemming werd twee lagen lager een lege lijst, dus de scheduler zag
    "er is geen snapshot" waar "ik kon niet kijken" stond. Deze toetsen leggen vast dat
    beide lagen het onderscheid nu doorgeven, en dat de leeslijst van de schermen
    ongewijzigd leeg blijft.
    """

    def test_de_connector_meldt_een_onbereikbare_repository(self) -> None:
        from opi.connectors.kopia import KopiaConnectionError, KopiaConnector

        kopia = KopiaConnector()
        with (
            patch.object(KopiaConnector, "_run_kopia_command", AsyncMock(return_value=("", _ONBEREIKBAAR, 1))),
            pytest.raises(KopiaConnectionError),
        ):
            asyncio.run(kopia.list_snapshots(_repo_config()))

    def test_een_repository_die_nog_niet_bestaat_blijft_leeg(self) -> None:
        """Een project dat nog nooit een back-up had is geen storing."""
        from opi.connectors.kopia import KopiaConnector

        kopia = KopiaConnector()
        with patch.object(
            KopiaConnector, "_run_kopia_command", AsyncMock(return_value=("", "repository not found", 1))
        ):
            assert asyncio.run(kopia.list_snapshots(_repo_config())) == []

    def test_de_strikte_variant_van_de_manager_laat_de_fout_door(self) -> None:
        from opi.connectors.kopia import KopiaConnectionError, KopiaConnector
        from opi.manager.backup import create_backup_manager

        manager = create_backup_manager()
        with (
            patch.object(type(manager), "_derive_backup_key", AsyncMock(return_value="wachtwoord")),
            patch.object(KopiaConnector, "list_snapshots", AsyncMock(side_effect=KopiaConnectionError("weg"))),
            pytest.raises(KopiaConnectionError),
        ):
            asyncio.run(manager.list_snapshots_or_raise(cluster="local", namespace="rig-test"))

    def test_de_leeslijst_van_de_schermen_blijft_leeg_in_plaats_van_stuk(self) -> None:
        from opi.connectors.kopia import KopiaConnectionError, KopiaConnector
        from opi.manager.backup import create_backup_manager

        manager = create_backup_manager()
        with (
            patch.object(type(manager), "_derive_backup_key", AsyncMock(return_value="wachtwoord")),
            patch.object(KopiaConnector, "list_snapshots", AsyncMock(side_effect=KopiaConnectionError("weg"))),
        ):
            assert asyncio.run(manager.list_snapshots(cluster="local", namespace="rig-test")) == []


@freeze_time(_FROZEN_TIME)
class TestDeTickBoektOnbekend:
    """Zonder gestubde ``_get_namespace_snapshots``: de schakel die op de sandbox brak."""

    def test_een_manager_die_de_fout_meldt_levert_een_onbekend_tick(self) -> None:
        from opi.connectors.kopia import KopiaConnectionError

        manager = MagicMock()
        manager.list_snapshots_or_raise = AsyncMock(side_effect=KopiaConnectionError("onbereikbaar"))
        scheduler = _scheduler()
        with (
            patch("opi.manager.backup.create_backup_manager", return_value=manager),
            patch("opi.core.backup_scheduler.get_project_store") as store,
        ):
            store.return_value.get_all.return_value = [_project("proj", ["prod"])]
            asyncio.run(scheduler._check_and_schedule())

        (state,) = backup_schedule_states()
        assert (state.unknown_streak, state.last_success) == (1, None)
