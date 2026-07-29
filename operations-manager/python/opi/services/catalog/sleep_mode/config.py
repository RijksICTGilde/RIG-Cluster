"""Load and merge sleep-mode config: service-owned cluster default + project override.

The cluster-wide default lives here, in the service package -- not in
``core/cluster_config.py`` -- so sleep-mode stays self-contained. ``load`` merges the
cluster default under the project's own config (project wins per key), validates the
result through ``SleepModeConfig`` and returns it, or ``None`` when sleep-mode is off
for this cluster/project.
"""

from __future__ import annotations

from typing import Any

from opi.services.catalog.sleep_mode.config_model import SleepModeConfig
from opi.services.services import service_entry_config, service_entry_name
from opi.services.services_enums import ServiceType

#: Cluster-wide defaults, owned by the service. Ship OFF everywhere; turn on per
#: cluster only where the behaviour is confirmed. Each entry is a partial override of
#: the model defaults; a project overrides these again, per key. A cluster missing
#: from this map inherits the model defaults (enabled=False -> off).
_CLUSTER_DEFAULTS: dict[str, dict[str, Any]] = {
    # Confirmed on the local Kind sandbox first; production stays off until validated.
    "sandboxed-local": {"enabled": True},
}


class SleepModeConfigError(ValueError):
    """Raised when a project's sleep-mode config is invalid (fail loud, never silent)."""


def _project_config(project_data: dict[str, Any]) -> dict[str, Any]:
    """The raw ``services/sleep-mode/config`` block from the project, or ``{}``."""
    for entry in project_data.get("services", []) or []:
        if service_entry_name(entry) == ServiceType.SLEEP_MODE.value:
            config = service_entry_config(entry)
            return config if isinstance(config, dict) else {}
    return {}


def load(project_data: dict[str, Any], cluster: str) -> SleepModeConfig | None:
    """Resolve sleep-mode config for a project on a cluster, or ``None`` when off.

    Merges the service-owned cluster default under the project config (project wins),
    validates, and returns the config only when ``enabled`` is true. A ``waker-component``
    that does not name an existing component fails loud -- a config error, not a corner
    case.
    """
    cluster_default = _CLUSTER_DEFAULTS.get(cluster, {})
    merged = {**cluster_default, **_project_config(project_data)}
    try:
        config = SleepModeConfig.model_validate(merged)
    except ValueError as exc:
        raise SleepModeConfigError(f"Invalid sleep-mode config: {exc}") from exc

    if not config.enabled:
        return None

    if config.waker_component is not None:
        component_names = {comp.get("name") for comp in project_data.get("components", []) or []}
        if config.waker_component not in component_names:
            raise SleepModeConfigError(
                f"sleep-mode waker-component '{config.waker_component}' is not a component of this project "
                f"(known components: {sorted(n for n in component_names if n)})"
            )

    return config
