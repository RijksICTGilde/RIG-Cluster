"""Clone state (``generation`` + ``revisions``) shared by services that can be cloned.

A cloned resource keeps a generation counter and a log of the resources that generation
produced, so a restore knows which database or bucket a deployment is actually pointing at.
``postgresql-database`` and ``minio-storage`` both carry it today; it is a recurring pattern
rather than a property of either service, so the shape lives here next to ``storage.py``
instead of being copied into both packages.

Written and read by ``opi/manager/revision_manager.py``. This is OPI-managed state, not a
user setting: nothing in the forms layer edits it, and it has no editables or visualizers.
Modelling it anyway is the point of the service contract, that a service can say what may
appear in its config block.

**The layer is the composing service's choice.** Both services put it on the deployment
today, but nothing here assumes that. A service that needs per-component clone state adds
``CloneState`` to its component-level model instead. Two things still stand in the way of
actually doing that, both tracked separately: ``$defs/deployment-service-config`` in
``project_v2.json`` is a closed object, and ``revision_manager._get_service_config`` hardcodes
a JSONPath that assumes the deployment layer and the ``reference`` entry form.

Required fields follow what 33 real revisions across the production project files actually
carry: everything except ``superseded_at``, which only appears once a revision is retired.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class RevisionAction(BaseModel):
    """One entry in a revision's audit trail (created, cloned, restored, ...)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: str = Field(description="What happened: created, cloned, restored, ...")
    source: str = Field(description="Where it came from: the deployment, project or external source cloned from.")
    timestamp: str = Field(description="When it happened, ISO 8601.")


class Revision(BaseModel):
    """One generation's resource, with the actions that produced and changed it."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    generation: int = Field(description="The generation number this revision records.")
    resource: str = Field(
        description="The provisioned resource this generation points at (database name, bucket name)."
    )
    status: str = Field(description="'active' for the generation in use, 'superseded' once a newer one took over.")
    created_at: str = Field(description="When this generation was provisioned, ISO 8601.")
    superseded_at: str | None = Field(default=None, description="When it was retired; absent on the active generation.")
    actions: list[RevisionAction] = Field(default=[], description="Audit trail of what was done to this generation.")


class CloneState(BaseModel):
    """Mix into a service's config model to give it clone state.

    Both fields are optional: a service that has never been cloned carries neither, and
    ``validate_config`` is called with an empty dict in that case.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    generation: int | None = Field(
        default=None, description="Current generation, incremented on each clone. Written by the platform."
    )
    revisions: list[Revision] = Field(
        default=[], description="Every generation this resource has had, newest last. Written by the platform."
    )
