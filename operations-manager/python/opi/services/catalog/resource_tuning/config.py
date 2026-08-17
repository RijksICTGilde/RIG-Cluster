"""Resource-tuning config values, owned by the service (was ``RESOURCE_TUNING_*``).

The values live here as a plain dict and are validated once through
``ResourceTuningConfig``. Mirrors the sleep-mode pattern (values in the service, a
Pydantic model over them) so the service stays self-contained -- explicitly not in
``core/config.py``/``Settings`` and not environment-driven.
"""

from __future__ import annotations

from typing import Any

from opi.services.catalog.resource_tuning.config_model import ResourceTuningConfig

#: The single source of the tuning numbers. Keep values here; the model types them.
_VALUES: dict[str, Any] = {
    "window_hours": 24,
    "memory_buffer_percent": 25,
    "memory_limit_factor": 1.5,
    "threshold_percent": 20,
    "oom_floor_min_age_days": 10,
    "oom_floor_stable_percent": 50,
    "increase_threshold": 10,
    "decrease_threshold": 30,
    "scheduler_enabled": True,
    "hour": 1,
    "pace_seconds": 15,
    "min_delta_mi": 16,
    "min_delta_m": 10,
    "min_limit_headroom_mi": 64,
    "min_observed_mi": 5.0,
}

_config: ResourceTuningConfig | None = None


def resource_tuning_config() -> ResourceTuningConfig:
    """The validated tuning config (built once, cached)."""
    global _config
    if _config is None:
        _config = ResourceTuningConfig.model_validate(_VALUES)
    return _config
