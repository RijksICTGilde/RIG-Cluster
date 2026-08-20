"""Typed config for the resource-tuning system service.

The tuning parameters used to be thirteen loose ``RESOURCE_TUNING_*`` fields on
``Settings`` that nothing validated as a coherent whole. They belong to the service,
so they live here as a single typed, validated unit -- the same shape the plan calls
for and the pattern sleep-mode uses (a dict of values plus a Pydantic model over it).

These are deliberately NOT environment-driven: they have never been set via env vars
in practice, and a system service owns its own configuration rather than reading it
from the platform's ``Settings``.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ResourceTuningConfig(BaseModel):
    """The memory/CPU auto-tuning parameters, typed and validated.

    Instantiated once from ``config._VALUES``; every field is required so a missing
    value fails loud at startup instead of silently defaulting.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: How far back the Prometheus max-usage window looks (hours).
    window_hours: int
    #: Percent added above observed max to size the memory request.
    memory_buffer_percent: int
    #: Memory limit = observed peak x this factor (burst headroom above the peak).
    memory_limit_factor: float
    #: Legacy symmetric threshold, superseded by the asymmetric deviation gate.
    #: Kept for parity; read by nothing today.
    threshold_percent: int
    #: An OOM floor may expire after this many days...
    oom_floor_min_age_days: int
    #: ...if the observed max stays below this percent of the floor.
    oom_floor_stable_percent: int
    #: A value a user set by hand may expire after this many days...
    user_intent_min_age_days: int
    #: ...if the measured usage stays below this percent of what they set. Starts equal
    #: to the OOM-floor pair so the two are explainable together, but stands apart so it
    #: can be tuned on its own.
    user_intent_stable_percent: int
    #: Apply an increase once the request grows by at least this percent.
    increase_threshold: int
    #: Apply a decrease only once the request shrinks by at least this percent.
    decrease_threshold: int
    #: Whether the nightly fleet-wide sweep runs.
    scheduler_enabled: bool
    #: Hour (Europe/Amsterdam) of the nightly sweep; before backups (~2am).
    hour: int
    #: Delay after each changed project, to spread pod rollouts.
    pace_seconds: int
    #: Absolute memory deadband (Mi): ignore a change smaller than this.
    min_delta_mi: int
    #: Absolute CPU deadband (millicores): ignore a change smaller than this.
    min_delta_m: int
    #: Minimum absolute headroom the memory limit keeps above the request (Mi), so a
    #: container never ends up with limit == request (which dies on the first spike).
    min_limit_headroom_mi: int
    #: Below this observed max (Mi) the measurement is treated as "no data" rather
    #: than as a real value. A container that actually ran uses more than this within
    #: seconds; a fraction of a Mi is the footprint of a pod that barely existed
    #: inside the window, and sizing on it lands on the cluster minimum.
    min_observed_mi: float
