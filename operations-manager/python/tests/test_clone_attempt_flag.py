"""Een run die na een geslaagde databasekloon afbreekt, maakt bij de volgende run geen ``_v1``.

De kloon loopt vooraan in ``process_project`` en de afronding wordt pas bij de save aan het
eind vastgelegd. De vlag ``clone-from.status.in-progress`` gaat daarom op schijf op het moment
dat de kloon begint, zodat de volgende run weet dat de bestaande database van die poging is.
"""

from __future__ import annotations

import copy
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.connectors.postgres import PostgresExecutionError
from opi.core.config import settings
from opi.handlers.project_file_handler import ProjectFileHandler
from opi.manager.database_manager import DatabaseManager
from opi.manager.project_manager import ProjectManager
from opi.manager.revision_manager import RevisionManager
from opi.services import ServiceType
from opi.services.registry import get_service

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

HERE = settings.CLUSTER_MANAGER
TARGET_DB = "demo_staging"


def _project(**status: Any) -> dict[str, Any]:
    clone_from: dict[str, Any] = {"type": "deployment", "reference": "production", "mode": "once"}
    if status:
        clone_from["status"] = status
    return {
        "name": "demo",
        "services": ["postgresql-database"],
        "components": [{"name": "web", "services": ["postgresql-database"]}],
        "deployments": [
            {"name": "production", "cluster": HERE, "components": [{"reference": "web"}]},
            {"name": "staging", "cluster": HERE, "components": [{"reference": "web"}], "clone-from": clone_from},
        ],
    }


def _status(project: dict[str, Any]) -> dict[str, Any]:
    return project["deployments"][1]["clone-from"].get("status", {})


class FakePostgres:
    """Houdt databases en schema's bij zoals de server dat doet, zodat een tweede run ziet wat de eerste achterliet."""

    def __init__(self, schemas: dict[str, set[str]] | None = None) -> None:
        self.schemas: dict[str, set[str]] = schemas if schemas is not None else {"demo_production": {"public"}}
        self.clone_calls: list[str] = []
        self.clone_error: Exception | None = None

    async def create_database(self, database_name: str, owner: str) -> dict[str, str]:
        if database_name in self.schemas:
            return {"status": "exists"}
        self.schemas[database_name] = {"public"}
        return {"status": "created"}

    async def delete_database(self, database_name: str) -> dict[str, str]:
        self.schemas.pop(database_name, None)
        return {"status": "deleted"}

    async def list_schemas(self, database: str) -> list[dict[str, str]]:
        return [{"schema_name": name, "schema_owner": "x"} for name in sorted(self.schemas[database])]

    async def create_schema(self, schema_name: str, database: str, owner: str) -> dict[str, str]:
        status = "exists" if schema_name in self.schemas[database] else "created"
        self.schemas[database].add(schema_name)
        return {"status": status}

    async def delete_schema(self, schema_name: str, database: str, cascade: bool = False) -> dict[str, str]:
        self.schemas[database].discard(schema_name)
        return {"status": "deleted"}

    async def clone_schema(self, *, source_schema: str, target_database: str, target_schema: str, **_: Any) -> dict:
        self.clone_calls.append(target_database)
        # Zoals _execute_pgdump_clone: eerst onder de bronnaam, pas aan het eind hernoemd.
        self.schemas.setdefault(target_database, {"public"}).add(source_schema)
        if self.clone_error is not None:
            raise self.clone_error
        self.schemas[target_database].discard(source_schema)
        self.schemas[target_database].add(target_schema)
        return {"status": "success"}

    async def clone_schema_from_external(
        self,
        *,
        source_schema: str,
        target_database: str,
        target_schema: str,
        force_clone: bool = False,
        **_: Any,
    ) -> dict:
        self.clone_calls.append(target_database)
        # Zoals _execute_pgdump_clone: een bestaand doelschema wordt zonder force_clone geweigerd.
        if target_schema in self.schemas[target_database] and not force_clone:
            raise RuntimeError(f"Target schema '{target_schema}' already exists in database '{target_database}'")
        self.schemas[target_database].add(source_schema)
        if self.clone_error is not None:
            raise self.clone_error
        self.schemas[target_database].discard(source_schema)
        self.schemas[target_database].add(target_schema)
        return {"status": "success"}

    async def set_role_search_path(self, **_: Any) -> None:
        return None


class FakeStore:
    """De committed staat van het projectbestand, met per save wat er werd aangeboden."""

    def __init__(self, committed: dict[str, Any]) -> None:
        self.committed = copy.deepcopy(committed)
        self.saves: list[dict[str, Any]] = []

    async def read_path(self, relative_path: str, ref: str = "HEAD") -> dict[str, Any]:
        return copy.deepcopy(self.committed)

    async def save(self, name: str, data: dict[str, Any], **kwargs: Any) -> None:
        self.saves.append({"data": copy.deepcopy(data), "base": copy.deepcopy(kwargs.get("base"))})
        self.committed = copy.deepcopy(data)


class FakeService:
    """Een dienst die voor de database provisioneert, zoals Keycloak."""

    def __init__(self, provision: Any) -> None:
        self.provision = AsyncMock(side_effect=provision)


def _manager(
    monkeypatch: pytest.MonkeyPatch,
    store: FakeStore,
    pg: FakePostgres,
    manifests: Any = None,
    before_database: Any = None,
) -> ProjectManager:
    """Een echte run door process_project; alleen wat naar het cluster en naar git gaat is nagebootst."""
    monkeypatch.setattr("opi.manager.project_manager.get_project_store", lambda: store)
    monkeypatch.setattr(
        "opi.manager.project_manager.provisioning_services",
        lambda: [FakeService(before_database), get_service(ServiceType.POSTGRESQL_DATABASE)],
    )
    pm = ProjectManager(project_file_relative_path="projects/demo.yaml")
    db_manager = DatabaseManager(pm, db_host="postgres", admin_username="admin", admin_password="admin")
    db_manager._postgres_connector = pg  # type: ignore[assignment]
    db_manager._resolve_database_credentials = AsyncMock(return_value="secret")  # type: ignore[method-assign]
    db_manager._ensure_readonly_user = AsyncMock(return_value=("ro", "ro-secret"))  # type: ignore[method-assign]
    db_manager._validate_clone_source = AsyncMock()  # type: ignore[method-assign]
    pm._ensure_database_manager = AsyncMock(return_value=db_manager)  # type: ignore[method-assign]
    pm.has_deployments_for_current_cluster = AsyncMock(return_value=True)  # type: ignore[method-assign]
    pm.check_and_create_namespaces = AsyncMock()  # type: ignore[method-assign]
    pm.check_and_create_sops_secrets_in_namespaces = AsyncMock()  # type: ignore[method-assign]
    pm._process_application_manifests = AsyncMock(side_effect=manifests)  # type: ignore[method-assign]
    pm._record_reconciliation = AsyncMock()  # type: ignore[method-assign]
    pm._argo_manager = MagicMock(create_argocd_resources=AsyncMock())
    pm._bootstrap_manager = MagicMock(execute_bootstrap_for_deployment=AsyncMock())
    return pm


async def test_a_run_that_breaks_after_the_clone_is_finished_by_the_next_run(monkeypatch: pytest.MonkeyPatch) -> None:
    store = FakeStore(_project())
    pg = FakePostgres()

    first = _manager(monkeypatch, store, pg, manifests=RuntimeError("manifesten stuk"))
    assert await first.process_project() is False

    assert _status(store.committed) == {"in-progress": True}, "de poging moet op schijf staan, ook na de fout"
    assert pg.schemas[TARGET_DB] == {"public", TARGET_DB}

    second = _manager(monkeypatch, store, pg)
    assert await second.process_project() is True

    assert pg.clone_calls == [TARGET_DB], "de tweede run mag niet opnieuw klonen"
    assert set(pg.schemas) == {"demo_production", TARGET_DB}, "geen _v1 naast de bestaande database"
    status = _status(store.committed)
    assert status["completed"] is True
    assert "in-progress" not in status
    handler = ProjectFileHandler()
    assert handler.get_deployment_service_generation(store.committed, "staging", "postgresql-database") == 0
    assert second._secrets_to_create["staging"]["database"].database == TARGET_DB


async def test_without_the_flag_an_existing_database_still_fails_over(monkeypatch: pytest.MonkeyPatch) -> None:
    """Een oud projectbestand zonder vlag gedraagt zich als voorheen."""
    store = FakeStore(_project())
    pg = FakePostgres({"demo_production": {"public"}, TARGET_DB: {"public"}})

    assert await _manager(monkeypatch, store, pg).process_project() is True

    assert pg.clone_calls == [f"{TARGET_DB}_v1"]


async def test_the_flag_is_on_disk_before_the_clone_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Een run die tijdens de kloon sterft, ruimt niets op; de volgende run moet de poging dan al zien."""
    store = FakeStore(_project())
    pg = FakePostgres()
    status_at_clone: list[dict[str, Any]] = []
    clone_schema = pg.clone_schema

    async def clone_and_note_the_status(**kwargs: Any) -> dict:
        status_at_clone.append(copy.deepcopy(_status(store.committed)))
        return await clone_schema(**kwargs)

    pg.clone_schema = clone_and_note_the_status  # type: ignore[method-assign]

    assert await _manager(monkeypatch, store, pg).process_project() is True

    assert status_at_clone == [{"in-progress": True}]


async def test_the_final_save_compares_against_the_state_after_the_flag_save(monkeypatch: pytest.MonkeyPatch) -> None:
    """De save halverwege verzet de basis; de save aan het eind moet tegen precies die staat vergelijken."""
    store = FakeStore(_project())
    pg = FakePostgres()

    async def someone_writes_and_pm_reads(*_: Any, **__: Any) -> None:
        # Wat de Keycloak-stap halverwege doet: iemand anders schrijft, deze manager leest.
        store.committed.setdefault("users", []).append({"email": "tussendoor@example.nl", "role": "admin"})
        await pm.get_contents()

    pm = _manager(
        monkeypatch, store, pg, manifests=someone_writes_and_pm_reads, before_database=someone_writes_and_pm_reads
    )
    assert await pm.process_project() is True

    flag_save, final_save = store.saves
    assert _status(flag_save["data"]) == {"in-progress": True}
    assert flag_save["base"] == _project(), "de vlag-save moet tegen de basis van de run vergelijken"
    assert final_save["base"] == flag_save["data"], "de save aan het eind moet de basis na de vlag-save houden"
    assert _status(final_save["data"])["completed"] is True
    assert "in-progress" not in _status(final_save["data"])


async def test_a_run_that_breaks_before_the_clone_leaves_no_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Een database van voor de clone-from, met zijn schema, mag een latere run niet voor de kloon aanzien."""
    store = FakeStore(_project())
    pg = FakePostgres({"demo_production": {"public"}, TARGET_DB: {"public", TARGET_DB}})

    first = _manager(monkeypatch, store, pg, before_database=RuntimeError("keycloak weg"))
    assert await first.process_project() is False

    assert store.saves == []
    assert pg.clone_calls == []

    assert await _manager(monkeypatch, store, pg).process_project() is True
    assert pg.clone_calls == [f"{TARGET_DB}_v1"], "de oude database telde als afgeronde kloon"


async def test_a_clone_into_a_new_generation_gets_no_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """De volgende run kent die generatie niet en zou de oude database voor de kloon aanzien."""
    store = FakeStore(_project())
    pg = FakePostgres({"demo_production": {"public"}, TARGET_DB: {"public", TARGET_DB}})

    first = _manager(monkeypatch, store, pg, manifests=RuntimeError("manifesten stuk"))
    assert await first.process_project() is False

    assert pg.clone_calls == [f"{TARGET_DB}_v1"]
    assert store.saves == []

    assert await _manager(monkeypatch, store, pg).process_project() is True
    assert len(pg.clone_calls) == 2, "de oude database telde als afgeronde kloon"


async def test_outside_a_run_the_flag_is_not_written(monkeypatch: pytest.MonkeyPatch) -> None:
    store = FakeStore(_project())
    pm = _manager(monkeypatch, store, FakePostgres(), before_database=RuntimeError("keycloak weg"))
    assert await pm.process_project() is False

    await pm.mark_clone_started("staging")

    assert store.saves == []


@pytest.mark.parametrize(
    ("target_schemas", "clones"),
    [({"public", TARGET_DB}, []), ({"public"}, [TARGET_DB])],
    ids=["kloon-af", "schema-weg"],
)
async def test_an_interrupted_run_does_not_save_the_flag_again(
    monkeypatch: pytest.MonkeyPatch, target_schemas: set[str], clones: list[str]
) -> None:
    store = FakeStore(_project(**{"in-progress": True}))
    pg = FakePostgres({"demo_production": {"public"}, TARGET_DB: target_schemas})

    assert await _manager(monkeypatch, store, pg).process_project() is True

    assert pg.clone_calls == clones
    assert len(store.saves) == 1, "alleen de save aan het eind, de vlag stond al"
    assert store.saves[0]["base"] == _project(**{"in-progress": True})


async def test_the_flag_stays_off_for_deployments_this_run_does_not_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """Alleen een deployment waarvan deze run de database kloont krijgt de vlag, ook als er meer deployments zijn gevraagd."""
    project = _project()
    elders = {"type": "deployment", "reference": "production", "mode": "once"}
    project["deployments"].append({"name": "elders", "cluster": "ander-cluster", "clone-from": elders})
    project["deployments"].append({"name": "later", "cluster": HERE, "clone-from": copy.deepcopy(elders)})
    store = FakeStore(project)

    pm = _manager(monkeypatch, store, FakePostgres())
    assert await pm.process_project(deployment_names=["production", "staging", "elders"]) is True

    flag_save = store.saves[0]
    assert _status(flag_save["data"]) == {"in-progress": True}
    for saved in store.saves:
        clone_status = {d["name"]: d.get("clone-from", {}).get("status") for d in saved["data"]["deployments"]}
        assert clone_status["elders"] is None, "een deployment op een ander cluster kreeg de vlag"
        assert clone_status["later"] is None, "een deployment buiten deze run kreeg de vlag"


@pytest.mark.parametrize(
    ("clone_from", "marked"),
    [
        ({"type": "deployment", "reference": "p", "mode": "once"}, True),
        ({"type": "deployment", "reference": "p"}, True),
        ({"type": "deployment", "reference": "p", "mode": "once", "status": {"completed": False}}, True),
        ({"type": "deployment", "reference": "p", "mode": "always"}, False),
        ({"type": "deployment", "reference": "p", "mode": "once", "status": {"completed": True}}, False),
    ],
)
def test_the_flag_goes_on_only_for_an_unfinished_once_clone(clone_from: dict[str, Any], marked: bool) -> None:
    handler = ProjectFileHandler()
    project = {"deployments": [{"name": "staging", "clone-from": clone_from}]}

    assert handler.mark_clone_in_progress(project, "staging") is marked
    assert handler.is_clone_in_progress(project, "staging") is marked
    assert handler.mark_clone_in_progress(project, "staging") is False


def test_recording_the_clone_as_completed_clears_the_flag() -> None:
    handler = ProjectFileHandler()
    project = _project()
    handler.mark_clone_in_progress(project, "staging")

    handler.set_clone_status(project, "staging", completed=True, timestamp="2026-09-17T00:00:00+00:00")

    assert handler.is_clone_in_progress(project, "staging") is False


def test_a_deployment_without_clone_from_gets_no_flag() -> None:
    project = _project()

    assert ProjectFileHandler().mark_clone_in_progress(project, "production") is False
    assert "clone-from" not in project["deployments"][0]


# --- DatabaseManager: de beslissing per kloon ---------------------------------


def _db_manager(pg: FakePostgres) -> tuple[DatabaseManager, MagicMock]:
    pm = MagicMock()
    pm._revision_manager = RevisionManager(MagicMock())
    pm.mark_clone_started = AsyncMock()
    manager = DatabaseManager(pm, db_host="postgres", admin_username="admin", admin_password="admin")
    manager._postgres_connector = pg  # type: ignore[assignment]
    return manager, pm


async def _ensure(
    manager: DatabaseManager,
    *,
    interrupted: bool,
    force: bool = False,
    extra: list[dict] | None = None,
    generation: int | None = None,
) -> Any:
    project = _project()
    if extra:
        project["services"] = [{"postgresql-database": {"config": {"schemas": extra}}}]
    with patch.object(manager, "_validate_clone_source", new_callable=AsyncMock):
        return await manager._ensure_database_state(
            project_name="demo",
            deployment_name="staging",
            deployment=project["deployments"][1],
            db_database=TARGET_DB,
            db_schema=TARGET_DB,
            db_username=TARGET_DB,
            db_password="secret",
            project_data=project,
            force_clone_override=force,
            generation=generation,
            clone_interrupted=interrupted,
        )


async def test_an_interrupted_clone_into_a_database_without_its_schema_clones_into_that_database() -> None:
    pg = FakePostgres({"demo_production": {"public"}, TARGET_DB: {"public"}})
    manager, _ = _db_manager(pg)

    result = await _ensure(manager, interrupted=True)

    assert pg.clone_calls == [TARGET_DB]
    assert result.database == TARGET_DB
    manager.project_manager.mark_clone_started.assert_awaited_once_with("staging")


async def test_force_clone_still_gets_a_new_generation_during_an_interrupted_attempt() -> None:
    pg = FakePostgres({"demo_production": {"public"}, TARGET_DB: {"public", TARGET_DB}})
    manager, pm = _db_manager(pg)

    result = await _ensure(manager, interrupted=True, force=True)

    assert pg.clone_calls == [f"{TARGET_DB}_v1"]
    assert result.database == f"{TARGET_DB}_v1"
    pm.report_clone_performed.assert_called_once_with("staging", "postgresql-database", 1)
    pm.mark_clone_started.assert_not_awaited()


async def test_a_finished_clone_is_recorded_like_a_fresh_one() -> None:
    pg = FakePostgres({"demo_production": {"public"}, TARGET_DB: {"public", TARGET_DB}})
    manager, pm = _db_manager(pg)

    result = await _ensure(manager, interrupted=True)

    assert pg.clone_calls == []
    assert result.database == TARGET_DB
    pm.report_clone_performed.assert_called_once_with("staging", "postgresql-database", None)


async def test_a_clone_missing_an_extra_schema_is_not_finished() -> None:
    pg = FakePostgres({"demo_production": {"public"}, TARGET_DB: {"public", TARGET_DB}})
    manager, pm = _db_manager(pg)

    await _ensure(manager, interrupted=True, extra=[{"postfix": "audit"}])

    assert pg.clone_calls == [TARGET_DB]


async def test_a_failed_clone_into_an_existing_database_leaves_no_schema_behind() -> None:
    pg = FakePostgres({"demo_production": {"public"}, TARGET_DB: {"public", "eigen"}})
    pg.clone_error = RuntimeError("verbinding weg")
    manager, _ = _db_manager(pg)

    with pytest.raises(RuntimeError):
        await _ensure(manager, interrupted=True)

    assert pg.schemas[TARGET_DB] == {"public", "eigen"}, "alleen wat deze poging aanmaakte mag weg"


@pytest.mark.parametrize(
    "present",
    [TARGET_DB, f"{TARGET_DB}_audit"],
    ids=["doelschema", "extra-schema"],
)
async def test_no_flag_above_a_target_schema_from_before_the_clone_from(present: str) -> None:
    """Met de vlag boven zo'n schema zou een latere run het voor een afgeronde kloon aanzien."""
    pg = FakePostgres({"demo_production": {"public"}, TARGET_DB: {"public", present}})
    manager, pm = _db_manager(pg)

    # Een vastgelegde generatie, want anders neemt een bestaande database eerst de failover.
    await _ensure(manager, interrupted=False, generation=0, extra=[{"postfix": "audit"}])

    pm.mark_clone_started.assert_not_awaited()


async def test_the_cleanup_leaves_a_schema_from_another_session_alone() -> None:
    """list_schemas meldt ook wat een andere sessie ondertussen maakte; dit draait in een levende database."""
    pg = FakePostgres({"demo_production": {"public"}, TARGET_DB: {"public", "eigen"}})
    manager, _ = _db_manager(pg)
    clone_schema = pg.clone_schema

    async def clone_while_another_session_works(**kwargs: Any) -> dict:
        pg.schemas[TARGET_DB].add("pg_temp_7")
        return await clone_schema(**kwargs)

    pg.clone_schema = clone_while_another_session_works  # type: ignore[method-assign]
    pg.clone_error = RuntimeError("verbinding weg")

    with pytest.raises(RuntimeError):
        await _ensure(manager, interrupted=True)

    assert pg.schemas[TARGET_DB] == {"public", "eigen", "pg_temp_7"}, "alleen de schema's van deze kloon mogen weg"


async def test_a_failing_schema_list_does_not_hide_the_clone_error() -> None:
    pg = FakePostgres({"demo_production": {"public"}, TARGET_DB: {"public"}})
    manager, _ = _db_manager(pg)
    pg.clone_error = RuntimeError("verbinding weg")
    list_schemas = pg.list_schemas

    async def fail_once_the_clone_has_run(database: str) -> list[dict[str, str]]:
        if pg.clone_calls:
            raise PostgresExecutionError("geen verbinding")
        return await list_schemas(database)

    pg.list_schemas = fail_once_the_clone_has_run  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="verbinding weg"):
        await _ensure(manager, interrupted=True)


async def test_one_failing_drop_does_not_leave_the_other_schema_behind() -> None:
    pg = FakePostgres({"demo_production": {"public"}, TARGET_DB: {"public"}})
    manager, _ = _db_manager(pg)

    async def clone_that_leaves_both_names(**_: Any) -> dict:
        # Zoals een kloon die valt na het hernoemen van het eerste schema.
        pg.schemas[TARGET_DB].update({"demo_production", TARGET_DB})
        raise RuntimeError("verbinding weg")

    delete_schema = pg.delete_schema

    async def refuse_the_first(schema_name: str, database: str, cascade: bool = False) -> dict[str, str]:
        if schema_name == "demo_production":
            raise PostgresExecutionError("geen rechten")
        return await delete_schema(schema_name, database, cascade=cascade)

    pg.clone_schema = clone_that_leaves_both_names  # type: ignore[method-assign]
    pg.delete_schema = refuse_the_first  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="verbinding weg"):
        await _ensure(manager, interrupted=True)

    assert pg.schemas[TARGET_DB] == {"public", "demo_production"}, "het doelschema moet weg, ook na een mislukte drop"


async def test_a_failed_clone_into_a_fresh_database_still_drops_the_database() -> None:
    pg = FakePostgres()
    pg.clone_error = RuntimeError("verbinding weg")
    manager, _ = _db_manager(pg)

    with pytest.raises(RuntimeError):
        await _ensure(manager, interrupted=False)

    assert TARGET_DB not in pg.schemas


async def test_a_failed_flag_save_drops_the_fresh_database() -> None:
    pg = FakePostgres()
    manager, pm = _db_manager(pg)
    pm.mark_clone_started.side_effect = RuntimeError("git weg")

    with pytest.raises(RuntimeError, match="git weg"):
        await _ensure(manager, interrupted=False)

    assert pg.clone_calls == []
    assert TARGET_DB not in pg.schemas, "een verse database zonder vlag telt later als zombie"


# --- Remote source ------------------------------------------------------------


def _remote_manager(pg: FakePostgres) -> DatabaseManager:
    manager, pm = _db_manager(pg)
    pm.get_contents = AsyncMock(return_value=_project())
    pm._project_file_handler = ProjectFileHandler()
    pm._secrets_to_create = {}
    manager._validate_external_source = AsyncMock(return_value={"table_count": 1})  # type: ignore[method-assign]
    manager._deployment_uses_postgresql = AsyncMock(return_value=True)  # type: ignore[method-assign]
    manager._get_database_config_for_deployment = AsyncMock(return_value=("postgres", "admin", "admin"))  # type: ignore[method-assign]
    manager._resolve_database_credentials = AsyncMock(return_value="secret")  # type: ignore[method-assign]
    manager._ensure_readonly_user = AsyncMock(return_value=("ro", "ro-secret"))  # type: ignore[method-assign]
    return manager


async def _remote(manager: DatabaseManager, *, interrupted: bool, force: bool = False) -> dict[str, Any]:
    return await manager.clone_database_from_external_source(
        project_name="demo",
        deployment_name="staging",
        source_host="bron",
        source_port=5432,
        source_username="lezer",
        source_password="pw",
        source_database="bron",
        source_schema="bron",
        force_clone=force,
        clone_interrupted=interrupted,
    )


async def test_an_interrupted_remote_clone_with_its_schema_is_not_cloned_again() -> None:
    pg = FakePostgres({TARGET_DB: {"public", TARGET_DB}})

    result = await _remote(_remote_manager(pg), interrupted=True)

    assert result["success"] is True
    assert pg.clone_calls == []


async def test_a_remote_clone_without_the_flag_still_clones() -> None:
    pg = FakePostgres({TARGET_DB: {"public"}})
    manager = _remote_manager(pg)
    clones_at_flag: list[list[str]] = []
    manager.project_manager.mark_clone_started.side_effect = lambda _: clones_at_flag.append(list(pg.clone_calls))

    await _remote(manager, interrupted=False)

    assert pg.clone_calls == [TARGET_DB]
    manager.project_manager.mark_clone_started.assert_awaited_once_with("staging")
    assert clones_at_flag == [[]], "de vlag moet op schijf staan voordat de kloon begint"


async def test_a_remote_clone_does_not_claim_a_target_schema_from_before_the_clone_from() -> None:
    """Zonder deze grens zet run 1 de vlag boven een bestaand schema en meldt run 2 een kloon die nooit liep."""
    pg = FakePostgres({TARGET_DB: {"public", TARGET_DB}})
    manager = _remote_manager(pg)

    first = await _remote(manager, interrupted=False)

    manager.project_manager.mark_clone_started.assert_not_awaited()
    assert first["success"] is False, "clone_schema_from_external weigert een bestaand doelschema"

    second = await _remote(manager, interrupted=False)

    assert second["success"] is False, "zonder vlag telt het oude schema niet als afgeronde kloon"
    assert pg.clone_calls == [TARGET_DB, TARGET_DB]


async def test_a_failed_remote_flag_save_is_reported_as_a_failed_clone() -> None:
    pg = FakePostgres({TARGET_DB: {"public"}})
    manager = _remote_manager(pg)
    manager.project_manager.mark_clone_started.side_effect = RuntimeError("git weg")

    result = await _remote(manager, interrupted=False)

    assert result["success"] is False
    assert any("git weg" in error for error in result["errors"])
    assert pg.clone_calls == []


async def test_a_failed_remote_clone_leaves_no_schema_behind() -> None:
    pg = FakePostgres({TARGET_DB: {"public", "eigen"}})
    pg.clone_error = RuntimeError("tunnel weg")

    result = await _remote(_remote_manager(pg), interrupted=False)

    assert result["success"] is False
    assert pg.clone_calls == [TARGET_DB]
    assert pg.schemas[TARGET_DB] == {"public", "eigen"}, "alleen wat deze poging aanmaakte mag weg"


async def test_an_interrupted_remote_clone_without_its_schema_clones_again() -> None:
    pg = FakePostgres({TARGET_DB: {"public"}})

    result = await _remote(_remote_manager(pg), interrupted=True)

    assert result["success"] is True
    assert pg.clone_calls == [TARGET_DB]


async def test_force_clone_still_gets_a_new_remote_generation_during_an_interrupted_attempt() -> None:
    pg = FakePostgres({TARGET_DB: {"public", TARGET_DB}})

    manager = _remote_manager(pg)

    result = await _remote(manager, interrupted=True, force=True)

    assert result["success"] is True
    assert pg.clone_calls == [f"{TARGET_DB}_v1"]
    manager.project_manager.mark_clone_started.assert_not_awaited()


def _remote_project(chisel: dict[str, str] | None = None) -> dict[str, Any]:
    project = _project()
    source: dict[str, Any] = {
        "name": "oud-systeem",
        "services": {
            "postgresql-database": {
                "host": "bron",
                "username": "lezer",
                "password": "pw",
                "database": "bron",
                "schema": "bron",
            }
        },
    }
    if chisel:
        source["chisel"] = chisel
    project["remote-sources"] = [source]
    project["deployments"][1]["clone-from"] = {"type": "remote-source", "reference": "oud-systeem", "mode": "once"}
    return project


@pytest.mark.parametrize(
    "chisel",
    [None, {"server-url": "https://chisel.example", "username": "c", "password": "pw"}],
    ids=["direct", "via-tunnel"],
)
async def test_the_flag_reaches_a_remote_clone_started_by_the_run(chisel: dict[str, str] | None) -> None:
    pg = FakePostgres({TARGET_DB: {"public", TARGET_DB}})
    manager = _remote_manager(pg)
    project = _remote_project(chisel)
    manager.project_manager.get_contents = AsyncMock(return_value=project)

    @asynccontextmanager
    async def tunnel(*_: Any) -> AsyncIterator[dict[str, Any]]:
        yield {"host": "127.0.0.1", "port": 15432}

    with (
        patch("opi.utils.age.get_decoded_project_private_key", AsyncMock(return_value="sleutel")),
        patch("opi.utils.age.decrypt_password_smart", AsyncMock(return_value="pw")),
        patch("opi.utils.chisel_helper.chisel_tunnel", tunnel),
    ):
        result = await manager._ensure_database_state(
            project_name="demo",
            deployment_name="staging",
            deployment=project["deployments"][1],
            db_database=TARGET_DB,
            db_schema=TARGET_DB,
            db_username=TARGET_DB,
            db_password="secret",
            project_data=project,
            clone_interrupted=True,
        )

    assert pg.clone_calls == [], "de vlag kwam niet aan bij de kloon op afstand"
    assert result.database == TARGET_DB
