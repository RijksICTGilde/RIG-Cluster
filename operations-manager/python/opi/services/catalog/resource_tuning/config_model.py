"""Typed config for the resource-tuning system service.

The tuning parameters belong to the service, so they live here as a single typed,
validated unit: the values in ``config.py``, this model over them -- the pattern
sleep-mode uses.

These are deliberately NOT environment-driven: a system service owns its own
configuration rather than reading it from the platform's ``Settings``.
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
    #: Symmetric threshold, read by nothing today; the asymmetric deviation gate below is
    #: what decides.
    threshold_percent: int
    #: An OOM floor may expire after this many days...
    oom_floor_min_age_days: int
    #: ...if the observed max stays below this percent of the floor.
    oom_floor_stable_percent: int
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
    #: Hard ceiling for the automatic path, as a multiple of the memory limit declared
    #: on the catalog component: an OOM-driven bump may raise a deployment override to
    #: at most ``declared limit x this factor``. Without it the only bound is the cluster
    #: ceiling, and a component can climb two orders of magnitude in a handful of
    #: automated rounds. The catalog value is a stable anchor precisely because the tuner
    #: never writes to it -- it only writes deployment overrides -- so the ratio cannot
    #: drift along with the increases.
    max_growth_factor: float
    #: Below this observed max (Mi) the measurement is treated as "no data" rather
    #: than as a real value. A container that actually ran uses more than this within
    #: seconds; a fraction of a Mi is the footprint of a pod that barely existed
    #: inside the window, and sizing on it lands on the cluster minimum.
    min_observed_mi: float
