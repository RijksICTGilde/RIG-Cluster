"""resource-tuning service package.

A system service (``kind=SYSTEM``): it is never in a project's services list and is not
user-selectable. It owns its config (``config.py`` / ``config_model.py``) and overrides
the after-sync observation hook to raise memory for a component that OOM'd.
"""

from __future__ import annotations

import logging

from opi.services.catalog.base import DeploymentObservationContext, ObservationOutcome, Service
from opi.services.catalog.events import on
from opi.services.services import ServiceDefinition
from opi.services.services_enums import ActionEvent, ServiceKind, ServiceType

logger = logging.getLogger(__name__)


class ResourceTuningService(Service):
    service_type = ServiceType.RESOURCE_TUNING
    definition = ServiceDefinition(
        name="Resource tuning",
        description=(
            "Systeemdienst: houdt draaiende deployments in de gaten na een sync en hoogt "
            "het geheugen op van een component dat OOM'd. Draait altijd, is niet kiesbaar."
        ),
        help_template="resource_tuning/help.md",
        icon="grafiek",
        color="grijs-600",
        selectable_per_component=False,
        variables=[],
        # Always on, never in the project file -> a system service (kind=SYSTEM also
        # keeps it out of the picker, so no explicit hidden is needed).
        kind=ServiceKind.SYSTEM,
    )
    # May enrol itself (RC-84): a system service is not a user choice, so there is no
    # project-level decision to make first.
    allows_implicit_project_selection = True

    @on(ActionEvent.AFTER_SYNC)
    async def tune_after_oom(self, ctx: DeploymentObservationContext) -> list[ObservationOutcome]:
        """After a sync, tune the memory of any component that OOM'd (tasks 2, 7, 10).

        Reads the OOM signals from the context, tunes only the OOM'd components on the
        OOM path, compacts history, and reports the outcome. It only mutates
        ``ctx.project_data`` and never commits -- the runner commits once for the whole
        scan, which is the ``ActionEvent`` contract.

        This is also where the pod-generation lock is applied, because every tuning path
        comes through here and this is the only place that knows which component was
        actually raised. The answered generations are reported, not recorded: the runner
        records them once its commit succeeded.
        """
        oom_components = [ref for ref, health in ctx.component_health.items() if health.oom_detected]
        if not oom_components:
            return []

        # Lazy import: resource_tuning_service indirection to avoid project_manager at load time.
        from opi.handlers.project_file_handler import ProjectFileHandler
        from opi.services.oom_watcher import oom_is_fresh_evidence
        from opi.services.resource_tuning_service import apply_resource_tuning, describe_growth_ceiling_block
        from opi.utils.naming import generate_unique_name

        # One tune per generation: a pod on the pre-increase template is stale evidence.
        # Unknown generation tunes anyway (conservative, like elsewhere in oom_is_fresh_evidence).
        generations = {
            ref: ctx.component_health[ref].oom_pod_template_hash
            for ref in oom_components
            if oom_is_fresh_evidence(
                ctx.project_name,
                ctx.deployment_name,
                generate_unique_name(ctx.deployment_name, ref),
                ctx.component_health[ref].oom_pod_template_hash,
            )
        }
        answered = [ref for ref in oom_components if ref not in generations]
        if answered:
            logger.info(
                "Resource tuning: OOM for %s in %s/%s is on the pod generation the previous tune "
                "already answered, waiting for that increase to roll out",
                ", ".join(answered),
                ctx.project_name,
                ctx.deployment_name,
            )
        oom_components = list(generations)
        if not oom_components:
            return []

        file_handler = ProjectFileHandler()
        try:
            changes, _ = await apply_resource_tuning(
                ctx.project_data, file_handler, ctx.deployment_name, oom_components=oom_components
            )
        except RuntimeError as exc:
            return [
                ObservationOutcome(
                    failures=[
                        f"OOM detected for {', '.join(oom_components)} in {ctx.deployment_name}, "
                        f"auto-tune failed: {exc}"
                    ]
                )
            ]

        if not changes:
            # Distinguish "no limit could be computed" from "a limit was computed and
            # refused". The second is the escalation ceiling, and it needs its own text:
            # the old wording sent people looking for missing metrics while the real
            # answer is that this component has outgrown its declared limit.
            blocked = [
                message
                for ref in oom_components
                if (message := describe_growth_ceiling_block(ctx.project_data, file_handler, ctx.deployment_name, ref))
            ]
            if blocked:
                return [ObservationOutcome(failures=blocked)]
            return [
                ObservationOutcome(
                    failures=[
                        f"OOM detected for {', '.join(oom_components)} in {ctx.deployment_name} "
                        "but auto-tune could not determine new limits"
                    ]
                )
            ]

        file_handler.compact_resource_history(ctx.project_data)
        return [
            ObservationOutcome(
                project_data_changed=True,
                requeue_refresh=True,
                oom_tune_generations={
                    generate_unique_name(ctx.deployment_name, record["component"]): generations.get(record["component"])
                    for record in changes
                },
            )
        ]
