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

from opi.services.catalog.base import ConfigLayer, Service
from opi.services.catalog.sleep_mode.config_model import SleepModeConfig
from opi.services.services import service_entry_name
from opi.services.services_enums import ServiceType


class SleepModeService(Service):
    service_type = ServiceType.SLEEP_MODE
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
