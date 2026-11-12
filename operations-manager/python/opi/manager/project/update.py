"""The one use case behind every write of a whole project file from the portal.

The wizard, the three modal routes and the approval route each merge their own form data
into the project. From the merge on it is one change: guard what no form may set, weigh
what the change asks of the caller, save through the store, and name the deployments the
result has to reach. Every entrance asks for that here, so none of them can answer the
same request differently.
"""

from __future__ import annotations

import copy
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from opi.core.authz.actions import Action
from opi.forms.wizard.members import restore_hidden_members
from opi.manager.project.errors import StepUpRequiredError
from opi.services.approvals import ensure_approval_requests
from opi.services.clone_validation import new_or_changed_clone_sources
from opi.services.members import member_list_changed
from opi.services.project_environment import environment_markings, log_production_downgrades
from opi.services.project_utils import deployments_touched
from opi.services.services import ServiceAdapter
from opi.services.step_up import log_step_up_refusal, production_marking_change, save_requires_step_up

if TYPE_CHECKING:
    from opi.manager.project.edit_authority import EditAuthority
    from opi.manager.project_manager import ProjectManager

#: An entrance's merge, run on the project as it stands in git at save time. Sync or async,
#: so a merge that awaits its hooks and one that does not both fit.
type ApplyChange = Callable[[dict[str, Any]], dict[str, Any] | Awaitable[dict[str, Any]]]


class EditGuard(StrEnum):
    """One guard or normalization a project edit can be held to."""

    #: Put back every service-config field the platform owns and no form may set.
    PLATFORM_FIELDS = "platform-fields"
    #: Put back the service accounts and token hashes the team form does not carry.
    HIDDEN_MEMBERS = "hidden-members"
    #: Record an approval request for everything the result asks for and has not been granted.
    APPROVAL_REQUESTS = "approval-requests"
    #: Weigh ``member.manage`` when the change touches the member list.
    MEMBER_ROLE = "member-role"
    #: Weigh ``environment.set`` when the change touches a production marking.
    MARKING = "marking"
    #: Weigh ``deployment.clone-from`` on the source of every clone-from the change sets.
    CLONE_SOURCE = "clone-source"
    #: Demand a fresh second factor for a change that touches production.
    STEP_UP = "step-up"


ALL_GUARDS: frozenset[EditGuard] = frozenset(EditGuard)


@dataclass(frozen=True)
class ProjectChange:
    """One write of a whole project file, whichever entrance asked for it."""

    project_name: str
    apply: ApplyChange
    commit_message: str
    guards: frozenset[EditGuard] = ALL_GUARDS


@dataclass(frozen=True)
class ProjectUpdateResult:
    """What the write produced: the saved file, and which deployments have to follow.

    ``rollout_deployments`` None means project-wide, a list narrows the round, and an empty
    list means no deployment needs anything. It is read off the difference and not handed
    in: none of the entrances knows what its own merge touched.
    """

    after: dict[str, Any]
    changed: bool
    rollout_deployments: list[str] | None

    @property
    def needs_rollout(self) -> bool:
        """Whether this write leaves the cluster something to do."""
        return self.changed and self.rollout_deployments != []


async def update_project(
    change: ProjectChange, authority: EditAuthority, project_manager: ProjectManager
) -> ProjectUpdateResult:
    """Merge ``change`` into the project as git holds it now, and save the result.

    The read is fresh on purpose: the merge has to land on the current state, and that same
    read is the compare-and-swap base of the save, so a change that arrived in between is
    merged by the store instead of overwritten.

    An approver's verdict goes through :func:`record_approval_decision` instead; this write
    may not set an approval status.

    Raises:
        StepUpRequiredError: when the change touches production without a fresh second factor.
        ProjectSchemaError, ProjectIntegrityError, ConflictError: raised by the save.
        HTTPException: raised by ``authority`` when the caller's roles fall short.
    """
    before, after = await _merge_and_weigh(change, authority, project_manager)
    await project_manager.save_and_commit_project(after, change.commit_message)
    return _finish(change, authority, before, after)


async def record_approval_decision(
    change: ProjectChange, authority: EditAuthority, project_manager: ProjectManager
) -> ProjectUpdateResult:
    """Save ``change`` as an approver's verdict: the one write that may set a status.

    Only the approval route may ask for this; every other entrance uses
    :func:`update_project`. The ``approval_decision=True`` below is the single marked save
    in the application, and ``validate_approval_decisions`` in the store is what makes it
    the only one that can set ``approved`` or ``denied``.
    """
    before, after = await _merge_and_weigh(change, authority, project_manager)
    await project_manager.save_and_commit_project(after, change.commit_message, approval_decision=True)
    return _finish(change, authority, before, after)


async def _merge_and_weigh(
    change: ProjectChange, authority: EditAuthority, project_manager: ProjectManager
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The read, the entrance's merge, the guards and the weighing. Returns (before, after)."""
    stored = await project_manager.get_contents()
    before = copy.deepcopy(stored)

    applied = change.apply(stored)
    after = await applied if inspect.isawaitable(applied) else applied

    if EditGuard.PLATFORM_FIELDS in change.guards:
        ServiceAdapter.enforce_platform_managed_fields(after, before)
    if EditGuard.HIDDEN_MEMBERS in change.guards:
        restore_hidden_members(after, before)
    if EditGuard.APPROVAL_REQUESTS in change.guards:
        ensure_approval_requests(after)

    _weigh(change, authority, before, after)
    return before, after


def _finish(
    change: ProjectChange, authority: EditAuthority, before: dict[str, Any], after: dict[str, Any]
) -> ProjectUpdateResult:
    """Report the downgrade this save made, and what the cluster still has to do."""
    log_production_downgrades(
        environment_markings(before),
        environment_markings(after),
        project_name=change.project_name,
        actor=authority.actor,
    )
    return ProjectUpdateResult(
        after=after,
        changed=after != before,
        rollout_deployments=deployments_touched(before, after),
    )


def _weigh(change: ProjectChange, authority: EditAuthority, before: dict[str, Any], after: dict[str, Any]) -> None:
    """Weigh everything the change asks of the caller, before anything is written.

    Each of these reads the AFTER state, so they can only run once the merge is done; a
    refusal here leaves nothing behind.
    """
    if EditGuard.MEMBER_ROLE in change.guards and member_list_changed(before, after):
        authority.require(Action.MEMBER_MANAGE)

    if EditGuard.MARKING in change.guards and production_marking_change(
        environment_markings(before), environment_markings(after)
    ):
        authority.require(Action.ENVIRONMENT_SET)

    if EditGuard.CLONE_SOURCE in change.guards:
        for source in new_or_changed_clone_sources(before, after):
            authority.require(Action.DEPLOYMENT_CLONE_FROM, deployment=source)

    if EditGuard.STEP_UP not in change.guards:
        return
    handeling = save_requires_step_up(before, after)
    if handeling is None or authority.step_up_is_fresh():
        return
    raise StepUpRequiredError(
        handeling,
        log_step_up_refusal(handeling, project_name=change.project_name, actor=authority.actor),
    )
