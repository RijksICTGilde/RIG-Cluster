"""Tests voor de cluster-brede pod-watch die OOM-kills tijdens runtime opmerkt."""

import asyncio
import contextlib
import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.services.catalog.base import SERVICE_ROLE_LABEL_KEY, all_application_pods_selector
from opi.services.oom_watcher import (
    _MAX_WATCH_BUFFER_BYTES,
    OOM_MAX_TUNE_ATTEMPTS,
    POD_WATCH_MAX_RECONNECT_SECONDS,
    ObservedOomKill,
    OomMetricSweeper,
    OomPodWatcher,
    _claim_oom_kill,
    _handled_oom_kills,
    _last_tuned_pod_template_hash,
    _oom_tune_attempts,
    forget_pod_restarts,
    handle_observed_oom_kill,
    observe_pod_restarts,
    read_pod_oom_kill,
    read_watch_events,
    resolve_oom_component,
    run_oom_metric_sweep,
)

WATCHER = "opi.services.oom_watcher"


@pytest.fixture(autouse=True)
def _clean_watcher_state():
    """De watcher-staat leeft op moduleniveau; zonder opruimen lekt hij tussen toetsen."""
    _handled_oom_kills.clear()
    _oom_tune_attempts.clear()
    _last_tuned_pod_template_hash.clear()
    yield
    _handled_oom_kills.clear()
    _oom_tune_attempts.clear()
    _last_tuned_pod_template_hash.clear()


# ---------------------------------------------------------------------------
# Pod-objecten zoals het incident van 29 september ze opleverde
# ---------------------------------------------------------------------------


def _pr1644_pod(
    *,
    restart_count: int,
    reason: str = "OOMKilled",
    exit_code: int = 137,
    finished_at: str = "2026-09-29T09:14:52Z",
    pod_name: str = "pr1644-api-7c9f4d8b6d-x2klm",
    container_name: str = "app",
    namespace: str = "rig-regel-k4c",
    labels: dict[str, str] | None = None,
) -> dict:
    """De pod-vorm van het pr1644-incident: crashloop op OOMKilled, nul events.

    De vorm van het ``terminated``-blok is 29 september nagemeten tegen een echte
    OOM-kill op de sandbox: ``reason: OOMKilled``, ``exitCode: 137``, ``startedAt`` en
    ``finishedAt``, en geen ``message``. Diezelfde meting gaf 27 events voor die pod,
    waarvan nul met OOM in de reason.
    """
    if labels is None:
        labels = {
            "app": "pr1644-api",
            "deployment": "pr1644",
            "project": "regel-k4c",
            "component": "application",
            "pod-template-hash": "7c9f4d8b6d",
        }
    return {
        "metadata": {"name": pod_name, "namespace": namespace, "labels": labels},
        "status": {
            "containerStatuses": [
                {
                    "name": container_name,
                    "restartCount": restart_count,
                    "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                    "lastState": {
                        "terminated": {
                            "reason": reason,
                            "exitCode": exit_code,
                            "startedAt": "2026-09-29T09:14:30Z",
                            "finishedAt": finished_at,
                        }
                    },
                }
            ]
        },
    }


def _waker_pod(*, restart_count: int) -> dict:
    """De pod van sleep-mode's waker, met de labels die hij met opzet deelt.

    ``manifests.py`` geeft hem de ``app``-, ``deployment``- en ``project``-labels van het
    component (hij neemt diens Service over) plus ``component: application``, en
    ``zad-role: waker`` om de twee Deployments uit elkaar te houden.
    """
    return _pr1644_pod(
        restart_count=restart_count,
        pod_name="pr1644-api-waker-59c7d8f4b7-qq2mn",
        labels={
            "app": "pr1644-api",
            "deployment": "pr1644",
            "project": "regel-k4c",
            "component": "application",
            SERVICE_ROLE_LABEL_KEY: "waker",
            "pod-template-hash": "59c7d8f4b7",
        },
    )


_WATCH_ERROR_EVENT = {
    "type": "ERROR",
    "object": {
        "kind": "Status",
        "apiVersion": "v1",
        "metadata": {},
        "status": "Failure",
        "message": "too old resource version: 1 (11815647)",
        "reason": "Expired",
        "code": 410,
    },
}
"""Wat de stream levert als de watch verlopen is.

Nagemeten op 29 september: `kubectl get --raw "/api/v1/pods?watch=1&resourceVersion=1"`
geeft dit object, en `kubectl get pods -A --watch --output-watch-events` schrijft zo'n
ERROR-event naar stdout (gemeten tegen een API-server die de stream liet mislukken).
Het is een dict, dus de isinstance-grendel in ``_handle_event`` laat hem door, en zijn
``status`` is een string in plaats van een blok.
"""


def _project_data() -> dict:
    return {
        "name": "regel-k4c",
        "deployments": [
            {
                "name": "pr1644",
                "cluster": "local",
                "namespace": "regel-k4c",
                "components": [
                    {"reference": "api"},
                    {"reference": "frontend"},
                ],
            }
        ],
    }


def _kill(**overrides) -> ObservedOomKill:
    fields = {
        "namespace": "rig-regel-k4c",
        "pod_name": "pr1644-api-7c9f4d8b6d-x2klm",
        "container_name": "app",
        "finished_at": "2026-09-29T09:14:52Z",
        "project_name": "regel-k4c",
        "deployment_name": "pr1644",
        "app_name": "pr1644-api",
        "pod_template_hash": "7c9f4d8b6d",
    }
    fields.update(overrides)
    return ObservedOomKill(**fields)


# ---------------------------------------------------------------------------
# observe_pod_restarts: de trigger
# ---------------------------------------------------------------------------


class TestObservePodRestarts:
    def test_first_sighting_only_seeds(self):
        """De eerste LIST vult de cache en meldt niets, ook niet bij een oude OOM.

        Zonder dit vuurt elke pod die ooit is omgevallen zodra OPI start.
        """
        counts: dict[str, int] = {}
        kills = observe_pod_restarts(_pr1644_pod(restart_count=9), counts)

        assert kills == []
        assert counts["rig-regel-k4c/pr1644-api-7c9f4d8b6d-x2klm/app"] == 9

    def test_risen_restart_count_with_oomkilled_reports_the_kill(self):
        counts: dict[str, int] = {}
        observe_pod_restarts(_pr1644_pod(restart_count=9), counts)
        kills = observe_pod_restarts(_pr1644_pod(restart_count=10), counts)

        assert len(kills) == 1
        kill = kills[0]
        assert kill.namespace == "rig-regel-k4c"
        assert kill.pod_name == "pr1644-api-7c9f4d8b6d-x2klm"
        assert kill.container_name == "app"
        assert kill.finished_at == "2026-09-29T09:14:52Z"
        assert kill.project_name == "regel-k4c"
        assert kill.deployment_name == "pr1644"
        assert kill.app_name == "pr1644-api"
        assert kill.pod_template_hash == "7c9f4d8b6d"
        assert counts["rig-regel-k4c/pr1644-api-7c9f4d8b6d-x2klm/app"] == 10

    def test_unchanged_restart_count_reports_nothing(self):
        """lastState blijft dezelfde kill beschrijven zolang de pod leeft.

        Trigger op de aanwezigheid van terminated zou die ene kill blijven melden bij
        elke volgende update van de pod.
        """
        counts: dict[str, int] = {}
        observe_pod_restarts(_pr1644_pod(restart_count=10), counts)
        assert observe_pod_restarts(_pr1644_pod(restart_count=10), counts) == []
        assert observe_pod_restarts(_pr1644_pod(restart_count=10), counts) == []

    @pytest.mark.parametrize(
        ("reason", "logged"),
        [
            ("Completed", "Completed"),
            ("Error", "Error"),
            ("ContainerStatusUnknown", "ContainerStatusUnknown"),
            ("", "unknown"),
        ],
    )
    def test_other_restart_reasons_are_ignored(self, reason, logged, caplog):
        """Een gewone herstart gaat het tune-pad niet in: die heeft zijn eigen pad.

        De reden staat in de logregel, want die regel is het enige spoor dat de watch een
        herstart zag en hem liet liggen. Zonder de reden is hij niet te gebruiken bij de
        vraag waarom een OOM niet getuned werd.
        """
        counts: dict[str, int] = {}
        observe_pod_restarts(_pr1644_pod(restart_count=1, reason=reason, exit_code=0), counts)
        with caplog.at_level(logging.DEBUG, logger=WATCHER):
            kills = observe_pod_restarts(_pr1644_pod(restart_count=2, reason=reason, exit_code=0), counts)

        assert kills == []
        assert f"reason {logged}" in caplog.text

    def test_each_container_is_tracked_apart(self):
        """Een sidecar die omvalt mag de teller van de app-container niet verzetten."""
        counts: dict[str, int] = {}
        pod = _pr1644_pod(restart_count=3)
        pod["status"]["containerStatuses"].append(
            {
                "name": "auth-wall",
                "restartCount": 0,
                "state": {"running": {}},
                "lastState": {},
            }
        )
        observe_pod_restarts(pod, counts)

        bumped = _pr1644_pod(restart_count=4)
        bumped["status"]["containerStatuses"].append(
            {"name": "auth-wall", "restartCount": 0, "state": {"running": {}}, "lastState": {}}
        )
        kills = observe_pod_restarts(bumped, counts)

        assert [k.container_name for k in kills] == ["app"]
        assert counts["rig-regel-k4c/pr1644-api-7c9f4d8b6d-x2klm/auth-wall"] == 0

    def test_a_restart_count_that_drops_reports_nothing(self):
        """Een pod die met dezelfde naam terugkomt begint weer op 0; dat is geen kill."""
        counts: dict[str, int] = {}
        observe_pod_restarts(_pr1644_pod(restart_count=10), counts)
        assert observe_pod_restarts(_pr1644_pod(restart_count=0), counts) == []

    def test_two_containers_that_both_fell_over_are_both_reported(self):
        """Eén pod-update kan twee kills dragen; stoppen bij de eerste laat de ander liggen."""
        counts: dict[str, int] = {}
        sidecar = {
            "name": "auth-wall",
            "restartCount": 0,
            "state": {"running": {}},
            "lastState": {
                "terminated": {
                    "reason": "OOMKilled",
                    "exitCode": 137,
                    "startedAt": "2026-09-29T09:19:40Z",
                    "finishedAt": "2026-09-29T09:20:11Z",
                }
            },
        }
        seed = _pr1644_pod(restart_count=9)
        seed["status"]["containerStatuses"].append(dict(sidecar))
        observe_pod_restarts(seed, counts)

        bumped = _pr1644_pod(restart_count=10)
        bumped["status"]["containerStatuses"].append({**sidecar, "restartCount": 1})
        kills = observe_pod_restarts(bumped, counts)

        assert [k.container_name for k in kills] == ["app", "auth-wall"]
        assert [k.finished_at for k in kills] == ["2026-09-29T09:14:52Z", "2026-09-29T09:20:11Z"]

    def test_a_pod_without_container_statuses_reports_nothing(self):
        """Een pod die nog aan het starten is heeft alleen een fase, geen containers."""
        counts: dict[str, int] = {}
        pending = {
            "metadata": {"name": "pr1644-api-7c9f4d8b6d-nieuw", "namespace": "rig-regel-k4c", "labels": {}},
            "status": {"phase": "Pending"},
        }

        assert observe_pod_restarts(pending, counts) == []
        assert counts == {}

    def test_a_pod_without_our_project_labels_still_yields_the_kill(self):
        """De projectlabels bepalen niet OF er een kill is, alleen of er iets te tunen valt.

        Wat er ontbreekt wordt verderop beslist (``handle_observed_oom_kill``), zodat een
        applicatiepod zonder projectlabels een logregel oplevert en niet stil verdwijnt.
        """
        counts: dict[str, int] = {}
        bare = {"component": "application"}
        observe_pod_restarts(_pr1644_pod(restart_count=1, labels=bare), counts)
        kills = observe_pod_restarts(_pr1644_pod(restart_count=2, labels=bare), counts)

        assert len(kills) == 1
        assert kills[0].project_name == ""
        assert kills[0].deployment_name == ""
        assert kills[0].app_name == ""
        assert kills[0].pod_template_hash is None
        assert kills[0].finished_at == "2026-09-29T09:14:52Z"

    def test_a_service_owned_pod_yields_nothing(self):
        """De waker van sleep-mode draagt de app-, deployment- en projectlabels van het
        component dat hij vervangt, plus ``component: application``. Alleen ``zad-role``
        onderscheidt hem, en hij draait op een vaste 64Mi: zonder die grendel verhoogt
        zijn OOM de limit van het echte component.
        """
        counts: dict[str, int] = {}
        observe_pod_restarts(_waker_pod(restart_count=1), counts)

        assert observe_pod_restarts(_waker_pod(restart_count=2), counts) == []

    def test_a_pod_that_is_not_an_application_pod_yields_nothing(self):
        """De andere helft van de selector: wat niet van ons is heeft niets te tunen."""
        counts: dict[str, int] = {}
        labels = {"app": "iets-anders"}
        observe_pod_restarts(_pr1644_pod(restart_count=1, labels=labels), counts)

        assert observe_pod_restarts(_pr1644_pod(restart_count=2, labels=labels), counts) == []


class TestForgetPodRestarts:
    def test_only_the_deleted_pod_is_dropped(self):
        counts: dict[str, int] = {}
        observe_pod_restarts(_pr1644_pod(restart_count=1), counts)
        observe_pod_restarts(_pr1644_pod(restart_count=1, pod_name="pr1644-api-other"), counts)

        forget_pod_restarts(_pr1644_pod(restart_count=1), counts)

        assert list(counts) == ["rig-regel-k4c/pr1644-api-other/app"]

    def test_a_pod_whose_name_the_deleted_one_starts_keeps_its_counts(self):
        """Zonder de scheiding in de sleutel neemt een verwijderde pod de buur mee wiens
        naam met dezelfde tekens begint, en die buur wordt daarna opnieuw gezaaid: zijn
        volgende OOM is dan "de eerste waarneming" en verdwijnt."""
        counts: dict[str, int] = {}
        observe_pod_restarts(_pr1644_pod(restart_count=1, pod_name="pr1644-api-1"), counts)
        observe_pod_restarts(_pr1644_pod(restart_count=4, pod_name="pr1644-api-10"), counts)

        forget_pod_restarts(_pr1644_pod(restart_count=1, pod_name="pr1644-api-1"), counts)

        assert list(counts) == ["rig-regel-k4c/pr1644-api-10/app"]

    def test_a_pod_in_another_namespace_keeps_its_counts(self):
        counts: dict[str, int] = {}
        observe_pod_restarts(_pr1644_pod(restart_count=1), counts)
        observe_pod_restarts(_pr1644_pod(restart_count=1, namespace="rig-ander-project"), counts)

        forget_pod_restarts(_pr1644_pod(restart_count=1), counts)

        assert list(counts) == ["rig-ander-project/pr1644-api-7c9f4d8b6d-x2klm/app"]


# ---------------------------------------------------------------------------
# De dedupe op finishedAt
# ---------------------------------------------------------------------------


class TestClaimOomKill:
    def test_the_same_kill_is_claimed_once(self):
        assert _claim_oom_kill(_kill()) is True
        assert _claim_oom_kill(_kill()) is False

    def test_a_later_kill_on_the_same_container_is_its_own(self):
        assert _claim_oom_kill(_kill()) is True
        assert _claim_oom_kill(_kill(finished_at="2026-09-29T09:19:07Z")) is True

    def test_the_ledger_is_bounded(self):
        for i in range(2500):
            _claim_oom_kill(_kill(finished_at=f"2026-09-29T09:{i:04d}Z"))
        assert len(_handled_oom_kills) <= 2000


# ---------------------------------------------------------------------------
# Van pod naar component
# ---------------------------------------------------------------------------


class TestResolveOomComponent:
    def test_matches_the_component_behind_the_app_label(self):
        assert resolve_oom_component(_kill(), _project_data()) == "api"

    def test_unknown_deployment_matches_nothing(self):
        assert resolve_oom_component(_kill(deployment_name="pr9999"), _project_data()) is None

    def test_a_deployment_on_another_cluster_is_not_ours(self):
        """Elke OPI beheert alleen zijn eigen cluster.

        Het buurcluster hier deelt het namespace-prefix ("rig-") met het onze. Met
        odcn-production ernaast zou de namespacetoets hieronder dit al afwijzen en zou
        het weghalen van de clustertoets zelf geen enkele toets rood maken.
        """
        data = _project_data()
        data["deployments"][0]["cluster"] = "sandboxed-local"
        assert resolve_oom_component(_kill(), data) is None

    def test_a_namespace_that_does_not_match_the_project_file_is_refused(self):
        """Het label zegt van wie de pod is, het projectbestand waar hij hoort te staan."""
        assert resolve_oom_component(_kill(namespace="rig-iemand-anders"), _project_data()) is None

    def test_a_disabled_component_is_not_tuned(self):
        data = _project_data()
        data["deployments"][0]["components"][0]["disabled"] = True
        assert resolve_oom_component(_kill(), data) is None

    def test_an_app_label_no_component_claims_matches_nothing(self):
        """Helmfile-deployments draaien in onze namespace maar staan niet in het bestand."""
        assert resolve_oom_component(_kill(app_name="grist-server"), _project_data()) is None

    @pytest.mark.parametrize("missing", ["cluster", "namespace"])
    def test_a_deployment_missing_its_cluster_or_namespace_matches_nothing(self, missing):
        """Een onvolledig deploymentblok mag geen uitzondering worden.

        De uitzondering zou uit ``_handle_event`` omhoog komen, daar de hele stream
        afbreken en de watch in een herverbindlus zetten: een project met een half
        deploymentblok legt dan de detectie voor alle projecten plat.
        """
        data = _project_data()
        del data["deployments"][0][missing]
        assert resolve_oom_component(_kill(), data) is None

    def test_a_deployment_without_components_matches_nothing(self):
        data = _project_data()
        data["deployments"][0]["components"] = []
        assert resolve_oom_component(_kill(), data) is None


# ---------------------------------------------------------------------------
# De koppeling aan de bestaande tune
# ---------------------------------------------------------------------------


class TestHandleObservedOomKill:
    @pytest.mark.asyncio
    async def test_a_resolved_kill_reaches_the_shared_tune(self):
        with (
            patch(f"{WATCHER}.get_project_data", return_value=(_project_data(), "regel-k4c.yaml")),
            patch(f"{WATCHER}.apply_oom_tune", new=AsyncMock(return_value=True)) as tune,
        ):
            assert await handle_observed_oom_kill(_kill(), source="pod watch") is True

        tune.assert_awaited_once()
        args, kwargs = tune.await_args
        assert args[0] == "regel-k4c"
        assert args[1] == "pr1644"
        assert args[2] == ["api"]
        assert args[3] == {"pr1644-api": "7c9f4d8b6d"}
        assert "pr1644-api-7c9f4d8b6d-x2klm" in kwargs["source"]

    @pytest.mark.asyncio
    async def test_the_same_kill_only_tunes_once(self):
        """Een relist speelt elke pod opnieuw af; dedupe op finishedAt vangt dat."""
        with (
            patch(f"{WATCHER}.get_project_data", return_value=(_project_data(), "regel-k4c.yaml")),
            patch(f"{WATCHER}.apply_oom_tune", new=AsyncMock(return_value=True)) as tune,
        ):
            await handle_observed_oom_kill(_kill(), source="pod watch")
            assert await handle_observed_oom_kill(_kill(), source="metric sweep") is False

        assert tune.await_count == 1

    @pytest.mark.asyncio
    async def test_a_pod_without_project_labels_is_only_reported(self):
        with patch(f"{WATCHER}.apply_oom_tune", new=AsyncMock()) as tune:
            assert await handle_observed_oom_kill(_kill(project_name="", app_name=""), source="pod watch") is False
        tune.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_project_this_instance_does_not_know_is_only_reported(self):
        with (
            patch(f"{WATCHER}.get_project_data", side_effect=ValueError("not found")),
            patch(f"{WATCHER}.apply_oom_tune", new=AsyncMock()) as tune,
        ):
            assert await handle_observed_oom_kill(_kill(), source="pod watch") is False
        tune.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_kill_on_the_generation_the_previous_tune_answered_is_refused(self):
        """De bestaande pod-generatiegrendel geldt ook hier: de verhoging rolt nog uit."""
        _last_tuned_pod_template_hash["regel-k4c/pr1644/pr1644-api"] = "7c9f4d8b6d"
        with (
            patch(f"{WATCHER}.get_project_data", return_value=(_project_data(), "regel-k4c.yaml")),
            patch(f"{WATCHER}.apply_oom_tune", new=AsyncMock()) as tune,
        ):
            assert await handle_observed_oom_kill(_kill(), source="pod watch") is False
        tune.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_kill_on_a_new_generation_is_fresh_evidence(self):
        _last_tuned_pod_template_hash["regel-k4c/pr1644/pr1644-api"] = "6b8e3c7a5c"
        with (
            patch(f"{WATCHER}.get_project_data", return_value=(_project_data(), "regel-k4c.yaml")),
            patch(f"{WATCHER}.apply_oom_tune", new=AsyncMock(return_value=True)) as tune,
        ):
            assert await handle_observed_oom_kill(_kill(), source="pod watch") is True
        tune.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_tune_that_committed_nothing_is_not_retried_on_the_same_kill(self):
        """De dedupe registreert een kill zodra hij GELEZEN is, niet zodra hij getuned is.

        Anders blijft dezelfde kill op elke pod-update terugkomen zolang de tune niets
        oplevert, en dat is precies de situatie waarin de limit op het plafond zit.
        """
        with (
            patch(f"{WATCHER}.get_project_data", return_value=(_project_data(), "regel-k4c.yaml")),
            patch(f"{WATCHER}.apply_oom_tune", new=AsyncMock(return_value=False)) as tune,
        ):
            assert await handle_observed_oom_kill(_kill(), source="pod watch") is False
            assert await handle_observed_oom_kill(_kill(), source="pod watch") is False

        assert tune.await_count == 1

    @pytest.mark.asyncio
    async def test_the_shared_budget_stops_a_flapping_pod(self, caplog):
        """Een pod die blijft omvallen wordt niet onbeperkt omhoog geschroefd.

        Dit loopt door de echte ``apply_oom_tune``: de rem is van het fire-and-forget-pad
        en de watch mag hem niet omzeilen. Elke kill draagt een eigen generatie, want
        anders zou de pod-generatiegrendel de tweede al afwijzen en zegt dit niets over
        het budget.
        """
        observation = MagicMock(requeue_refresh=True, failures=[])
        with (
            patch(f"{WATCHER}.get_project_data", return_value=(_project_data(), "regel-k4c.yaml")),
            patch(f"{WATCHER}._queue_refresh_task", new=AsyncMock()),
            patch(
                "opi.services.deployment_observation.run_after_sync_observation",
                new=AsyncMock(return_value=observation),
            ) as observe,
            caplog.at_level(logging.WARNING, logger="opi.services.oom_watcher"),
        ):
            committed = [
                await handle_observed_oom_kill(
                    _kill(finished_at=f"2026-09-29T09:1{n}:52Z", pod_template_hash=f"gen-{n}"),
                    source="pod watch",
                )
                for n in range(OOM_MAX_TUNE_ATTEMPTS + 1)
            ]

        assert committed == [True] * OOM_MAX_TUNE_ATTEMPTS + [False]
        assert observe.await_count == OOM_MAX_TUNE_ATTEMPTS
        assert "budget" in caplog.text


# ---------------------------------------------------------------------------
# De stream zelf
# ---------------------------------------------------------------------------


class _ChunkStream:
    """Een StreamReader-achtige die de opgegeven brokken teruggeeft en dan eindigt."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    async def read(self, _n: int) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""


class TestReadWatchEvents:
    @pytest.mark.asyncio
    async def test_documents_split_across_chunks_are_reassembled(self):
        """Een brokgrens valt waar de socket breekt, niet op een documentgrens.

        Hier bewust met de ingesprongen vorm, de moeilijkste: kubectl schrijft elk
        event compact op een eigen regel, maar wie op regels leest gaat alsnog stuk
        zodra een document over twee brokken valt.
        """
        blob = "".join(
            json.dumps({"type": t, "object": _pr1644_pod(restart_count=n)}, indent=2)
            for t, n in (("ADDED", 9), ("MODIFIED", 10))
        )
        raw = blob.encode()
        chunks = [raw[:17], raw[17:400], raw[400:]]

        events = [e async for e in read_watch_events(_ChunkStream(chunks))]

        assert [e["type"] for e in events] == ["ADDED", "MODIFIED"]
        assert events[1]["object"]["status"]["containerStatuses"][0]["restartCount"] == 10

    @pytest.mark.asyncio
    async def test_an_empty_stream_yields_nothing(self):
        assert [e async for e in read_watch_events(_ChunkStream([]))] == []

    @pytest.mark.asyncio
    async def test_unparsable_output_is_dropped_instead_of_growing_forever(self, caplog):
        """Een halfgeschreven object (kubectl gekild) is voor de decoder niet van een
        onafgemaakt document te onderscheiden, dus zonder plafond groeit de buffer tot
        het proces herstart. Na het weggooien moet de stream het weer oppakken."""
        garbage = b"}" * (_MAX_WATCH_BUFFER_BYTES + 1)
        good = json.dumps({"type": "MODIFIED", "object": _pr1644_pod(restart_count=10)}).encode()

        with caplog.at_level(logging.WARNING, logger="opi.services.oom_watcher"):
            events = [e async for e in read_watch_events(_ChunkStream([garbage, good]))]

        assert [e["type"] for e in events] == ["MODIFIED"]
        assert "unparsable watch output" in caplog.text

    @pytest.mark.asyncio
    async def test_a_document_that_is_not_an_object_is_skipped(self):
        stream = _ChunkStream([b'"los" {"type": "MODIFIED", "object": {}}'])
        assert [e async for e in read_watch_events(stream)] == [{"type": "MODIFIED", "object": {}}]


class TestPodWatcherEvents:
    @pytest.mark.asyncio
    async def test_added_seeds_and_modified_tunes(self):
        watcher = OomPodWatcher(cluster="local")
        with patch(f"{WATCHER}.handle_observed_oom_kill", new=AsyncMock(return_value=True)) as handle:
            await watcher._handle_event({"type": "ADDED", "object": _pr1644_pod(restart_count=9)})
            handle.assert_not_awaited()
            await watcher._handle_event({"type": "MODIFIED", "object": _pr1644_pod(restart_count=10)})
            handle.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_deleted_drops_the_pod_from_the_cache(self):
        watcher = OomPodWatcher(cluster="local")
        await watcher._handle_event({"type": "ADDED", "object": _pr1644_pod(restart_count=9)})
        await watcher._handle_event({"type": "DELETED", "object": _pr1644_pod(restart_count=9)})

        assert watcher._restart_counts == {}

    @pytest.mark.asyncio
    async def test_an_error_event_is_not_read_as_a_pod(self, caplog):
        """Bij een verlopen watch stuurt de API-server een Status in plaats van een pod.

        Die mag geen cache-regel opleveren en geen tune, alleen het einde van de stream.
        De warning eronder is het enige spoor dat een operator van zo'n 410 te zien
        krijgt: de watch herstelt zichzelf, dus zonder die regel met reason en code is
        een relist-lus onzichtbaar.
        """
        watcher = OomPodWatcher(cluster="local")
        with (
            patch(f"{WATCHER}.handle_observed_oom_kill", new=AsyncMock()) as handle,
            caplog.at_level(logging.WARNING, logger="opi.services.oom_watcher"),
        ):
            assert await watcher._handle_event(_WATCH_ERROR_EVENT) is False

        handle.assert_not_awaited()
        assert watcher._restart_counts == {}
        assert "Expired" in caplog.text
        assert "410" in caplog.text
        assert "too old resource version" in caplog.text

    @pytest.mark.asyncio
    async def test_an_error_event_without_a_status_object_ends_the_stream_too(self):
        """Het ERROR-event is de enige plek waar ``object`` geen pod is, dus ook de plek
        waar een andere vorm dan een Status-blok kan opduiken. Dat mag geen uitzondering
        geven: de stream is hoe dan ook voorbij."""
        watcher = OomPodWatcher(cluster="local")

        assert await watcher._handle_event({"type": "ERROR", "object": "kapot"}) is False

    @pytest.mark.asyncio
    async def test_an_event_without_a_pod_object_is_ignored(self):
        """Een event zonder pod-object slaan we over, maar het is geen streameinde: alleen
        het ERROR-event beeindigt de ronde."""
        watcher = OomPodWatcher(cluster="local")
        with patch(f"{WATCHER}.handle_observed_oom_kill", new=AsyncMock()) as handle:
            assert await watcher._handle_event({"type": "MODIFIED", "object": "kapot"}) is True
            assert await watcher._handle_event({"type": "DELETED"}) is True

        handle.assert_not_awaited()
        assert watcher._restart_counts == {}


class TestPodWatcherReconnect:
    @pytest.mark.asyncio
    async def test_a_stream_that_delivers_nothing_backs_off(self):
        """Een stream die meteen sterft (RBAC weg, API-server plat) mag niet hameren."""
        watcher = OomPodWatcher(cluster="local")
        delays: list[float] = []

        async def _sleep(delay):
            delays.append(delay)
            if len(delays) >= 4:
                watcher._running = False

        watcher._running = True
        with (
            patch.object(watcher, "_watch_once", new=AsyncMock(return_value=0)),
            patch(f"{WATCHER}.asyncio.sleep", new=_sleep),
        ):
            await watcher._run()

        assert delays == [5, 10, 20, 40]

    @pytest.mark.asyncio
    async def test_a_stream_that_ran_resets_the_backoff(self):
        watcher = OomPodWatcher(cluster="local")
        delays: list[float] = []
        results = [0, 0, 7, 0]

        async def _sleep(delay):
            delays.append(delay)
            if len(delays) >= len(results):
                watcher._running = False

        watcher._running = True
        with (
            patch.object(watcher, "_watch_once", new=AsyncMock(side_effect=results)),
            patch(f"{WATCHER}.asyncio.sleep", new=_sleep),
        ):
            await watcher._run()

        assert delays == [5, 10, 5, 10]

    @pytest.mark.asyncio
    async def test_the_backoff_stops_climbing_at_the_ceiling(self):
        """Zonder plafond blijft het verdubbelen: na een nacht met een weggenomen
        RBAC-recht zou de eerstvolgende poging dagen weg liggen en komt de watch ook
        niet terug als het recht er weer is."""
        watcher = OomPodWatcher(cluster="local")
        delays: list[float] = []

        async def _sleep(delay):
            delays.append(delay)
            if len(delays) >= 7:
                watcher._running = False

        watcher._running = True
        with (
            patch.object(watcher, "_watch_once", new=AsyncMock(return_value=0)),
            patch(f"{WATCHER}.asyncio.sleep", new=_sleep),
        ):
            await watcher._run()

        assert delays == [5, 10, 20, 40, 80, 120, 120]
        assert max(delays) == POD_WATCH_MAX_RECONNECT_SECONDS

    @pytest.mark.asyncio
    async def test_the_cache_survives_the_reconnect(self):
        """De relist speelt elke pod opnieuw af. Was de cache leeg, dan zou een kill
        die tijdens de onderbreking viel als eerste waarneming worden weggezaaid."""
        watcher = OomPodWatcher(cluster="local")
        await watcher._handle_event({"type": "ADDED", "object": _pr1644_pod(restart_count=9)})

        watcher._running = True
        call_count = 0

        async def _sleep(_delay):
            nonlocal call_count
            call_count += 1
            watcher._running = False

        with (
            patch.object(watcher, "_watch_once", new=AsyncMock(return_value=0)),
            patch(f"{WATCHER}.asyncio.sleep", new=_sleep),
        ):
            await watcher._run()

        assert watcher._restart_counts["rig-regel-k4c/pr1644-api-7c9f4d8b6d-x2klm/app"] == 9

        with patch(f"{WATCHER}.handle_observed_oom_kill", new=AsyncMock(return_value=True)) as handle:
            await watcher._handle_event({"type": "ADDED", "object": _pr1644_pod(restart_count=11)})
        handle.assert_awaited_once()


# ---------------------------------------------------------------------------
# Het vangnet op de metric
# ---------------------------------------------------------------------------


class TestReadPodOomKill:
    @pytest.mark.asyncio
    async def test_an_oomkilled_container_becomes_a_kill(self):
        kubectl = MagicMock()
        kubectl.run_command = AsyncMock(return_value=(json.dumps(_pr1644_pod(restart_count=10)), "", 0))
        with patch(f"{WATCHER}.KubectlConnector", return_value=kubectl) as connector:
            connector.isConnected = True
            kill = await read_pod_oom_kill("rig-regel-k4c", "pr1644-api-7c9f4d8b6d-x2klm", "app")

        assert kill is not None
        assert kill.finished_at == "2026-09-29T09:14:52Z"
        assert kill.project_name == "regel-k4c"

    @pytest.mark.asyncio
    async def test_a_metric_hit_the_pod_does_not_confirm_is_dropped(self):
        """De metric is EXPERIMENTAL en meldt exitcode-137 kills ook als Error.

        Daarom beslist hij niets: de pod-status is de bron.
        """
        kubectl = MagicMock()
        pod = _pr1644_pod(restart_count=10, reason="Error")
        kubectl.run_command = AsyncMock(return_value=(json.dumps(pod), "", 0))
        with patch(f"{WATCHER}.KubectlConnector", return_value=kubectl) as connector:
            connector.isConnected = True
            assert await read_pod_oom_kill("rig-regel-k4c", "pr1644-api-7c9f4d8b6d-x2klm", "app") is None

    @pytest.mark.asyncio
    async def test_a_service_owned_pod_is_dropped(self):
        """Het vangnet leest een pod op NAAM, dus de selector filtert hier niets.

        De metric kent alleen namespace/pod/container. Zonder de grendel in
        ``build_oom_kill`` levert een OOM van de waker (vaste 64Mi) een kill op die naar
        het echte component resolvet en zijn limit in het projectbestand verhoogt.
        """
        kubectl = MagicMock()
        kubectl.run_command = AsyncMock(return_value=(json.dumps(_waker_pod(restart_count=3)), "", 0))
        with patch(f"{WATCHER}.KubectlConnector", return_value=kubectl) as connector:
            connector.isConnected = True
            kill = await read_pod_oom_kill("rig-regel-k4c", "pr1644-api-waker-59c7d8f4b7-qq2mn", "app")

        assert kill is None

    @pytest.mark.asyncio
    async def test_a_service_role_with_an_empty_value_is_still_a_service_pod(self):
        """``!zad-role`` sluit uit op AANWEZIGHEID van het label, niet op zijn waarde.

        Dat is precies waarom de selector geen rollen hoeft te kennen. Kijkt de spiegel
        voor deze weg naar de waarde, dan laat een label met een lege waarde de waker
        alsnog door, en dan verhoogt zijn 64Mi-OOM de limit van het echte component.
        """
        pod = _waker_pod(restart_count=3)
        pod["metadata"]["labels"][SERVICE_ROLE_LABEL_KEY] = ""
        kubectl = MagicMock()
        kubectl.run_command = AsyncMock(return_value=(json.dumps(pod), "", 0))
        with patch(f"{WATCHER}.KubectlConnector", return_value=kubectl) as connector:
            connector.isConnected = True
            kill = await read_pod_oom_kill("rig-regel-k4c", "pr1644-api-waker-59c7d8f4b7-qq2mn", "app")

        assert kill is None

    @pytest.mark.asyncio
    async def test_a_pod_that_is_not_an_application_pod_is_dropped(self):
        """De andere helft van de selector, op de weg die hem niet vanzelf krijgt."""
        pod = _pr1644_pod(restart_count=3, labels={"app": "pr1644-api", "deployment": "pr1644"})
        kubectl = MagicMock()
        kubectl.run_command = AsyncMock(return_value=(json.dumps(pod), "", 0))
        with patch(f"{WATCHER}.KubectlConnector", return_value=kubectl) as connector:
            connector.isConnected = True
            assert await read_pod_oom_kill("rig-regel-k4c", "pr1644-api-7c9f4d8b6d-x2klm", "app") is None

    @pytest.mark.asyncio
    async def test_a_pod_that_is_gone_is_dropped(self):
        kubectl = MagicMock()
        kubectl.run_command = AsyncMock(return_value=("", "NotFound", 1))
        with patch(f"{WATCHER}.KubectlConnector", return_value=kubectl) as connector:
            connector.isConnected = True
            assert await read_pod_oom_kill("rig-regel-k4c", "weg", "app") is None

    @pytest.mark.asyncio
    async def test_only_the_container_the_metric_names_is_read(self):
        """De metric noemt pod EN container. Leest dit de eerste container in plaats van
        de genoemde, dan komt ``finishedAt`` van de verkeerde kill en dedupet de hit niet
        tegen het watch-event van dezelfde kill.
        """
        pod = _pr1644_pod(restart_count=2, reason="Error", exit_code=1)
        pod["status"]["containerStatuses"].append(
            {
                "name": "auth-wall",
                "restartCount": 3,
                "state": {"running": {}},
                "lastState": {
                    "terminated": {
                        "reason": "OOMKilled",
                        "exitCode": 137,
                        "startedAt": "2026-09-29T09:19:40Z",
                        "finishedAt": "2026-09-29T09:20:11Z",
                    }
                },
            }
        )
        kubectl = MagicMock()
        kubectl.run_command = AsyncMock(return_value=(json.dumps(pod), "", 0))
        with patch(f"{WATCHER}.KubectlConnector", return_value=kubectl) as connector:
            connector.isConnected = True
            kill = await read_pod_oom_kill("rig-regel-k4c", "pr1644-api-7c9f4d8b6d-x2klm", "auth-wall")

        assert kill is not None
        assert kill.container_name == "auth-wall"
        assert kill.finished_at == "2026-09-29T09:20:11Z"

    @pytest.mark.asyncio
    async def test_a_container_the_pod_does_not_have_is_dropped(self):
        kubectl = MagicMock()
        kubectl.run_command = AsyncMock(return_value=(json.dumps(_pr1644_pod(restart_count=10)), "", 0))
        with patch(f"{WATCHER}.KubectlConnector", return_value=kubectl) as connector:
            connector.isConnected = True
            assert await read_pod_oom_kill("rig-regel-k4c", "pr1644-api-7c9f4d8b6d-x2klm", "weg") is None

    @pytest.mark.asyncio
    async def test_without_a_kubectl_connection_nothing_is_read(self):
        kubectl = MagicMock()
        kubectl.run_command = AsyncMock()
        with patch(f"{WATCHER}.KubectlConnector", return_value=kubectl) as connector:
            connector.isConnected = False
            assert await read_pod_oom_kill("rig-regel-k4c", "pr1644-api-7c9f4d8b6d-x2klm", "app") is None
        kubectl.run_command.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_output_that_is_not_json_is_dropped(self):
        kubectl = MagicMock()
        kubectl.run_command = AsyncMock(return_value=("<html>een proxy-fout</html>", "", 0))
        with patch(f"{WATCHER}.KubectlConnector", return_value=kubectl) as connector:
            connector.isConnected = True
            assert await read_pod_oom_kill("rig-regel-k4c", "pr1644-api-7c9f4d8b6d-x2klm", "app") is None


class TestOomMetricSweep:
    @pytest.mark.asyncio
    async def test_a_hit_is_verified_and_then_tuned(self):
        metrics = MagicMock()
        metrics.custom_query = AsyncMock(
            return_value=[
                {
                    "metric": {
                        "namespace": "rig-regel-k4c",
                        "pod": "pr1644-api-7c9f4d8b6d-x2klm",
                        "container": "app",
                    },
                    "value": [1759132492, "1"],
                }
            ]
        )
        with (
            patch(f"{WATCHER}.get_metrics_connector", new=AsyncMock(return_value=metrics)),
            patch(f"{WATCHER}.read_pod_oom_kill", new=AsyncMock(return_value=_kill())),
            patch(f"{WATCHER}.handle_observed_oom_kill", new=AsyncMock(return_value=True)) as handle,
        ):
            assert await run_oom_metric_sweep("local") == 1

        assert 'reason="OOMKilled"' in metrics.custom_query.await_args[0][0]
        assert 'namespace=~"rig-.*"' in metrics.custom_query.await_args[0][0]
        assert handle.await_args.kwargs["source"] == "metric sweep"

    @pytest.mark.asyncio
    async def test_a_hit_on_a_service_owned_pod_tunes_nothing(self):
        """De hele weg, zonder ``read_pod_oom_kill`` weg te mocken: de metric noemt de
        waker-pod, de pod-status bevestigt de OOM, en er mag toch niets getuned worden.
        """
        metrics = MagicMock()
        metrics.custom_query = AsyncMock(
            return_value=[
                {
                    "metric": {
                        "namespace": "rig-regel-k4c",
                        "pod": "pr1644-api-waker-59c7d8f4b7-qq2mn",
                        "container": "app",
                    },
                    "value": [1759132492, "1"],
                }
            ]
        )
        kubectl = MagicMock()
        kubectl.run_command = AsyncMock(return_value=(json.dumps(_waker_pod(restart_count=3)), "", 0))
        with (
            patch(f"{WATCHER}.get_metrics_connector", new=AsyncMock(return_value=metrics)),
            patch(f"{WATCHER}.KubectlConnector", return_value=kubectl) as connector,
            patch(f"{WATCHER}.handle_observed_oom_kill", new=AsyncMock(return_value=True)) as handle,
        ):
            connector.isConnected = True
            assert await run_oom_metric_sweep("local") == 0

        handle.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_prefix_comes_from_the_cluster(self):
        metrics = MagicMock()
        metrics.custom_query = AsyncMock(return_value=[])
        with patch(f"{WATCHER}.get_metrics_connector", new=AsyncMock(return_value=metrics)):
            await run_oom_metric_sweep("odcn-production")

        assert 'namespace=~"rig-prd-.*"' in metrics.custom_query.await_args[0][0]

    @pytest.mark.asyncio
    async def test_without_a_metrics_backend_the_net_falls_away(self):
        """Een cluster zonder Mimir logt 'disabled' en breekt de watch niet."""
        with (
            patch(
                f"{WATCHER}.get_metrics_connector",
                new=AsyncMock(side_effect=ConnectionError("no datasource")),
            ),
            patch(f"{WATCHER}.handle_observed_oom_kill", new=AsyncMock()) as handle,
        ):
            assert await run_oom_metric_sweep("local") == 0
        handle.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_hit_the_pod_no_longer_confirms_starts_no_tune(self):
        metrics = MagicMock()
        metrics.custom_query = AsyncMock(
            return_value=[{"metric": {"namespace": "rig-regel-k4c", "pod": "weg", "container": "app"}}]
        )
        with (
            patch(f"{WATCHER}.get_metrics_connector", new=AsyncMock(return_value=metrics)),
            patch(f"{WATCHER}.read_pod_oom_kill", new=AsyncMock(return_value=None)),
            patch(f"{WATCHER}.handle_observed_oom_kill", new=AsyncMock()) as handle,
        ):
            assert await run_oom_metric_sweep("local") == 0
        handle.assert_not_awaited()

    @pytest.mark.parametrize("missing", ["namespace", "pod", "container"])
    @pytest.mark.asyncio
    async def test_a_row_missing_a_label_is_skipped_without_reading_a_pod(self, missing):
        """De metric is EXPERIMENTAL; een rij zonder een van de drie labels levert anders
        een kubectl-aanroep op een lege podnaam op."""
        row = {"namespace": "rig-regel-k4c", "pod": "pr1644-api-7c9f4d8b6d-x2klm", "container": "app"}
        del row[missing]
        metrics = MagicMock()
        metrics.custom_query = AsyncMock(return_value=[{"metric": row}])
        with (
            patch(f"{WATCHER}.get_metrics_connector", new=AsyncMock(return_value=metrics)),
            patch(f"{WATCHER}.read_pod_oom_kill", new=AsyncMock()) as read,
        ):
            assert await run_oom_metric_sweep("local") == 0
        read.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_backend_that_answers_with_nothing_tunes_nothing(self):
        metrics = MagicMock()
        metrics.custom_query = AsyncMock(return_value=None)
        with (
            patch(f"{WATCHER}.get_metrics_connector", new=AsyncMock(return_value=metrics)),
            patch(f"{WATCHER}.handle_observed_oom_kill", new=AsyncMock()) as handle,
        ):
            assert await run_oom_metric_sweep("local") == 0
        handle.assert_not_awaited()


class TestPodWatcherLifecycle:
    @pytest.mark.asyncio
    async def test_start_and_stop_leave_no_task_behind(self):
        watcher = OomPodWatcher(cluster="local")
        with patch.object(watcher, "_watch_once", new=AsyncMock(return_value=0)):
            await watcher.start()
            await asyncio.sleep(0)
            await watcher.stop()

        assert watcher._task is None
        assert watcher._running is False


# ---------------------------------------------------------------------------
# Een ronde van de stream: het subprocess, stderr, en wat er stukloopt
# ---------------------------------------------------------------------------


class _StdoutStream:
    """stdout van een neppe kubectl.

    Is er een ``stderr_drained``-gebeurtenis meegegeven, dan eindigt de stream pas als
    stderr gelezen is: zo wacht de toets op de gebeurtenis en niet op de klok. De timeout
    daaronder is een veiligheidsnet, zodat een lus die stderr NIET leest rood geeft in
    plaats van te blijven hangen.
    """

    def __init__(self, chunks: list, stderr_drained: asyncio.Event | None = None) -> None:
        self._chunks = list(chunks)
        self._drained = stderr_drained

    async def read(self, _n: int) -> bytes:
        if self._chunks:
            chunk = self._chunks.pop(0)
            if isinstance(chunk, Exception):
                raise chunk
            return chunk
        if self._drained is not None:
            drained, self._drained = self._drained, None
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(drained.wait(), timeout=2)
        return b""


class _LineStream:
    """stderr van een neppe kubectl, die meldt zodra hij tot het einde gelezen is."""

    def __init__(self, lines: list[bytes], read: asyncio.Event | None = None) -> None:
        self._lines = list(lines)
        self._read = read

    async def readline(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        if self._read is not None:
            self._read.set()
        return b""


class _FakeWatchProcess:
    """Het streamende subprocess, met de twee dingen waar de lus op wordt aangesproken."""

    def __init__(self, stdout, stderr=None) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.pid = 4242
        self.returncode: int | None = None
        self.waited = False

    def terminate(self) -> None:
        self.returncode = -15

    async def wait(self) -> int | None:
        self.waited = True
        return self.returncode


def _watch_blob(*pods: tuple[str, int]) -> bytes:
    """De stream zoals kubectl hem schrijft: een compact event per regel."""
    return "".join(json.dumps({"type": t, "object": _pr1644_pod(restart_count=n)}) + "\n" for t, n in pods).encode()


def _kubectl_watching(process) -> MagicMock:
    kubectl = MagicMock()
    kubectl.watch_pods = AsyncMock(return_value=process)
    return kubectl


class TestWatchOnce:
    @pytest.mark.asyncio
    async def test_the_events_of_one_stream_reach_the_handler(self):
        process = _FakeWatchProcess(_StdoutStream([_watch_blob(("ADDED", 9), ("MODIFIED", 10))]), _LineStream([]))
        kubectl = _kubectl_watching(process)
        watcher = OomPodWatcher(cluster="local")

        with (
            patch(f"{WATCHER}.KubectlConnector", return_value=kubectl),
            patch(f"{WATCHER}.handle_observed_oom_kill", new=AsyncMock(return_value=True)) as handle,
        ):
            assert await watcher._watch_once() == 2

        handle.assert_awaited_once()
        assert kubectl.watch_pods.await_args[0][0] == all_application_pods_selector()

    @pytest.mark.asyncio
    async def test_the_kubectl_process_is_closed_when_the_stream_ends(self):
        """Blijft het staan, dan laat elke herverbinding er een achter."""
        process = _FakeWatchProcess(_StdoutStream([_watch_blob(("ADDED", 9))]), _LineStream([]))
        watcher = OomPodWatcher(cluster="local")

        with patch(f"{WATCHER}.KubectlConnector", return_value=_kubectl_watching(process)):
            await watcher._watch_once()

        assert process.returncode == -15
        assert process.waited is True

    @pytest.mark.asyncio
    async def test_kubectl_stderr_is_drained_while_the_stream_runs(self, caplog):
        """Een niet-gelezen stderr-pipe loopt vol en blokkeert dan precies het proces dat
        de pods moet streamen. De stdout-kant hier eindigt pas als stderr gelezen is."""
        drained = asyncio.Event()
        process = _FakeWatchProcess(
            _StdoutStream([], stderr_drained=drained),
            _LineStream([b'E0929 pods is forbidden: User "system:serviceaccount:x" cannot watch\n'], read=drained),
        )
        watcher = OomPodWatcher(cluster="local")

        with (
            patch(f"{WATCHER}.KubectlConnector", return_value=_kubectl_watching(process)),
            caplog.at_level(logging.WARNING, logger="opi.services.oom_watcher"),
        ):
            assert await watcher._watch_once() == 0

        assert "pods is forbidden" in caplog.text

    @pytest.mark.asyncio
    async def test_a_stream_that_breaks_ends_the_round_instead_of_the_watcher(self):
        """De lus eromheen verbindt opnieuw. Kwam de uitzondering hier omhoog, dan sloopte
        hij de taak en lag de detectie stil tot de volgende OPI-herstart."""
        process = _FakeWatchProcess(
            _StdoutStream([_watch_blob(("ADDED", 9)), ConnectionResetError("stream weg")]),
            _LineStream([]),
        )
        watcher = OomPodWatcher(cluster="local")

        with patch(f"{WATCHER}.KubectlConnector", return_value=_kubectl_watching(process)):
            assert await watcher._watch_once() == 1

        assert process.returncode == -15

    @pytest.mark.asyncio
    async def test_a_cancelled_round_still_closes_the_kubectl_process(self):
        """``stop()`` cancelt de taak midden in de stream; het subprocess moet mee."""
        reading = asyncio.Event()
        forever = asyncio.Event()

        class _BlockingStream:
            async def read(self, _n: int) -> bytes:
                reading.set()
                await forever.wait()
                return b""

        process = _FakeWatchProcess(_BlockingStream(), _LineStream([]))
        watcher = OomPodWatcher(cluster="local")

        with patch(f"{WATCHER}.KubectlConnector", return_value=_kubectl_watching(process)):
            task = asyncio.create_task(watcher._watch_once())
            await reading.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert process.returncode == -15

    @pytest.mark.asyncio
    async def test_a_watch_that_could_not_be_started_delivers_nothing(self):
        watcher = OomPodWatcher(cluster="local")
        with patch(f"{WATCHER}.KubectlConnector", return_value=_kubectl_watching(None)):
            assert await watcher._watch_once() == 0

    @pytest.mark.asyncio
    async def test_a_process_without_stdout_delivers_nothing(self):
        watcher = OomPodWatcher(cluster="local")
        process = _FakeWatchProcess(None, _LineStream([]))
        with patch(f"{WATCHER}.KubectlConnector", return_value=_kubectl_watching(process)):
            assert await watcher._watch_once() == 0

    @pytest.mark.asyncio
    async def test_a_stream_that_only_errored_did_not_run(self):
        """Het aantal events is wat de backoff terugzet, en een ERROR-event is geen event.

        Telt het mee, dan is een stream die meteen mislukt "een stream die liep" en komt
        de watch elke vijf seconden terug, precies de lus die het plafond moet voorkomen.
        """
        process = _FakeWatchProcess(
            _StdoutStream([(json.dumps(_WATCH_ERROR_EVENT) + "\n").encode()]),
            _LineStream([]),
        )
        watcher = OomPodWatcher(cluster="local")

        with patch(f"{WATCHER}.KubectlConnector", return_value=_kubectl_watching(process)):
            assert await watcher._watch_once() == 0

    @pytest.mark.asyncio
    async def test_the_stream_stops_at_the_error_event(self):
        """Na een 410 is deze stream dood, dus wat kubectl erna nog schrijft is niet meer
        van de API-server te vertrouwen en hoort niet meer afgehandeld te worden. Bleef de
        ronde doorlopen, dan zou de backoff er nooit aan te pas komen en lag de detectie
        stil tot de volgende OPI-herstart.
        """
        blob = (
            json.dumps({"type": "ADDED", "object": _pr1644_pod(restart_count=9)})
            + "\n"
            + json.dumps(_WATCH_ERROR_EVENT)
            + "\n"
            + json.dumps({"type": "MODIFIED", "object": _pr1644_pod(restart_count=10)})
            + "\n"
        ).encode()
        process = _FakeWatchProcess(_StdoutStream([blob]), _LineStream([]))
        watcher = OomPodWatcher(cluster="local")

        with (
            patch(f"{WATCHER}.KubectlConnector", return_value=_kubectl_watching(process)),
            patch(f"{WATCHER}.handle_observed_oom_kill", new=AsyncMock(return_value=True)) as handle,
        ):
            assert await watcher._watch_once() == 1, "alleen het event VOOR de fout is afgehandeld"

        handle.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_deleted_pod_does_not_end_the_stream(self):
        """Alleen het ERROR-event beeindigt de ronde. Een DELETED komt bij elke rollout en
        elke scale-down langs; zou die de ronde ook beeindigen, dan deed de watch bij elke
        verdwijnende pod een volle cluster-brede LIST met backoff erachter, en de kill van
        de pod erna zag hij pas na de relist.
        """
        blob = (
            json.dumps({"type": "ADDED", "object": _pr1644_pod(restart_count=9)})
            + "\n"
            + json.dumps(
                {"type": "DELETED", "object": _pr1644_pod(restart_count=3, pod_name="pr1644-api-6b8c5f9a44-q7ntz")}
            )
            + "\n"
            + json.dumps({"type": "MODIFIED", "object": _pr1644_pod(restart_count=10)})
            + "\n"
        ).encode()
        process = _FakeWatchProcess(_StdoutStream([blob]), _LineStream([]))
        watcher = OomPodWatcher(cluster="local")

        with (
            patch(f"{WATCHER}.KubectlConnector", return_value=_kubectl_watching(process)),
            patch(f"{WATCHER}.handle_observed_oom_kill", new=AsyncMock(return_value=True)) as handle,
        ):
            assert await watcher._watch_once() == 3

        handle.assert_awaited_once()


class TestOomMetricSweeper:
    @pytest.mark.asyncio
    async def test_each_pass_waits_out_its_interval_first(self):
        """Zou de sweep voor de wacht komen, dan deed elke OPI-start er een, bovenop de
        volle lijst die de watch op datzelfde moment ophaalt."""
        sweeper = OomMetricSweeper(cluster="local", interval_seconds=3600)
        order: list[tuple[str, object]] = []

        async def _sleep(delay):
            order.append(("sleep", delay))
            if len(order) >= 3:
                sweeper._running = False

        async def _sweep(cluster):
            order.append(("sweep", cluster))
            return 0

        sweeper._running = True
        with (
            patch(f"{WATCHER}.asyncio.sleep", new=_sleep),
            patch(f"{WATCHER}.run_oom_metric_sweep", new=_sweep),
        ):
            await sweeper._run()

        assert order == [("sleep", 3600), ("sweep", "local"), ("sleep", 3600), ("sweep", "local")]

    @pytest.mark.asyncio
    async def test_a_failing_sweep_does_not_end_the_loop(self):
        """Het vangnet mag stil wegvallen, maar niet de lus meenemen: dan is er na een
        kapotte query geen sweep meer tot OPI herstart."""
        sweeper = OomMetricSweeper(cluster="local", interval_seconds=60)
        sweeps: list[str] = []

        async def _sweep(cluster):
            sweeps.append(cluster)
            if len(sweeps) >= 2:
                sweeper._running = False
            raise RuntimeError("geen metrics-backend")

        async def _sleep(_delay):
            return None

        sweeper._running = True
        with (
            patch(f"{WATCHER}.asyncio.sleep", new=_sleep),
            patch(f"{WATCHER}.run_oom_metric_sweep", new=_sweep),
        ):
            await sweeper._run()

        assert sweeps == ["local", "local"]

    @pytest.mark.asyncio
    async def test_start_and_stop_leave_no_task_behind(self):
        sweeper = OomMetricSweeper(cluster="local", interval_seconds=3600)
        with patch(f"{WATCHER}.run_oom_metric_sweep", new=AsyncMock(return_value=0)):
            await sweeper.start()
            await asyncio.sleep(0)
            await sweeper.stop()

        assert sweeper._task is None
        assert sweeper._running is False


# ---------------------------------------------------------------------------
# Het commando dat de stream opent
# ---------------------------------------------------------------------------


class TestWatchPodsCommand:
    """De connector is een singleton die bij het bouwen zelf ``isConnected`` zet, dus de
    vlag wordt gepatcht NA het bouwen, anders overschrijft de init de patch."""

    @pytest.mark.asyncio
    async def test_the_watch_lists_first_and_labels_each_event(self):
        """``--watch`` alleen streamt geen beginstand en ``-o json`` alleen zegt niet WAT
        er met de pod gebeurde. Zonder de eerste is er niets om de cache mee te zaaien,
        zonder de tweede is een verwijderde pod niet van een gewijzigde te onderscheiden.
        """
        from opi.connectors.kubectl import KubectlConnector

        connector = KubectlConnector()
        process = MagicMock()
        process.pid = 4242
        with (
            patch.object(KubectlConnector, "isConnected", True),
            patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)) as spawn,
        ):
            assert await connector.watch_pods("component=application,!zad-role") is process

        cmd = list(spawn.await_args[0])
        assert cmd[:3] == ["kubectl", "get", "pods"]
        assert "--all-namespaces" in cmd
        assert "--watch" in cmd
        assert "--output-watch-events" in cmd
        assert cmd[cmd.index("-l") + 1] == "component=application,!zad-role"

    @pytest.mark.asyncio
    async def test_without_a_kubectl_connection_no_process_is_started(self):
        from opi.connectors.kubectl import KubectlConnector

        connector = KubectlConnector()
        with (
            patch.object(KubectlConnector, "isConnected", False),
            patch("asyncio.create_subprocess_exec", new=AsyncMock()) as spawn,
        ):
            assert await connector.watch_pods("component=application") is None
        spawn.assert_not_awaited()


# ---------------------------------------------------------------------------
# De vlag waarachter dit alles staat
# ---------------------------------------------------------------------------


class TestClusterFlag:
    def test_the_watch_is_off_on_every_cluster_for_now(self):
        """Een lange stream over alle pods die zelf remedieert gaat per cluster aan,
        niet door gemerged te worden."""
        from opi.core.cluster_config import CLUSTER_CONFIG, watches_pods_for_oom

        assert [c for c in CLUSTER_CONFIG if watches_pods_for_oom(c)] == []

    def test_a_cluster_that_turns_it_on_gets_it(self):
        from opi.core.cluster_config import CLUSTER_CONFIG, watches_pods_for_oom

        with patch.dict(CLUSTER_CONFIG["local"], {"oom_pod_watch": True}):
            assert watches_pods_for_oom("local") is True

    def test_the_server_starts_the_watch_behind_that_flag(self):
        """Driftgrendel: zonder de vlag ervoor zou de watch op elk cluster aangaan zodra
        deze branch gemerged is, en precies dat is wat "standaard uit" moet voorkomen."""
        import inspect

        from opi import server

        source = inspect.getsource(server)
        guard = "if watches_pods_for_oom(settings.CLUSTER_MANAGER):"
        assert guard in source
        block = source[source.index(guard) :][:1200]
        assert "OomPodWatcher(" in block
        assert "OomMetricSweeper(" in block
        assert "settings.OOM_METRIC_SWEEP_INTERVAL_SECONDS" in block, "het interval komt uit de instelling"

    def test_the_server_stops_both_on_shutdown(self):
        """Driftgrendel: zonder de stop blijft het kubectl-subprocess van de watch draaien
        als OPI afsluit, en bij een herstart in dezelfde pod een tweede ernaast."""
        import inspect

        from opi import server

        source = inspect.getsource(server)
        assert "oom_metric_sweeper.stop()" in source
        assert "oom_pod_watcher.stop()" in source
