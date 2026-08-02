"""resource-tuning service package.

A system service (``kind=SYSTEM``): it is never in a project's services list and is not
user-selectable. It owns its config (``config.py`` / ``config_model.py``) and overrides
the after-sync observation hook to raise memory for a component that OOM'd.
"""

from __future__ import annotations

from opi.services.catalog.base import DeploymentObservationContext, ObservationOutcome, Service
from opi.services.services_enums import ServiceType


class ResourceTuningService(Service):
    service_type = ServiceType.RESOURCE_TUNING

    async def observe_deployment(self, ctx: DeploymentObservationContext) -> ObservationOutcome:
        """After a sync, tune the memory of any component that OOM'd (tasks 2, 7, 10).

        Reads the OOM signals from the context, tunes only the OOM'd components on the
        OOM path, compacts history, and reports the outcome. It only mutates
        ``ctx.project_data``; the runner commits once for all hooks together.
        """
        oom_components = [ref for ref, health in ctx.component_health.items() if health.oom_detected]
        if not oom_components:
            return ObservationOutcome()

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
            return ObservationOutcome(
                failures=[
                    f"OOM detected for {', '.join(oom_components)} in {ctx.deployment_name}, auto-tune failed: {exc}"
                ]
            )

        if not changes:
            return ObservationOutcome(
                failures=[
                    f"OOM detected for {', '.join(oom_components)} in {ctx.deployment_name} "
                    "but auto-tune could not determine new limits"
                ]
            )

        file_handler.compact_resource_history(ctx.project_data)
        return ObservationOutcome(project_data_changed=True, requeue_refresh=True)
