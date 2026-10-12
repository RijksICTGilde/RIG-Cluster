"""De lopende sync-operation van een ArgoCD-Application: lezen, beoordelen, beëindigen.

`.operation` is wat de controller BLOKKEERT, want `autoSync` begint met
`if app.Operation != nil`. `.status.operationState` is wat de LAATSTE operation deed. Ze
lopen uiteen zodra iemand `.operation` weghaalt terwijl de phase nog `Running` is: niets
ruimt die losgekoppelde stand op, dus hij blijft `Running` tot een volgende sync hem
overschrijft, terwijl auto-sync al vrij is.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from opi.connectors.kubectl import KubectlConnectionError, KubectlExecutionError

if TYPE_CHECKING:
    from opi.connectors.kubectl import KubectlConnector

logger = logging.getLogger(__name__)

#: `Terminating` hoort erbij: de controller-grens zet die phase en de operation staat er dan nog.
ONGOING_PHASES = frozenset({"Running", "Terminating"})

#: Gelijk aan OPI's eigen wacht van 300s: wat langer loopt dan waar OPI zelf op wacht, wacht
#: op iets wat niet meer komt. Lager zou een uitrol kunnen afbreken die nog gewoon bezig is.
#:
#: Dat geldt voor wie naar een uitrol van IEMAND ANDERS kijkt: de ronde voor een gewone push
#: en het rapport. Waar "nog gewoon bezig" geen mogelijkheid meer is wordt 0 meegegeven: op
#: OPI's eigen time-out-tak (``ProjectManager._terminate_operation_we_waited_on``) en op het
#: refresh-pad, waar iemand op reset drukt (``reset_running_sync``).
STUCK_OPERATION_THRESHOLD_SECONDS = 300


@dataclass(frozen=True)
class SyncOperation:
    """Wat een Application over zijn laatste of lopende sync zegt."""

    phase: str | None
    started_at: str | None
    finished_at: str | None
    #: `syncResult.revision`: de revisie die werkelijk is UITGEROLD. Niet te verwarren met
    #: `status.sync.revision`, de revisie waartegen vergeleken is.
    revision: str | None
    message: str | None
    #: Of `.operation` nog aan de Application hangt.
    attached: bool

    @property
    def ongoing(self) -> bool:
        return self.phase in ONGOING_PHASES

    @property
    def blocks_next_sync(self) -> bool:
        return self.attached and self.ongoing


def read_sync_operation(application: dict[str, Any] | None) -> SyncOperation:
    """Lees de operation uit een Application-payload.

    Dezelfde lezer voor `kubectl get application -o json` en voor
    `GET /api/v1/applications/{naam}`: de swagger van argocd-server geeft
    `v1alpha1Application` dezelfde velden als de CR, dus `.operation` staat in beide payloads
    op dezelfde plek.
    """
    application = application or {}
    state = (application.get("status") or {}).get("operationState") or {}
    sync_result = state.get("syncResult") or {}
    return SyncOperation(
        phase=state.get("phase") or None,
        started_at=state.get("startedAt") or None,
        finished_at=state.get("finishedAt") or None,
        revision=sync_result.get("revision") or None,
        message=state.get("message") or None,
        attached=application.get("operation") is not None,
    )


def operation_age_seconds(operation: SyncOperation, *, now: datetime | None = None) -> int | None:
    """Hoe lang de operation al loopt, of ``None`` als `startedAt` ontbreekt of onleesbaar is."""
    if not operation.started_at:
        return None
    try:
        started = datetime.fromisoformat(operation.started_at)
    except ValueError:
        logger.warning("Onleesbare startedAt op een sync-operation: %r", operation.started_at)
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    return int(((now or datetime.now(UTC)) - started).total_seconds())


def is_stuck(operation: SyncOperation, *, threshold_seconds: int, now: datetime | None = None) -> bool:
    """Blokkeert deze operation een volgende sync en loopt hij al zo lang als de drempel?

    Een ontbrekende of onleesbare `startedAt` telt als vastgelopen zodra hij blokkeert: een
    aangehechte operation zonder begin is precies de losgekoppelde stand waar niets meer aan
    verandert.

    Een negatieve leeftijd leest als 0, om dezelfde reden als in ``format_duration``: bij
    klokverschil tussen ArgoCD (UTC) en OPI (CEST) ligt `startedAt` in de toekomst. Zonder die
    klem is een drempel van 0 geen "altijd zolang hij blokkeert" meer, en blijft juist op de
    time-out-tak de blokkade staan die daar opgeruimd moet worden. Op de drempel van 300
    verandert de klem niets.
    """
    if not operation.blocks_next_sync:
        return False
    age = operation_age_seconds(operation, now=now)
    return age is None or max(age, 0) >= threshold_seconds


async def terminate_stuck_operation(
    kubectl: KubectlConnector,
    app_name: str,
    namespace: str,
    *,
    threshold_seconds: int = STUCK_OPERATION_THRESHOLD_SECONDS,
) -> bool:
    """Beëindig de lopende sync-operation van ``app_name`` als die een volgende sync blokkeert.

    Returns True als er werkelijk een operation is weggehaald. False betekent dat er niets te
    beëindigen was (geen operation, al klaar, of nog jong genoeg om gewoon bezig te zijn), dat
    de patch mislukte (dat logt ``terminate_argocd_application_operation`` zelf), of dat
    kubectl niet bereikbaar was.

    Die laatste hoort hier en niet bij de aanroeper: dit is een best-effort opruiming, en
    beide aanroepers staan op een pad dat niet mag stranden. Voor de push is de volgorde juist
    commit, opruimen, push, dus een omgevallen kubectl zou de manifests gecommit en ongepusht
    achterlaten; op de time-out-tak zou hij van een nette time-out een ``KubectlConnectionError``
    maken. En die tak is niet zeldzaam: ``_run_kubectl_command`` gooit zodra
    ``KubectlConnector.isConnected`` False is, en die vlag gaat procesbreed om bij een enkele
    "connection refused", met een achtergrondretry van 30s.
    """
    try:
        # Geen eigen grendel op een Application die niet bestaat: die levert `None`, en daarop
        # blijft `attached` False, dus de grendel zou nooit aan de beurt komen.
        operation = read_sync_operation(await kubectl.get_argocd_application(app_name, namespace))
        if not operation.blocks_next_sync:
            return False

        age = operation_age_seconds(operation)
        if not is_stuck(operation, threshold_seconds=threshold_seconds):
            logger.info(
                "Sync-operation op '%s' loopt %ss (phase %s); onder de drempel van %ss, dus laten staan",
                app_name,
                age,
                operation.phase,
                threshold_seconds,
            )
            return False

        logger.warning(
            "Sync-operation op '%s' loopt %s (phase %s, revisie %s): %s. Die blokkeert elke volgende "
            "sync, dus OPI beëindigt hem",
            app_name,
            f"{age}s" if age is not None else "sinds een onbekend moment",
            operation.phase,
            operation.revision or "onbekend",
            operation.message or "geen bericht",
        )
        return await kubectl.terminate_argocd_application_operation(app_name, namespace)
    except (KubectlConnectionError, KubectlExecutionError) as e:
        logger.warning(
            "Kon de sync-operation op '%s' niet beoordelen of beëindigen: %s. Een eventuele "
            "blokkade blijft staan; de volgende ronde kijkt opnieuw",
            app_name,
            e,
        )
        return False


def format_duration(seconds: int | None) -> str:
    """Een looptijd als '45 seconden', '19 minuten', '2 uur', of 'onbekend lang'.

    Naar BENEDEN afgerond, zoals ``relatieve_tijd`` in ``opi/web/lotc_switch.py``: 119 minuten
    leest als '1 uur' en niet als '2 uur'. Een duur en geen klok, want ArgoCD rapporteert in
    UTC en OPI's eigen regels in CEST. Een negatieve duur komt daar ook vandaan, bij
    klokverschil tussen de twee, en leest als '0 seconden' in plaats van als een onmogelijk
    getal; ``relatieve_tijd`` vangt hem om dezelfde reden af.
    """
    if seconds is None:
        return "onbekend lang"
    seconds = max(seconds, 0)
    if seconds < 60:
        return "1 seconde" if seconds == 1 else f"{seconds} seconden"
    minutes = seconds // 60
    if minutes < 60:
        return "1 minuut" if minutes == 1 else f"{minutes} minuten"
    hours = minutes // 60
    return "1 uur" if hours == 1 else f"{hours} uur"
