"""Generic approval capability for the service catalog (RC-5).

A service declares -- as data -- that some value it manages needs approval before it
takes effect ("dit heeft approval nodig"), and supplies the rule that reads the stored
approval state back ("is dit veld approved?"). Generic code (a future catalog-driven
approval interface, and the enforcers today) consumes these ``ApprovalSpec``s uniformly
instead of hard-coding one subsystem per approvable thing.

Grounded in the single real case today: publish-on-web's domain / subdomain approval,
whose state lives in the project's ``domains:`` block and whose rules are the pure
predicates in ``connectors/subdomain.py``. This module does NOT move that state or
rewrite those rules -- it wraps them so the shape (declare + check) is visible and
reusable. Moving the state under the service is a separate schema+data migration
(see features/futures/service-vertical-slice.md).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable


class ApprovalStatus(StrEnum):
    """Where a requested value sits in the approval lifecycle.

    The three non-NONE values match the strings persisted in the project file
    (``requested`` / ``approved`` / ``denied``), so a stored status maps straight onto
    the enum. NONE means "no request on record" -- nothing to approve yet.
    """

    NONE = "none"
    REQUESTED = "requested"
    APPROVED = "approved"
    DENIED = "denied"


class ApproverScope(StrEnum):
    """Who is entitled to approve a value -- what makes the capability reusable.

    Domains are cross-project, so a PLATFORM_ADMIN approves them. A future service
    granting e.g. cross-namespace access would be approved within the project
    (PROJECT_ADMIN / PROJECT_MEMBER). The scope selects which approver a generic
    approval interface routes a pending item to.
    """

    PLATFORM_ADMIN = "platform-admin"
    PROJECT_ADMIN = "project-admin"
    PROJECT_MEMBER = "project-member"


@dataclass(frozen=True)
class ApprovalSpec:
    """A service's declaration that a value it manages requires approval.

    This is the DEFINITION ("dit heeft approval nodig"): a service returns one or more
    of these from ``config_approvals(layer)``. It is pure data plus one rule callback --
    it carries no forms / manager / DB imports, so the catalog stays load-light.

    Attributes:
        key: Stable identifier for this approvable within the service (e.g. ``"domain"``,
            ``"subdomain"``). Used to look the spec up and to key any UI / state.
        label: Human label for the approver-facing interface.
        approver: Who may approve (see :class:`ApproverScope`).
        status_of: The CHECK ("is dit veld approved?"). Given the project data and the
            value being requested, returns its :class:`ApprovalStatus` by reading the
            stored approval state. The service owns this rule; generic code just calls
            it. The ``value`` is opaque to the generic layer -- its shape is the
            service's business (a domain string, a ``(domain, subdomain)`` pair, ...).
    """

    key: str
    label: str
    approver: ApproverScope
    status_of: Callable[[dict[str, Any], Any], ApprovalStatus]

    def status(self, project_data: dict[str, Any], value: Any) -> ApprovalStatus:
        """The approval status of ``value`` given the current project state."""
        return self.status_of(project_data, value)

    def is_approved(self, project_data: dict[str, Any], value: Any) -> bool:
        """Whether ``value`` is approved -- the common yes/no check for gating."""
        return self.status_of(project_data, value) is ApprovalStatus.APPROVED
