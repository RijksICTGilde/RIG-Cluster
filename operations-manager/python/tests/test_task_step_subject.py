"""Een stap in de voortgang zegt waarvoor hij loopt.

Bij het aanmaken van een project met twee deployments stonden dezelfde drie stappen
twee keer onder elkaar -- "Database klaarmaken", "MinIO-opslag klaarmaken",
"Redis-cache klaarmaken" -- zonder dat er iets bij stond waaraan je zag welke
deployment aan de beurt was. Wie keek of het opschoot, kon niet zien waar hij was.

Een stap draagt daarom nu een ONDERWERP naast zijn naam: de deploymentnaam. Naast, niet
erin, zodat de weergave kan kiezen hoe het toont en de naam bruikbaar blijft om op te
groeperen. Een stap die een keer per project loopt heeft geen onderwerp, want dat maakt
de lijst alleen langer.

De tweede helft van hetzelfde: de helft van de regels was Engels ("Creating MinIO
storage resources" tussen "Database klaarmaken"), en dat viel op als iets dat vergeten
was. De scan onderaan dit bestand meet dat op de bron van alle stapnamen tegelijk, zodat
een nieuwe Engelse regel er niet ongemerkt bij komt.
"""

import ast
import pathlib
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.core.task_manager import TaskStatus, format_step_line

OPI_ROOT = pathlib.Path(__file__).resolve().parents[1] / "opi"

#: De aanroepen waarvan het eerste (of tweede) tekstargument letterlijk op het scherm
#: komt als regel in de voortgangsindicator.
STEP_CALLS = {"add_task", "add_subtask", "update_current_step"}


def _progress() -> Any:
    """Een echte PersistentTaskProgressManager zonder zijn periodieke flush-lus."""
    with patch(
        "opi.core.persistent_task_progress.asyncio.get_running_loop",
        side_effect=RuntimeError,
    ):
        from opi.core.persistent_task_progress import PersistentTaskProgressManager

        return PersistentTaskProgressManager(
            task_id="task-under-test",
            project_name="test-project",
            task_service=MagicMock(),
        )


def _rows(progress: Any) -> list[tuple[str, str | None]]:
    """De stappen zoals de pagina ze toont: (naam, onderwerp), in volgorde."""
    return [(info["name"], info.get("subject")) for info in progress._subtasks.values()]


# ---------------------------------------------------------------------------
# 1. Het onderwerp staat naast de naam
# ---------------------------------------------------------------------------


def test_step_without_subject_is_unchanged() -> None:
    """Een stap die een keer per project loopt krijgt geen onderwerp."""
    progress = _progress()

    progress.add_task("Project aanmaken")

    assert _rows(progress) == [("Project aanmaken", None)]
    assert progress._current_step == "Project aanmaken"


def test_subject_is_stored_next_to_the_name_not_inside_it() -> None:
    progress = _progress()

    progress.add_task("Database klaarmaken", subject="productie")

    assert _rows(progress) == [("Database klaarmaken", "productie")]


def test_the_running_line_joins_name_and_subject() -> None:
    """De takenlijst houdt ze uit elkaar; de eenregelige "huidige stap" voegt ze samen."""
    progress = _progress()

    progress.add_task("Database klaarmaken", subject="productie")

    assert progress._current_step == "Database klaarmaken - productie"
    assert format_step_line("Database klaarmaken", None) == "Database klaarmaken"


def test_a_subtask_carries_its_subject_too() -> None:
    progress = _progress()
    parent = progress.add_task("Project uitrollen")

    progress.add_subtask(parent, "Wachten op de uitrol", subject="acceptatie")

    assert _rows(progress)[1] == ("Wachten op de uitrol", "acceptatie")
    assert progress._current_step == "Wachten op de uitrol - acceptatie"


def test_renaming_a_running_step_keeps_its_subject() -> None:
    """``update_task`` schrijft alleen de naam; het onderwerp is niet van hem."""
    progress = _progress()
    step = progress.add_task("Uitrol voorbereiden", subject="acceptatie")

    progress.update_task(step, "Uitgerold en gezond")

    assert _rows(progress) == [("Uitgerold en gezond", "acceptatie")]


@pytest.mark.asyncio
async def test_the_subject_reaches_the_database_payload() -> None:
    """Wat de pagina leest komt uit de flush, niet uit het geheugen van de worker."""
    progress = _progress()
    progress._task_service.update_progress = AsyncMock()
    progress.add_task("Database klaarmaken", subject="productie")

    await progress._flush_to_db()

    subtasks = progress._task_service.update_progress.await_args.kwargs["subtasks"]
    assert [(s["name"], s["subject"]) for s in subtasks] == [("Database klaarmaken", "productie")]


# ---------------------------------------------------------------------------
# 2. Twee deployments, dezelfde stap, uit elkaar te houden
# ---------------------------------------------------------------------------


def _redis_manager(progress: Any) -> Any:
    """Een echte RedisManager waarvan alleen de buitenwereld is afgevangen."""
    from opi.manager.redis_manager import RedisManager

    project_manager = MagicMock()
    project_manager.get_name = AsyncMock(return_value="test-project")
    project_manager.get_progress_manager = MagicMock(return_value=progress)
    project_manager._add_secret_to_create = MagicMock()

    manager = RedisManager(project_manager)
    manager._deployment_uses_redis = AsyncMock(return_value=True)
    manager._project_uses_namespace_redis = MagicMock(return_value=False)
    manager._get_redis_service_config = MagicMock(return_value={})
    manager._test_redis_connection = AsyncMock(return_value=True)
    manager._get_existing_redis_credentials_from_k8s = AsyncMock(return_value=None)
    manager._check_acl_user_exists = AsyncMock(return_value=False)
    manager._create_acl_user = AsyncMock()
    return manager


@pytest.mark.asyncio
async def test_two_deployments_are_told_apart_by_their_subject() -> None:
    """Dezelfde stap, twee keer, en de lijst zegt van welke deployment hij is."""
    progress = _progress()
    manager = _redis_manager(progress)
    project_data = {"name": "test-project"}

    for name in ("productie", "acceptatie"):
        await manager.create_resources_for_deployment(project_data, {"name": name, "cluster": "local"})

    assert _rows(progress) == [
        ("Redis-cache klaarmaken", "productie"),
        ("Redis-cache klaarmaken", "acceptatie"),
    ]
    # En de stap zelf blijft gewoon lopen en afronden.
    assert all(info["status"] == TaskStatus.COMPLETED.value for info in progress._subtasks.values())


# ---------------------------------------------------------------------------
# 3. De weergave toont het, en oude taken struikelen er niet over
# ---------------------------------------------------------------------------


def test_stored_steps_from_before_this_field_still_render() -> None:
    """Taken die al liepen dragen geen ``subject``; dat mag geen KeyError worden."""
    from opi.web.router import _build_task_hierarchy

    hierarchy = _build_task_hierarchy(
        [
            {"id": "a", "name": "Oude stap", "status": "completed"},
            {"id": "b", "name": "Nieuwe stap", "status": "running", "subject": "productie"},
        ]
    )

    assert [(t["name"], t["subject"]) for t in hierarchy] == [
        ("Oude stap", None),
        ("Nieuwe stap", "productie"),
    ]


def _fragment(layout: str, tasks: list[dict]) -> str:
    from opi.web.task_progress import render_progress_fragment

    request = SimpleNamespace(query_params={"layout": layout}, cookies={})
    return render_progress_fragment(
        request,
        {
            "task_id": "t-1",
            "progress_url": "/projects/demo/task-progress/t-1",
            "progress": 0,
            "current_step": "Bezig",
            "tasks": tasks,
            "status": "running",
        },
    )


@pytest.mark.parametrize("layout", ["nldd", "roos"])
def test_the_fragment_shows_the_subject_next_to_the_step(layout: str) -> None:
    html = _fragment(
        layout,
        [{"name": "Redis-cache klaarmaken", "status": "running", "subject": "productie", "subtasks": []}],
    )

    assert "Redis-cache klaarmaken" in html
    assert "productie" in html


@pytest.mark.parametrize("layout", ["nldd", "roos"])
def test_subject_and_error_share_the_second_line(layout: str) -> None:
    """Een mislukte stap zegt nog steeds voor welke deployment hij mislukte."""
    html = _fragment(
        layout,
        [
            {
                "name": "Redis-cache klaarmaken",
                "status": "failed",
                "subject": "productie",
                "error": "geen verbinding",
                "subtasks": [],
            }
        ],
    )

    assert "productie - geen verbinding" in html


@pytest.mark.parametrize("layout", ["nldd", "roos"])
def test_a_subject_is_shown_not_executed(layout: str) -> None:
    """Het onderwerp komt uit het projectbestand, dus het is tekst en geen sjabloon."""
    html = _fragment(
        layout,
        [{"name": "Redis-cache klaarmaken", "status": "running", "subject": "{{ 7 * 7 }}", "subtasks": []}],
    )

    assert "49" not in html


# ---------------------------------------------------------------------------
# 4. De regels zijn Nederlands, en overal hetzelfde
# ---------------------------------------------------------------------------


def _step_literals() -> list[tuple[str, str]]:
    """Elke letterlijke stapnaam in opi/, als (bestand, tekst).

    Op de BRON en niet op een lijst in de test: een nieuwe Engelse regel komt er
    daardoor niet ongemerkt bij, en een verwijderde stap laat de scan niet omvallen.
    """
    found: list[tuple[str, str]] = []
    for path in OPI_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in STEP_CALLS:
                continue
            # add_subtask zet de bovenliggende taak voorop; de naam is dan het tweede
            # argument. Alleen letterlijke teksten zijn hier te beoordelen.
            found.extend(
                (str(path.relative_to(OPI_ROOT)), arg.value)
                for arg in node.args
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
            )
    return found


#: Woorden die alleen in een Engelse regel voorkomen. Klein en concreet gehouden: dit
#: hoort een vergeten vertaling te vangen, geen leenwoord als "backup" of "deployment".
ENGLISH_MARKERS = (
    "processing",
    "creating",
    "deleting",
    "executing",
    "initializing",
    "validating",
    "deploying",
    "pushing",
    "adding",
    "updating",
    "applying",
    "configuring",
    "upserting",
    "monitoring",
)


def test_no_english_left_in_the_progress_lines() -> None:
    offenders = [
        (path, text) for path, text in _step_literals() for marker in ENGLISH_MARKERS if marker in text.lower()
    ]

    assert offenders == []


def _deploy_step_names(relative_path: str) -> set[str]:
    """De namen van de herverwerkingsstap in een handlerbestand.

    Het anker is de toewijzing: elk van deze stappen heet in de bron ``deploy_task``,
    en dat is wat "dezelfde stap" hier betekent. Een van de zeven anders vertalen komt
    er daardoor uit, ook als die vertaling keurig Nederlands is.
    """
    tree = ast.parse((OPI_ROOT / relative_path).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "deploy_task" not in targets:
            continue
        for arg in node.value.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                names.add(arg.value)
    return names


def test_processing_a_project_is_called_the_same_thing_everywhere() -> None:
    """Twee vertalingen van dezelfde stap lezen op het scherm als twee dingen."""
    assert _deploy_step_names("core/task_handlers_components.py") == {
        "Project verwerken",
        "Deployment verwerken",
    }
    # In dit bestand heet de uitrol bij het aanmaken ook deploy_task; die staat er
    # naast en hoort een eigen naam te houden.
    assert "Deployment verwerken" in _deploy_step_names("core/task_handlers_project.py")
