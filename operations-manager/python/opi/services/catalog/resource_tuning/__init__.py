"""resource-tuning service package.

A system service (``kind=SYSTEM``): it is never in a project's services list and is not
user-selectable. It owns its config (``config.py`` / ``config_model.py``) and overrides
the after-sync observation hook to raise memory for a component that OOM'd.
"""

from __future__ import annotations

from opi.services.catalog.base import DeploymentObservationContext, ObservationOutcome, Service
from opi.services.catalog.events import on
from opi.services.services import ServiceDefinition
from opi.services.services_enums import ActionEvent, ServiceBinding, ServiceKind, ServiceType


class ResourceTuningService(Service):
    service_type = ServiceType.RESOURCE_TUNING
    definition = ServiceDefinition(
        name="Resource tuning",
        description=(
            "Systeemdienst: houdt draaiende deployments in de gaten na een sync en hoogt "
            "het geheugen op van een component dat OOM'd. Draait altijd, is niet kiesbaar."
        ),
        help_template="resource_tuning/help.html.j2",
        icon="grafiek",
        color="grijs-600",
        binding=ServiceBinding.DEPLOYMENT,
        variables=[],
        # Always on, never in the project file -> a system service (kind=SYSTEM also
        # keeps it out of the picker, so no explicit hidden is needed).
        kind=ServiceKind.SYSTEM,
    )

    @on(ActionEvent.AFTER_SYNC)
    async def tune_after_oom(self, ctx: DeploymentObservationContext) -> list[ObservationOutcome]:
        """After a sync, tune the memory of any component that OOM'd (tasks 2, 7, 10).

        Reads the OOM signals from the context, tunes only the OOM'd components on the
        OOM path, compacts history, and reports the outcome. It only mutates
        ``ctx.project_data`` and never commits -- the runner commits once for the whole
        scan, which is the ``ActionEvent`` contract.
        """
        oom_components = [ref for ref, health in ctx.component_health.items() if health.oom_detected]
        if not oom_components:
            return []

        # Lazy import: resource_tuning_service pulls in the project manager, which the
        # catalog must not import at load time.
        from opi.handlers.project_file_handler import ProjectFileHandler
        from opi.services.resource_tuning_service import apply_resource_tuning

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
            return [
                ObservationOutcome(
                    failures=[
                        f"OOM detected for {', '.join(oom_components)} in {ctx.deployment_name} "
                        "but auto-tune could not determine new limits"
                    ]
                )
            ]

        file_handler.compact_resource_history(ctx.project_data)
        return [ObservationOutcome(project_data_changed=True, requeue_refresh=True)]
