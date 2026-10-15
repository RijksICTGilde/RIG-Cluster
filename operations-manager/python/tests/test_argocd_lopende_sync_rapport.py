"""Wat ZAD over een sync rapporteert moet kloppen, ook terwijl die vastzit (RC-250).

Op 5 oktober 2026 om 15:30 leek het alsof de wijziging was aangekomen. `last_synced_at`
viel terug op `status.reconciledAt`, en dat is het moment van de laatste VERGELIJKING, niet
van een uitrol. Er was op dat moment niets uitgerold: de sync van 14:54 stond nog `Running`
op een revisie van voor de fix.

Drie dingen gaan hier mis zodra je ze door elkaar haalt, en de toetsen hieronder houden ze
apart:

* `status.sync.revision` is waartegen VERGELEKEN wordt, `operationState.syncResult.revision`
  is wat werkelijk is UITGEROLD. Tijdens de storing waren dat twee verschillende revisies,
  en juist dat verschil was het probleem.
* `operationState.finishedAt` bestaat niet zolang een sync loopt. Dat is geen reden om een
  ander tijdstip te melden; het is de reden om de lopende sync zelf te melden.
* ArgoCD's sync is niet ZAD's wachtrij. `pending_rollout.rollout_in_progress` gaat over
  taken die nog naar git moeten schrijven; hier staat de commit al in git en past de cluster
  hem toe. Bij deze storing stond alleen de tweede aan.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.responses import HTMLResponse
from opi.api.v2.models import DeploymentStatus
from opi.api.v2.router import _build_deployment_detail, _extract_live_status
from opi.core.templates_lotc import templates_lotc as templates
from opi.services.deployment_diagnostics import gather_sync_deviations
from opi.services.deployment_state import collect_deployment_state
from opi.services.services import ServiceAdapter
from opi.web.router import dashboard

CARD = "bg/_argocd-deployment-card.html.j2"
CLUSTER = "odcn-production"


def _status(
    *,
    phase: str | None = "Running",
    started_at: str | None = "2026-10-05T14:54:30Z",
    finished_at: str | None = None,
    operation_revision: str | None = "6a0295a671",
    compared_revision: str = "d65d2e8eaf",
    sync: str = "OutOfSync",
    health: str = "Progressing",
    attached: bool = True,
) -> dict[str, Any]:
    """De Application van `dd-mco-pr-68` zoals hij er om 15:30 uitzag."""
    state: dict[str, Any] = {"message": "waiting for healthy state of apps/Deployment/pr-68-website"}
    if phase:
        state["phase"] = phase
    if started_at:
        state["startedAt"] = started_at
    if finished_at:
        state["finishedAt"] = finished_at
    if operation_revision:
        state["syncResult"] = {"revision": operation_revision}
    payload: dict[str, Any] = {
        "spec": {"syncPolicy": {"automated": {"prune": True, "selfHeal": True}}},
        "status": {
            "sync": {"status": sync, "revision": compared_revision},
            "health": {"status": health},
            "operationState": state,
            # Een verse refresh: dit is precies het tijdstip dat de oude terugval meldde.
            "reconciledAt": "2026-10-05T15:30:44Z",
        },
    }
    if attached:
        payload["operation"] = {"sync": {"revision": operation_revision}}
    return payload


class TestLastSyncedAt:
    def test_a_running_sync_means_nothing_finished_yet(self) -> None:
        """De terugval op ``reconciledAt`` is weg. Die meldde 15:30:44 terwijl de laatste sync
        die afrondde van voor 14:54 was, en gaf zo de indruk dat de fix was uitgerold."""
        live = _extract_live_status(_status())
        assert live.last_synced_at is None

    def test_a_finished_sync_reports_when_it_finished(self) -> None:
        live = _extract_live_status(
            _status(phase="Succeeded", finished_at="2026-10-05T15:42:40Z", sync="Synced", health="Healthy")
        )
        assert live.last_synced_at == "2026-10-05T15:42:40Z"

    def test_a_deployment_without_an_application_reports_nothing(self) -> None:
        live = _extract_live_status(None)
        assert live.status is DeploymentStatus.Pending
        assert live.last_synced_at is None
        assert live.running_sync is None


class TestWhichRevisionIsReported:
    def test_the_rolled_out_revision_is_the_one_the_operation_applied(self) -> None:
        live = _extract_live_status(_status())
        assert live.rolled_out_revision == "6a0295a671"
        assert live.revision == "d65d2e8eaf"

    def test_the_running_sync_carries_the_revision_it_is_stuck_on(self) -> None:
        """Een lopende operation zit vast aan de revisie waarmee hij begon en ziet nieuwere
        commits niet. Dat die twee uiteenlopen is het hele signaal."""
        running = _extract_live_status(_status()).running_sync
        assert running is not None
        assert running.revision == "6a0295a671"


class TestRunningSync:
    def test_it_reports_the_phase_and_since_when(self) -> None:
        running = _extract_live_status(_status()).running_sync
        assert running is not None
        assert running.phase == "Running"
        assert running.started_at == "2026-10-05T14:54:30Z"
        assert running.message == "waiting for healthy state of apps/Deployment/pr-68-website"
        assert running.blocks_next_sync is True

    def test_a_detached_state_is_reported_but_blocks_nothing(self) -> None:
        """De stand die achterblijft nadat iemand ``.operation`` heeft weggehaald: niets ruimt
        die ``operationState`` op, dus hij blijft ``Running`` tot een volgende sync hem
        overschrijft. Auto-sync is dan al vrij."""
        running = _extract_live_status(_status(attached=False)).running_sync
        assert running is not None
        assert running.phase == "Running"
        assert running.blocks_next_sync is False

    def test_a_finished_sync_is_not_a_running_one(self) -> None:
        live = _extract_live_status(
            _status(phase="Succeeded", finished_at="2026-10-05T15:42:40Z", sync="Synced", health="Healthy")
        )
        assert live.running_sync is None

    def test_terminating_still_counts_as_running(self) -> None:
        """De phase die de controller-grens zet; de operation staat er dan nog."""
        running = _extract_live_status(_status(phase="Terminating")).running_sync
        assert running is not None
        assert running.phase == "Terminating"


class TestItReachesTheApiAnswer:
    """Dekking op de lezer pint het ANTWOORD niet: het veld moet ook in DeploymentDetail staan."""

    def test_the_detail_answer_carries_the_running_sync_and_both_revisions(self) -> None:
        project_data = {
            "name": "dd-mco",
            "components": [{"name": "website", "type": "single", "ports": {"inbound": [8080]}}],
            "deployments": [
                {
                    "name": "pr-68",
                    "cluster": CLUSTER,
                    "namespace": "dd-mco",
                    "components": [{"reference": "website", "image": "ghcr.io/x/y:1"}],
                }
            ],
        }
        detail = _build_deployment_detail(
            project_data["deployments"][0], "dd-mco", project_data, _extract_live_status(_status())
        )
        assert detail.last_synced_at is None
        assert detail.rolled_out_revision == "6a0295a671"
        assert detail.sync_revision == "d65d2e8eaf"
        assert detail.running_sync is not None
        assert detail.running_sync.blocks_next_sync is True
        assert detail.running_sync.started_at == "2026-10-05T14:54:30Z"


class TestDeviationReasons:
    """ "Wordt bij de volgende sync bijgewerkt" is een belofte die een vastgelopen sync niet
    inlost: zolang hij staat wordt elke volgende overgeslagen."""

    def _reason(self, status_data: dict[str, Any], *, pruning: bool) -> str:
        status_data["status"]["resources"] = [
            {
                "kind": "Deployment",
                "name": "pr-68-website",
                "status": "OutOfSync",
                **({"requiresPruning": True} if pruning else {}),
            }
        ]
        deviations = gather_sync_deviations(status_data, deployment_name="pr-68")
        assert len(deviations) == 1
        return deviations[0]["reason"]

    def test_a_drifted_resource_names_the_stuck_sync_instead_of_the_next_one(self) -> None:
        reason = self._reason(_status(), pruning=False)
        assert "loopt al" in reason
        assert "vast" in reason
        assert "volgende sync" not in reason

    def test_a_prune_candidate_names_the_sync_that_should_have_removed_it(self) -> None:
        reason = self._reason(_status(), pruning=True)
        assert "opruimen loopt al" in reason
        assert "volgende sync" not in reason

    def test_a_sync_that_is_still_just_busy_keeps_the_old_promise(self) -> None:
        """Onder de drempel is "bij de volgende sync" waar: die sync is deze, en hij loopt."""
        import datetime

        just_now = datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z")
        reason = self._reason(_status(started_at=just_now), pruning=False)
        assert reason == "wijkt af van git en wordt bij de volgende sync bijgewerkt"

    def test_without_a_running_sync_nothing_changes(self) -> None:
        reason = self._reason(
            _status(phase="Succeeded", finished_at="2026-10-05T14:54:33Z", attached=False), pruning=False
        )
        assert reason == "wijkt af van git en wordt bij de volgende sync bijgewerkt"

    def test_a_stuck_delete_keeps_its_own_reason(self) -> None:
        """Een resource die de LAATSTE GESLAAGDE sync al prunede en er nog staat heeft een
        andere oorzaak, en die reden gaat voor: daar lost geen enkele sync iets op."""
        status_data = _status(phase="Succeeded", finished_at="2026-10-05T14:54:33Z", attached=False)
        status_data["status"]["operationState"]["syncResult"]["resources"] = [
            {"kind": "Deployment", "name": "pr-68-website", "status": "Pruned"}
        ]
        assert self._reason(status_data, pruning=True) == (
            "is verwijderd, maar het cluster maakt de verwijdering niet af"
        )


def _render_card(status: dict[str, Any]) -> str:
    project_data = {
        "name": "dd-mco",
        "components": [{"name": "website"}],
        "deployments": [
            {"name": "pr-68", "cluster": CLUSTER, "components": [{"reference": "website", "image": "ghcr.io/x/y:1"}]}
        ],
    }
    deployment = project_data["deployments"][0]
    return templates.env.get_template(CARD).render(
        deployment=deployment,
        project={"name": project_data["name"]},
        argocd_status={deployment["name"]: status},
        current_cluster=CLUSTER,
        deployment_states={deployment["name"]: collect_deployment_state(project_data, deployment["name"])},
        ServiceAdapter=ServiceAdapter,
    )


class TestTheCard:
    def _card_status(self, **overrides: Any) -> dict[str, Any]:
        status = {
            "available": True,
            "health": "Progressing",
            "sync": "OutOfSync",
            "operation_phase": "Running",
            "operation_message": "waiting for healthy state of apps/Deployment/pr-68-website",
            "operation_duration": "19 minuten",
            "operation_is_stuck": True,
            "errors": [],
            "deviations": [],
        }
        status.update(overrides)
        return status

    def test_it_says_how_long_the_sync_has_been_running(self) -> None:
        html = _render_card(self._card_status())
        assert "loopt al 19 minuten" in html

    def test_it_says_this_is_argocds_sync_and_not_zads_queue(self) -> None:
        """Beide stonden met het woord "Operatie" op het scherm, en het zijn twee
        verschillende dingen die onafhankelijk aan kunnen staan."""
        html = _render_card(self._card_status())
        assert "Sync van ArgoCD" in html
        assert "Operatie:" not in html

    def test_a_blocking_sync_says_a_new_version_will_not_land(self) -> None:
        html = _render_card(self._card_status())
        assert "slaat ArgoCD elke volgende over" in html
        assert "niet weg te gooien" in html or "niet weg hoeft te gooien" in html or "weggooien" in html

    def test_a_sync_that_is_still_busy_gets_no_blockade_notice(self) -> None:
        html = _render_card(self._card_status(operation_is_stuck=False, operation_duration="40 seconden"))
        assert "loopt al 40 seconden" in html
        assert "slaat ArgoCD elke volgende over" not in html

    def test_a_detached_state_gets_no_blockade_notice(self) -> None:
        """Rapporteren ja, waarschuwen nee: deze stand blokkeert niets meer.

        ``is_stuck`` is hier de enige sleutel die deze waarschuwing aanzet, en hij begint met
        ``if not blocks_next_sync: return False``, dus een losgekoppelde stand komt hier als
        False aan.
        """
        html = _render_card(self._card_status(operation_is_stuck=False))
        assert "slaat ArgoCD elke volgende over" not in html

    def test_a_blocking_sync_without_a_start_warns_without_claiming_a_duration(self) -> None:
        """``startedAt`` kan ontbreken, en dan is er geen duur. De waarschuwing moet er wel
        staan, want zonder leesbaar begin verandert er aan deze stand juist niets meer.
        "loopt al onbekend lang" op het scherm zetten is geen antwoord op "is dit lang?"."""
        html = _render_card(self._card_status(operation_duration=None))
        assert "Sync van ArgoCD: Running" in html
        assert "loopt al" not in html
        assert "slaat ArgoCD elke volgende over" in html

    def test_a_healthy_card_says_nothing_about_a_sync(self) -> None:
        html = _render_card(
            {
                "available": True,
                "health": "Healthy",
                "sync": "Synced",
                "operation_phase": "Succeeded",
                "last_sync": "2026-10-05T15:42:40Z",
                "errors": [],
                "deviations": [],
            }
        )
        assert "Sync van ArgoCD" not in html
        assert "slaat ArgoCD elke volgende over" not in html


def _dashboard_context(status_data: dict[str, Any] | None) -> dict[str, Any]:
    """Render het dashboard met één deployment en geef de context die het sjabloon krijgt.

    De lezer zit midden in de route en niet in een eigen functie, dus de route zelf is de
    enige plek waar hij te meten is.
    """
    store = MagicMock()
    store.get_all.return_value = [
        SimpleNamespace(
            name="dd-mco",
            data={"deployments": [{"name": "pr-68", "cluster": CLUSTER, "namespace": "dd-mco"}]},
            users=[],
        )
    ]
    captured: dict[str, Any] = {}

    def _capture(_request: Any, *, template: str, context: dict[str, Any]) -> HTMLResponse:
        captured.update(context)
        return HTMLResponse("ok")

    connector = MagicMock()
    connector.auth_token = "token"
    connector.get_application_status = AsyncMock(return_value=status_data)

    with (
        patch("opi.connectors.argo.create_argo_connector", MagicMock(return_value=connector)),
        patch("opi.web.router.get_current_user", MagicMock(return_value={"email": "u@example.com"})),
        patch("opi.web.router.get_project_store", MagicMock(return_value=store)),
        patch("opi.web.router.is_user_authorized_for_project", MagicMock(return_value=True)),
        patch("opi.web.router.render", MagicMock(side_effect=_capture)),
    ):
        asyncio.run(dashboard(MagicMock()))

    return captured


class TestTheDashboardTile:
    """`last_deployed` staat op het dashboard als het moment van uitrollen ("3 uur geleden",
    `dashboard/_projecten.html.j2`). Dezelfde terugval op `reconciledAt` die uit de v2-API is
    gehaald zat ook hier, en hier gaat hij naar het scherm van iedereen met een project."""

    def _projects(self, status_data: dict[str, Any] | None) -> list[dict[str, Any]]:
        return _dashboard_context(status_data)["projects"]

    def test_a_stuck_sync_does_not_report_the_comparison_as_a_rollout(self) -> None:
        """Dit is 15:30:44 uit de storing: een verse vergelijking, en een sync die sinds 14:54
        niets heeft uitgerold. Een datum tonen is hier erger dan geen datum tonen."""
        assert self._projects(_status())[0]["last_deployed"] is None

    def test_a_finished_sync_still_reports_when_it_finished(self) -> None:
        finished = _status(phase="Succeeded", finished_at="2026-10-05T15:42:40Z", sync="Synced", health="Healthy")
        assert self._projects(finished)[0]["last_deployed"] == "2026-10-05T15:42:40Z"

    def test_a_deployment_without_an_application_reports_nothing(self) -> None:
        assert self._projects(None)[0]["last_deployed"] is None
