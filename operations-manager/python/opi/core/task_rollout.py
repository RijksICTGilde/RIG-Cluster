"""One place that decides whether a task rolls its change out to the cluster.

Endpoints that write to the project file accept ``rollout=false``: the change is
validated, saved and committed, but no manifests are generated and nothing reaches
the cluster. The project file then runs ahead of the cluster until someone calls
``POST /api/v2/projects/{project_name}/:refresh``.

The flag travels in the task payload and is read here, at the single point where a
handler is about to process the project -- not rebuilt per endpoint.

Not every task may skip. A refresh reprocesses what the project file declares; it
does not remove cluster resources the file no longer mentions. So a deferred delete
would simply never happen, and deleting task types are absent from
``DEFERRABLE_TASK_TYPES`` on purpose. Endpoints backing a non-deferrable task type
refuse ``rollout=false`` with an explanation instead of ignoring it.
"""

from typing import Any

PAYLOAD_KEY = "rollout"

# Task types whose rollout may be deferred: they only write to the project file, and a
# later refresh reconciles the result faithfully.
DEFERRABLE_TASK_TYPES = frozenset(
    {
        "add_component",
        "update_component",
        "add_component_to_deployment",
        "add_service",
        "configure_service",
        "update_image",
        "upsert_deployment",
    }
)

# Why each non-deferrable task type refuses the flag. Keyed by task type so the API can
# tell the caller what is wrong rather than silently rolling out anyway.
NON_DEFERRABLE_REASONS: dict[str, str] = {
    "refresh_project": "processing the project is the whole point of this operation",
    "refresh_deployment": "processing the deployment is the whole point of this operation",
    "delete_deployment": (
        "deleting removes cluster resources; a later refresh reprocesses what the project "
        "file declares and would never perform the deletion"
    ),
    "clone_database": "cloning acts on the cluster directly and writes nothing to the project file",
    "clone_bucket": "cloning acts on the cluster directly and writes nothing to the project file",
}

# Task types that reconcile the WHOLE project and therefore clear every deferred change
# before them. A refresh is what the API tells the caller to run, and delete-component runs
# the same refresh internally. Deliberately narrow: a task that processes only one
# deployment (add_component, refresh_deployment) leaves the rest of the file ahead of the
# cluster, so counting it as a rollout would hide real drift.
ROLLOUT_CLEARING_TASK_TYPES = frozenset({"refresh_project", "delete_component"})

# What the caller sees in ``processing`` when the rollout was skipped. ``skipped`` is the
# status the handlers already use for "no processing happened"; the reason distinguishes
# "you asked for this" from "there was nothing to do".
SKIPPED_REASON = "rollout_disabled"
SKIPPED_MESSAGE = (
    "Change saved to the project file; not rolled out because rollout=false was requested. "
    "Call POST /api/v2/projects/{project_name}/:refresh to roll it out."
)

# Shown as a task step, so the progress view says what did not happen instead of just
# missing a step.
SKIPPED_STEP_LABEL = "Uitrol overgeslagen (rollout=false)"
SKIPPED_STEP_DETAIL = (
    "Wijziging staat in het projectbestand. Verwerk hem met 'Project herverwerken' wanneer je zover bent."
)


def rollout_requested(payload: dict) -> bool:
    """Whether this task should roll its change out. Absent flag means yes."""
    return bool(payload.get(PAYLOAD_KEY, True))


def note_rollout_skipped(progress: Any) -> None:
    """Record the skipped rollout as a completed step in the task progress."""
    step = progress.add_task(SKIPPED_STEP_LABEL)
    progress.update_current_step(SKIPPED_STEP_DETAIL)
    progress.complete_task(step)


def skipped_processing() -> dict[str, str]:
    """The ``processing`` block for a task that deliberately did not roll out."""
    return {"status": "skipped", "reason": SKIPPED_REASON, "message": SKIPPED_MESSAGE}
