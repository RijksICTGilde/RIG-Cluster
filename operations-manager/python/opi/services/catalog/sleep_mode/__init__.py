"""sleep-mode service: scale idle preview deployments to zero, wake on request.

The package is self-contained: this module is the declaration hub (config model +
version), ``config.py`` owns the cluster-wide default and the project merge,
``state.py`` the per-deployment runtime state, and the remaining modules the token,
the state transitions, the waker manifests, the action button, the API router and the
sweeper. Only ``__init__.py`` is imported by the registry; the router and scheduler are
bound explicitly by ``server.py`` (they pull in FastAPI / managers, which the catalog
must stay free of).
"""

from __future__ import annotations

from typing import Any

from opi.services.catalog.base import ConfigLayer, DeploymentStateContext, DeploymentStateFact, Service
from opi.services.catalog.sleep_mode.config_model import SleepModeConfig
from opi.services.services import ServiceDefinition, service_entry_name
from opi.services.services_enums import ServiceBinding, ServiceType


class SleepModeService(Service):
    service_type = ServiceType.SLEEP_MODE
    definition = ServiceDefinition(
        name="Slaapstand",
        description=(
            "Zet bepaalde deployments, op basis van matching, na een deadline in slaapstand "
            "en wek ze op verzoek weer op. De deployment doet een koude start."
        ),
        help_template="sleep_mode/help.html.j2",
        icon="klok",
        color="donkerblauw",
        binding=ServiceBinding.DEPLOYMENT,
        # Selectable in the wizard with its own project-level config section
        # (SleepModeService.config_form_section). A cluster-wide default still
        # applies, and `match` scopes which deployments it affects.
        variables=[],
        # actions_provider is bound by opi/services/catalog/sleep_mode/__init__.py
        # (the wake button); services.py must not import the catalog package.
    )
    config_model = SleepModeConfig
    config_schema_version = "1.0"
    config_section_id = "sleep-mode-config"
    modal_flow_id = "modal-edit-sleep-mode-config"

    # --- config field ownership (project level) ---------------------------------
    # sleep-mode owns its project-level config fields; the wizard/edit layer sources
    # this section from here. Forms building blocks are imported lazily so the catalog
    # stays forms-free at import time (avoids the forms -> registry -> catalog cycle).

    def _config_selected(self, project_data: dict[str, Any]) -> bool:
        """Section visibility: whether this project selected sleep-mode."""
        return self.service_type.value in [
            service_entry_name(entry) for entry in project_data.get("services", []) or []
        ]

    def config_api_fields(self, layer: ConfigLayer) -> list[str]:
        return self.config_model_field_names() if layer is ConfigLayer.PROJECT else []

    def config_editables(self, layer: ConfigLayer):
        if layer is not ConfigLayer.PROJECT:
            return []
        from opi.services.catalog.sleep_mode.editables import SLEEP_MODE_EDITABLES

        return SLEEP_MODE_EDITABLES

    def deployment_state(self, ctx: DeploymentStateContext) -> list[DeploymentStateFact]:
        """Report that this deployment is asleep, or on its way back (RC-28).

        Sleep-mode is the reason this hook exists: it scales the application to zero and
        parks a waker in front of it, and nothing outside the service knew. The health
        check and the deployment page read the situation from here.

        Facts, not verdicts:

        * ``sleeping`` -- the application was scaled to zero deliberately, so zero
          application pods is the intended state (``expects_no_application_pods``).
        * ``waking`` -- the transitional state. Pods are supposed to be coming back, so
          this deliberately does NOT excuse their absence; a wake that never produces
          pods must still be visible as a problem.
        * ``awake`` -- nothing to report.

        Read from the project file (the service's own record), never from the cluster.
        """
        from opi.services.catalog.sleep_mode.state import STATE_SLEEPING, STATE_WAKING, read

        sleep = read(ctx.project_data, ctx.deployment_name)
        if sleep.state == STATE_SLEEPING:
            return [
                DeploymentStateFact(
                    service=self.service_type.value,
                    summary=(
                        "Deze deployment slaapt: de componenten zijn naar nul geschaald en worden "
                        "gewekt bij het eerste bezoek."
                    ),
                    expects_no_application_pods=True,
                    details={"state": sleep.state},
                )
            ]
        if sleep.state == STATE_WAKING:
            return [
                DeploymentStateFact(
                    service=self.service_type.value,
                    summary="Deze deployment wordt gewekt uit de slaapstand; de componenten starten op.",
                    # Explicitly False: during a wake the pods are supposed to return, so
                    # their absence is exactly what should stay visible.
                    expects_no_application_pods=False,
                    details={"state": sleep.state, "expires-at": sleep.expires_at},
                )
            ]
        return []

    def config_form_section(self, layer: ConfigLayer):
        if layer is not ConfigLayer.PROJECT:
            return None
        # Built once and cached so consumers comparing section identity keep one object.
        cached = getattr(self, "_config_section_cache", None)
        if cached is None:
            from opi.forms.visualizers.sections import FormSection
            from opi.services.catalog.sleep_mode.editables import SLEEP_MODE_EDITABLES
            from opi.services.catalog.sleep_mode.visualizers import SLEEP_MODE_VISUALIZERS

            cached = FormSection(
                section_id="sleep-mode-config",
                title="Slaapstand configuratie",
                icon="klok",
                description="Instellingen voor de slaapstand en het wekken",
                visible=self._config_selected,
                post_save_action="process_project",
                editables=SLEEP_MODE_VISUALIZERS,
                layout=[editable.yaml_path for editable in SLEEP_MODE_EDITABLES],
            )
            self._config_section_cache = cached
        return cached


# Bind the wake button onto the bound ServiceDefinition. Done here (not in services.py)
# so services.py never imports the catalog package -- the definition is a mutable
# dataclass, and the registry imports this module at startup, before any request.
from opi.services.catalog.sleep_mode.actions import sleep_actions  # noqa: E402

SleepModeService.definition.actions_provider = sleep_actions
