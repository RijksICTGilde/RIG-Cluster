"""The live count of pods that cannot pull, and what the card and the alert read off it.

This is the replacement for the intervention RC-243 took away. Until then a component
that could not pull was scaled to zero, which made ArgoCD call the application Healthy;
now the pod stays in ImagePullBackOff and this is what notices.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.handlers.project_file_handler import IMAGE_PULL_ABSENT, IMAGE_PULL_CAPACITY, IMAGE_PULL_UNDIAGNOSED
from opi.services import image_pull_report
from opi.services.image_pull_report import (
    ImagePullObserver,
    image_pull_failure_count,
    image_pull_failure_counts,
    image_pull_failures_for,
    observe_image_pull_failures,
    read_image_pull_failure,
)

ABSENT = 'Back-off pulling image "ghcr.io/org/app:bad-tag": ErrImagePull: manifest unknown'
QUOTA = "ErrImagePull: reading manifest pr-692 in rcr.rijksapps.nl/ghcr-rig/org/app: denied: Quota has been exceeded on namespace"
EOF_OUTAGE = 'ErrImagePull: pinging container registry rcr.rijksapps.nl: Get "https://rcr.rijksapps.nl/v2/": EOF'


def _pod(
    *,
    name: str = "production-api-abc-1",
    namespace: str = "rig-prd-myproject",
    app: str = "production-api",
    message: str | None = ABSENT,
    reason: str = "ImagePullBackOff",
    container: str = "app",
    image: str = "ghcr.io/org/app:bad-tag",
    extra_labels: dict[str, str] | None = None,
    terminating: bool = False,
    extra_containers: list[dict] | None = None,
) -> dict:
    labels = {
        "app": app,
        "component": "application",
        "project": "myproject",
        "deployment": "production",
    }
    labels.update(extra_labels or {})
    statuses: list[dict] = []
    if message is not None:
        statuses.append(
            {"name": container, "image": image, "state": {"waiting": {"reason": reason, "message": message}}}
        )
    statuses.extend(extra_containers or [])
    pod: dict = {
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": labels,
            "creationTimestamp": "2026-10-05T08:00:00Z",
        },
        "status": {"containerStatuses": statuses},
    }
    if terminating:
        pod["metadata"]["deletionTimestamp"] = "2026-10-05T09:00:00Z"
    return pod


@pytest.fixture(autouse=True)
def _empty_snapshot():
    """The snapshot is process state; no test may inherit another's."""
    image_pull_report._snapshot = ()
    yield
    image_pull_report._snapshot = ()


class TestReadImagePullFailure:
    def test_the_registry_answer_the_image_and_the_age_all_come_along(self) -> None:
        failure = read_image_pull_failure(_pod())

        assert failure is not None
        assert failure.app == "production-api"
        assert failure.namespace == "rig-prd-myproject"
        assert failure.image == "ghcr.io/org/app:bad-tag"
        assert failure.reason_class == IMAGE_PULL_ABSENT
        # The registry's own words, unabridged: the card shows them and they are what a
        # repeat of one of the three outages is reconstructed from.
        assert ABSENT in failure.message
        assert failure.message.startswith("ImagePullBackOff: ")
        # The container never started, so the pod's own age is how long this has failed.
        # The waiting state carries no timestamp of its own.
        assert failure.since == "2026-10-05T08:00:00Z"

    def test_the_three_classes_are_told_apart(self) -> None:
        assert read_image_pull_failure(_pod(message=QUOTA)).reason_class == IMAGE_PULL_CAPACITY
        assert read_image_pull_failure(_pod(message=EOF_OUTAGE)).reason_class == IMAGE_PULL_UNDIAGNOSED
        assert read_image_pull_failure(_pod(message=ABSENT)).reason_class == IMAGE_PULL_ABSENT

    def test_a_healthy_pod_reports_nothing(self) -> None:
        running = {"name": "app", "image": "ghcr.io/org/app:v1", "state": {"running": {"startedAt": "x"}}}
        assert read_image_pull_failure(_pod(message=None, extra_containers=[running])) is None

    def test_a_crash_loop_is_not_an_image_pull_failure(self) -> None:
        assert read_image_pull_failure(_pod(reason="CrashLoopBackOff")) is None

    def test_a_service_owned_pod_is_not_the_components_own(self) -> None:
        # Sleep-mode's waker carries the component's app/deployment/project labels AND
        # component=application. Only the absence of zad-role tells them apart, and
        # counting the waker would report a component that is deliberately asleep.
        assert read_image_pull_failure(_pod(extra_labels={"zad-role": "waker"})) is None

    def test_a_terminating_pod_is_not_counted(self) -> None:
        # During a rollout the replaced pod keeps its labels and its state until the
        # handover finishes.
        assert read_image_pull_failure(_pod(terminating=True)) is None

    def test_the_main_container_wins_over_a_sidecar(self) -> None:
        # Sidecar FIRST in the list, so the answer cannot be "whichever came first". A
        # bad platform sidecar once sent a debug down the wrong path for hours because
        # its image was reported as the component's own.
        pod = _pod(message=None)
        pod["status"]["containerStatuses"] = [
            {
                "name": "oauth2-proxy",
                "image": "quay.io/oauth2-proxy:v7",
                "state": {"waiting": {"reason": "ErrImagePull", "message": "manifest unknown"}},
            },
            {
                "name": "app",
                "image": "ghcr.io/org/app:bad-tag",
                "state": {"waiting": {"reason": "ImagePullBackOff", "message": ABSENT}},
            },
        ]

        failure = read_image_pull_failure(pod)

        assert failure is not None
        assert failure.container == "app"
        assert failure.image == "ghcr.io/org/app:bad-tag"

    def test_a_sidecar_only_failure_is_named_as_the_sidecar(self) -> None:
        sidecar = {
            "name": "oauth2-proxy",
            "image": "quay.io/oauth2-proxy:v7",
            "state": {"waiting": {"reason": "ErrImagePull", "message": "manifest unknown"}},
        }
        failure = read_image_pull_failure(_pod(message=None, extra_containers=[sidecar]))

        # A pod never becomes ready while ANY container cannot pull, so it counts -- but
        # named as the sidecar, so its image is not mistaken for the component's own.
        assert failure is not None
        assert failure.container == "oauth2-proxy"
        assert failure.image == "quay.io/oauth2-proxy:v7"


class TestObserve:
    @patch("opi.services.image_pull_report.KubectlConnector")
    @pytest.mark.asyncio
    async def test_a_pass_replaces_the_snapshot(self, mock_kubectl_cls) -> None:
        mock_kubectl_cls.isConnected = True
        mock_kubectl = MagicMock()
        mock_kubectl_cls.return_value = mock_kubectl
        pods = {"items": [_pod(), _pod(name="other-1", app="production-worker", message=QUOTA)]}
        mock_kubectl.run_command = AsyncMock(return_value=(json.dumps(pods), "", 0))

        assert await observe_image_pull_failures() == 2
        assert image_pull_failure_count() == 2
        assert image_pull_failure_counts() == {
            ("rig-prd-myproject", IMAGE_PULL_ABSENT): 1,
            ("rig-prd-myproject", IMAGE_PULL_CAPACITY): 1,
        }

        # A later healthy pass takes the entries away again: this is a snapshot, not a
        # log, so a fixed image stops alerting without anyone clearing anything.
        mock_kubectl.run_command = AsyncMock(return_value=(json.dumps({"items": []}), "", 0))
        assert await observe_image_pull_failures() == 0
        assert image_pull_failure_count() == 0

    @patch("opi.services.image_pull_report.KubectlConnector")
    @pytest.mark.asyncio
    async def test_a_failed_pass_keeps_the_previous_count(self, mock_kubectl_cls) -> None:
        """ "We could not look" must never read as "nothing is wrong": zero is what the
        alert treats as healthy, so a kubectl that fails would silence it."""
        mock_kubectl_cls.isConnected = True
        mock_kubectl = MagicMock()
        mock_kubectl_cls.return_value = mock_kubectl
        mock_kubectl.run_command = AsyncMock(return_value=(json.dumps({"items": [_pod()]}), "", 0))
        await observe_image_pull_failures()

        for outcome in (("", "forbidden", 1), ("niet json", "", 0)):
            mock_kubectl.run_command = AsyncMock(return_value=outcome)
            assert await observe_image_pull_failures() == 1
            assert image_pull_failure_count() == 1

        mock_kubectl.run_command = AsyncMock(side_effect=RuntimeError("kubectl is weg"))
        assert await observe_image_pull_failures() == 1
        assert image_pull_failure_count() == 1

    @patch("opi.services.image_pull_report.KubectlConnector")
    @pytest.mark.asyncio
    async def test_the_selector_excludes_the_service_owned_pods(self, mock_kubectl_cls) -> None:
        mock_kubectl_cls.isConnected = True
        mock_kubectl = MagicMock()
        mock_kubectl_cls.return_value = mock_kubectl
        mock_kubectl.run_command = AsyncMock(return_value=(json.dumps({"items": []}), "", 0))

        await observe_image_pull_failures()

        args = mock_kubectl.run_command.call_args.args[0]
        assert "--all-namespaces" in args
        assert "component=application,!zad-role" in args


class TestWhatTheCardReads:
    def test_three_failing_replicas_are_one_entry_for_the_component(self) -> None:
        image_pull_report._snapshot = tuple(
            read_image_pull_failure(_pod(name=f"production-api-abc-{i}")) for i in range(3)
        )

        # The gauge counts pods, because a pod is what cannot pull.
        assert image_pull_failure_count() == 3
        # The card names components, because that is the unit a reader acts on.
        entries = image_pull_failures_for("rig-prd-myproject", {"production-api"})
        assert len(entries) == 1
        assert entries[0].app == "production-api"

    def test_another_namespace_and_another_component_stay_out(self) -> None:
        image_pull_report._snapshot = (
            read_image_pull_failure(_pod()),
            read_image_pull_failure(_pod(name="x", namespace="rig-prd-other", app="production-api")),
            read_image_pull_failure(_pod(name="y", app="production-worker")),
        )

        entries = image_pull_failures_for("rig-prd-myproject", {"production-api"})
        assert [e.app for e in entries] == ["production-api"]
        assert entries[0].namespace == "rig-prd-myproject"


class TestWhatTheAlertReads:
    """The alert is ``opi_image_pull_failing_pods > 0``, so the series has to EXIST at
    zero. A metric that only appears once something is broken cannot be told apart from a
    metric that is not being produced -- which is the state this replaced: a component
    that could not pull went to zero replicas and the application went green."""

    def _families(self) -> dict[str, object]:
        from opi.core.metrics import OPICollector

        return {family.name: family for family in OPICollector().collect()}

    def test_the_total_is_there_on_a_healthy_fleet(self) -> None:
        families = self._families()

        total = families["opi_image_pull_failing_pods"]
        assert [s.value for s in total.samples] == [0.0]
        # The labelled one carries nothing, which is correct: there is no namespace to
        # name, and inventing label values is a cardinality problem, not a measurement.
        assert families["opi_image_pull_failing_pods_by_reason"].samples == []

    def test_a_failing_component_shows_up_in_both(self) -> None:
        image_pull_report._snapshot = (read_image_pull_failure(_pod()),)

        families = self._families()

        assert [s.value for s in families["opi_image_pull_failing_pods"].samples] == [1.0]
        by_reason = families["opi_image_pull_failing_pods_by_reason"].samples
        assert len(by_reason) == 1
        # ``project_namespace`` and not ``namespace``: the scrape adds a namespace of its
        # own and a collision is silently renamed to ``exported_namespace``.
        assert by_reason[0].labels == {"project_namespace": "rig-prd-myproject", "reason": IMAGE_PULL_ABSENT}
        assert by_reason[0].value == 1.0
        # Never the kubelet message or the image tag: those are unbounded.
        assert set(by_reason[0].labels) == {"project_namespace", "reason"}


class TestWatDeKaartKrijgt:
    """``describe_image_pull_failures`` zet de waarneming om in wat de kaart toont."""

    def _deployment(self, *, disabled: bool = False) -> dict:
        return {
            "name": "production",
            "cluster": "odcn-production",
            "namespace": "myproject",
            "components": [
                {
                    "reference": "api",
                    "image": "ghcr.io/org/app:bad-tag",
                    **({"disabled": True, "disabled-reason": "OOMKilled"} if disabled else {}),
                }
            ],
        }

    def test_de_waarneming_wordt_een_componentverwijzing(self) -> None:
        from opi.services.deployment_diagnostics import describe_image_pull_failures

        image_pull_report._snapshot = (read_image_pull_failure(_pod()),)

        gevonden = describe_image_pull_failures(self._deployment())

        assert len(gevonden) == 1
        # De pod draagt de UNIEKE naam (production-api); de kaart toont de verwijzing
        # zoals die in het projectbestand staat.
        assert gevonden[0].reference == "api"
        assert gevonden[0].reason_class == IMAGE_PULL_ABSENT
        assert gevonden[0].since == "2026-10-05T08:00:00Z"

    def test_een_uitgeschakeld_component_blijft_erbuiten(self) -> None:
        """Nul replicas is daar de bedoelde eindstand, en de kaart noemt hem al apart.
        Twee meldingen over hetzelfde component die elkaar tegenspreken is erger dan een."""
        from opi.services.deployment_diagnostics import describe_image_pull_failures

        image_pull_report._snapshot = (read_image_pull_failure(_pod()),)

        assert describe_image_pull_failures(self._deployment(disabled=True)) == []


class TestDeObserver:
    @patch("opi.services.image_pull_report.observe_image_pull_failures", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_hij_telt_meteen_en_niet_pas_na_het_eerste_interval(self, mock_observe) -> None:
        """Een gauge die na een herstart een interval lang nul leest, zegt "gezond" over
        een vloot waar hij nog niet naar gekeken heeft."""
        observer = ImagePullObserver(interval_seconds=3600)
        await observer.start()
        try:
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert mock_observe.await_count == 1
        finally:
            await observer.stop()

    @patch("opi.services.image_pull_report.observe_image_pull_failures", new_callable=AsyncMock)
    @pytest.mark.asyncio
    async def test_een_kapotte_ronde_stopt_de_lus_niet(self, mock_observe) -> None:
        # Anders is een enkele hik het einde van de telling, stil, tot de volgende
        # herstart van OPI.
        mock_observe.side_effect = [RuntimeError("stuk")] + [None] * 50
        observer = ImagePullObserver(interval_seconds=0)
        await observer.start()
        try:
            for _ in range(10):
                await asyncio.sleep(0)
            assert mock_observe.await_count > 1
        finally:
            await observer.stop()

    @pytest.mark.asyncio
    async def test_stoppen_laat_geen_taak_achter(self) -> None:
        observer = ImagePullObserver(interval_seconds=3600)
        await observer.start()
        task = observer._task
        await observer.stop()

        assert task is not None and task.done()
        assert observer._task is None
