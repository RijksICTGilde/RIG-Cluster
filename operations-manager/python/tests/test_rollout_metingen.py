"""Tests voor de meting van een rollout: duur per stap en fase-histogram.

De meting is de basis onder RC-418: elke versnelling erna wordt hieraan afgelezen.
Daarom toetsen deze tests de drie dingen waar een dashboard op staat: dat een stap zijn
begin- en eindtijd vastlegt, dat de voortgangsweergave daar een duur van maakt, en dat een
fase die loopt in het histogram terechtkomt, ook als hij faalt.
"""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from opi.core.metrics import (
    PHASE_ARGO_WAIT_SYNCED,
    PHASE_GENERATE,
    ROLLOUT_PHASES,
    observe_rollout_phase,
    rollout_phase,
    rollout_phase_seconds,
    set_rollout_task_type,
)
from opi.core.template_helpers import format_step_duration
from opi.web.router import _build_task_hierarchy, _step_duration_seconds, _total_duration_seconds


def _progress() -> Any:
    """Een echte voortgangsmanager zonder zijn periodieke flush-lus."""
    with patch(
        "opi.core.persistent_task_progress.asyncio.get_running_loop",
        side_effect=RuntimeError,
    ):
        from opi.core.persistent_task_progress import PersistentTaskProgressManager

        return PersistentTaskProgressManager(
            task_id="task-meting",
            project_name="test-project",
            task_service=MagicMock(),
        )


def _phase_count(task_type: str, phase: str) -> float:
    for metric in rollout_phase_seconds.collect():
        for sample in metric.samples:
            if (
                sample.name.endswith("_count")
                and sample.labels.get("task_type") == task_type
                and sample.labels.get("phase") == phase
            ):
                return sample.value
    return 0.0


class TestStapTijdstempels:
    def test_een_stap_krijgt_een_starttijd_en_pas_bij_afronden_een_eindtijd(self) -> None:
        progress = _progress()
        step_id = progress.add_task("Diensten en manifesten bijwerken")

        assert progress._subtasks[step_id]["started_at"]
        assert progress._subtasks[step_id]["completed_at"] is None

        progress.complete_task(step_id)
        assert progress._subtasks[step_id]["completed_at"]

    def test_een_mislukte_stap_krijgt_ook_een_eindtijd(self) -> None:
        progress = _progress()
        step_id = progress.add_task("ArgoCD laten uitrollen")
        progress.fail_task(step_id, "time-out na 300s")

        assert progress._subtasks[step_id]["completed_at"]

    def test_een_substap_krijgt_dezelfde_tijdstempels(self) -> None:
        progress = _progress()
        parent = progress.add_task("Wachten tot ArgoCD gesynchroniseerd is")
        child = progress.add_subtask(parent, "deployment-1")
        progress.complete_task(child)

        assert progress._subtasks[child]["started_at"]
        assert progress._subtasks[child]["completed_at"]


class TestDurationInTheView:
    def test_a_step_shows_no_duration(self) -> None:
        subtasks = [
            {
                "id": "a",
                "name": "Projectbestand ophalen",
                "status": "completed",
                "started_at": "2026-11-04T10:00:00+00:00",
                "completed_at": "2026-11-04T10:00:12.500000+00:00",
            }
        ]
        [stap] = _build_task_hierarchy(subtasks)
        assert "duration_seconds" not in stap

    def test_the_total_runs_from_the_first_start_to_the_last_end(self) -> None:
        subtasks = [
            {"id": "a", "started_at": "2026-11-04T10:00:00+00:00", "completed_at": "2026-11-04T10:00:02+00:00"},
            {"id": "b", "started_at": "2026-11-04T10:00:02+00:00", "completed_at": "2026-11-04T10:00:17.500000+00:00"},
        ]
        assert _total_duration_seconds(subtasks) == pytest.approx(17.5)

    def test_without_timestamps_there_is_no_total(self) -> None:
        assert _total_duration_seconds([{"id": "a", "status": "completed"}]) is None

    def test_een_onleesbaar_tijdstempel_levert_geen_fout_op(self) -> None:
        assert _step_duration_seconds({"started_at": "gisteren", "completed_at": "vandaag"}) is None

    @pytest.mark.parametrize(
        ("seconden", "verwacht"),
        [(0.44, "0,4 s"), (12.5, "12,5 s"), (59.9, "59,9 s"), (60, "1 min 0 s"), (185, "3 min 5 s"), (None, "")],
    )
    def test_de_duur_leest_als_nederlandse_tekst(self, seconden: float | None, verwacht: str) -> None:
        assert format_step_duration(seconden) == verwacht


class TestFaseHistogram:
    def test_alle_fases_van_het_plan_hebben_een_naam(self) -> None:
        assert len(set(ROLLOUT_PHASES)) == len(ROLLOUT_PHASES)
        assert PHASE_GENERATE in ROLLOUT_PHASES
        assert PHASE_ARGO_WAIT_SYNCED in ROLLOUT_PHASES

    def test_een_fase_wordt_gelabeld_met_het_taaktype(self) -> None:
        set_rollout_task_type("upsert_deployment")
        voor = _phase_count("upsert_deployment", PHASE_GENERATE)
        with rollout_phase(PHASE_GENERATE):
            pass
        assert _phase_count("upsert_deployment", PHASE_GENERATE) == voor + 1

    def test_een_fase_die_faalt_wordt_ook_gemeten(self) -> None:
        set_rollout_task_type("refresh_project")
        voor = _phase_count("refresh_project", PHASE_GENERATE)
        with pytest.raises(RuntimeError), rollout_phase(PHASE_GENERATE):
            raise RuntimeError("kustomization kapot")
        assert _phase_count("refresh_project", PHASE_GENERATE) == voor + 1

    def test_een_zelf_gemeten_duur_landt_in_hetzelfde_histogram(self) -> None:
        set_rollout_task_type("upsert_deployment")
        voor = _phase_count("upsert_deployment", PHASE_ARGO_WAIT_SYNCED)
        observe_rollout_phase(PHASE_ARGO_WAIT_SYNCED, 4.2)
        assert _phase_count("upsert_deployment", PHASE_ARGO_WAIT_SYNCED) == voor + 1
