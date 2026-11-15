"""The vocabulary of the authorization system, and the decision function.

The roles, the scopes and the shape of a rule live here; the register itself is in
:mod:`opi.core.authz.rules` and the matrix that follows from it in
:mod:`opi.core.authz.matrix`. One function decides, for the API and the portal:
:func:`decide`.

The principal belongs here and not in the API layer, because the portal builds one too and
``opi.core`` may not lean on ``opi.api``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable  # noqa: TC003  # dataclass fields, so needed at runtime
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from opi.core.actor import ACTOR_UNKNOWN, Actor

if TYPE_CHECKING:
    from starlette.requests import Request

    from opi.core.authz.actions import Action


class Role(StrEnum):
    """A role on ONE project. The value is what the project file holds (B13)."""

    VIEWER = "viewer"
    DEVELOPER = "developer"
    OPS = "ops"
    ADMIN = "admin"
    DEVELOPER_CI = "developer-ci"
    OPS_CI = "ops-ci"


#: What a person sees on the screen. Renaming one is a change here and not a migration of
#: project files (B13).
ROLE_LABELS: dict[Role, str] = {
    Role.VIEWER: "Viewer",
    Role.DEVELOPER: "Ontwikkelaar",
    Role.OPS: "Ops",
    Role.ADMIN: "Admin",
    Role.DEVELOPER_CI: "Developer CI",
    Role.OPS_CI: "Ops CI",
}

#: One sentence per role, for the Team card and the matrix page.
ROLE_DESCRIPTIONS: dict[Role, str] = {
    Role.VIEWER: "Leest het project mee, behalve de waarden van omgevingsvariabelen en secrets.",
    Role.DEVELOPER: "Bouwt aan alles wat niet op productie staat: deployments, componenten, services en env-vars.",
    Role.OPS: "Doet hetzelfde als een ontwikkelaar, ook op productie, en zet de OTAP-markering.",
    Role.ADMIN: "Beheert leden, rollen, tokens en service accounts, en kan het project verwijderen.",
    Role.DEVELOPER_CI: "Werkt vanuit een CI/CD pipeline deployments bij die niet op productie staan en mag nieuwe (niet productie) deployments aanmaken.",
    Role.OPS_CI: "Vervangt vanuit een CI/CD pipeline enkel het image van elke deployment, ook op productie.",
}


def role_label(role: Role | str) -> str:
    """The label of a role. A stored key without a label shows itself."""
    try:
        return ROLE_LABELS[Role(role)]
    except ValueError:
        return str(role)


#: Which role covers which other one, all in one place. Worked out transitively by
#: :func:`effective_roles`.
INCLUDES: dict[Role, frozenset[Role]] = {
    Role.DEVELOPER: frozenset({Role.VIEWER}),
    Role.OPS: frozenset({Role.DEVELOPER}),
    Role.ADMIN: frozenset({Role.VIEWER}),
    Role.DEVELOPER_CI: frozenset({Role.VIEWER}),
}


def effective_roles(roles: Iterable[Role]) -> frozenset[Role]:
    """The transitive closure of *roles* over :data:`INCLUDES`."""
    found: set[Role] = set()
    todo = list(roles)
    while todo:
        role = todo.pop()
        if role in found:
            continue
        found.add(role)
        todo.extend(INCLUDES.get(role, frozenset()))
    return frozenset(found)


class PrincipalKind(StrEnum):
    """Which credential the caller used."""

    PROJECT_KEY = "project-key"
    HUMAN = "human"
    SERVICE_ACCOUNT = "service-account"


@dataclass(frozen=True)
class Principal:
    """A recognised caller, apart from how they legitimised themselves."""

    kind: PrincipalKind
    name: str
    project: str
    roles: frozenset[Role]
    actor: Actor = ACTOR_UNKNOWN

    @property
    def is_human(self) -> bool:
        return self.kind is PrincipalKind.HUMAN


@dataclass(frozen=True)
class Resource:
    """What the call works on, as far as a scope or the audit knows it."""

    project: str
    path: str
    deployment: str | None = None
    environment: str | None = None
    component: str | None = None
    component_in_production: bool = False
    project_has_production: bool = False


#: The marking called "productie". The same value as
#: :attr:`opi.services.project_environment.Omgeving.PRODUCTIE`, as text here because a scope
#: reads only the marking of the resource.
PRODUCTION_MARKING = "productie"


@dataclass(frozen=True)
class Scope:
    """A condition on the resource, and the row it gets in the matrix.

    ``parts`` decides whether a grant covers a row: a grant covers a row when its parts are
    a subset of the row's. ``ANY`` has none and therefore covers everything.
    """

    name: str
    label: str
    predicate: Callable[[Resource], bool]
    parts: frozenset[str] = field(default_factory=frozenset)

    def covers(self, other: Scope) -> bool:
        return self.parts <= other.parts


def _scope(name: str, label: str, predicate: Callable[[Resource], bool]) -> Scope:
    return Scope(name, label, predicate, frozenset({name}))


ANY = Scope("any", "", lambda _r: True, frozenset())
PRODUCTION = _scope("production", "productie", lambda r: r.environment == PRODUCTION_MARKING)
NON_PRODUCTION = _scope("non-production", "niet-productie", lambda r: r.environment != PRODUCTION_MARKING)
PROJECT_WITH_PRODUCTION = _scope("project-with-production", "project met productie", lambda r: r.project_has_production)
PROJECT_WITHOUT_PRODUCTION = _scope(
    "project-without-production", "project zonder productie", lambda r: not r.project_has_production
)
COMPONENT_IN_PRODUCTION = _scope("component-in-production", "in productie", lambda r: r.component_in_production)
COMPONENT_NOT_IN_PRODUCTION = _scope(
    "component-not-in-production", "niet in productie", lambda r: not r.component_in_production
)

#: Every scope a rule may name. A grant with a scope that is not in here makes :func:`rule`
#: fail, so the matrix cannot miss a row.
SCOPES: tuple[Scope, ...] = (
    ANY,
    NON_PRODUCTION,
    PRODUCTION,
    PROJECT_WITHOUT_PRODUCTION,
    PROJECT_WITH_PRODUCTION,
    COMPONENT_NOT_IN_PRODUCTION,
    COMPONENT_IN_PRODUCTION,
)


class Requirement(StrEnum):
    """An extra condition on a grant, on top of the role and the scope."""

    HUMAN_ONLY = "human-only"
    STEP_UP = "step-up"


STEP_UP = frozenset({Requirement.STEP_UP})
HUMAN_ONLY = frozenset({Requirement.HUMAN_ONLY})


@dataclass(frozen=True)
class Grant:
    """Who may do this action, where, and under which condition."""

    role: Role
    scope: Scope = ANY
    requires: frozenset[Requirement] = frozenset()

    def __str__(self) -> str:
        """The shape the audit records a grant in: ``ops@production``."""
        return f"{self.role.value}@{self.scope.name}"

    @property
    def needs_step_up(self) -> bool:
        return Requirement.STEP_UP in self.requires

    @property
    def needs_human(self) -> bool:
        return bool(self.requires & {Requirement.STEP_UP, Requirement.HUMAN_ONLY})


class Audience(StrEnum):
    """Who decides about this action.

    The role matrix is about :attr:`PROJECT`; the other two exist because not every route
    works on a project. A route without an action would be quietly unguarded, so the portal
    pages and the platform administration carry one as well.
    """

    #: The grants decide, with the roles of the member on this project.
    PROJECT = "project"
    #: Every signed-in user; there is no project to hold a role on.
    SIGNED_IN = "signed-in"
    #: A platform administrator only (``require_platform_admin``). No project role opens this.
    PLATFORM_ADMIN = "platform-admin"


@dataclass(frozen=True)
class Rule:
    """One action with everything that opens it. This is also the row in the matrix."""

    action: Action
    label: str
    section: str
    grants: tuple[Grant, ...]
    read: bool = False
    audience: Audience = Audience.PROJECT

    def row_scopes(self) -> tuple[Scope, ...]:
        """The scopes this rule gets a row on, in the order of SCOPES."""
        named = {grant.scope for grant in self.grants} or {ANY}
        return tuple(scope for scope in SCOPES if scope in named)


_RULES: dict[Action, Rule] = {}


class RuleError(Exception):
    """A rule that cannot exist as written. Fails while the register is loaded."""


def rule(
    action: Action,
    *,
    label: str,
    section: str,
    grants: Iterable[Grant] = (),
    read: bool = False,
    audience: Audience = Audience.PROJECT,
) -> Rule:
    """Register what *action* opens.

    Empty grants are allowed: the action then opens for nobody, and the matrix shows that row
    empty.

    Raises ``RuleError`` on a second rule for the same action, an empty label or section, or
    a grant with a scope outside :data:`SCOPES`.
    """
    # The shape first, then the duplicate: a rule with an empty label or a scope of its own
    # has to be told so, also when somebody declares it twice by accident.
    if not label.strip():
        raise RuleError(f"'{action.value}' heeft geen label, en kan dus geen rij in de matrix krijgen")
    if not section.strip():
        raise RuleError(f"'{action.value}' heeft geen sectie")
    recorded = tuple(grants)
    for grant in recorded:
        if grant.scope not in SCOPES:
            raise RuleError(f"'{action.value}' noemt scope '{grant.scope.name}', die niet in SCOPES staat")
    if audience is not Audience.PROJECT and recorded:
        raise RuleError(f"'{action.value}' is geen projectrecht, dus een projectrol kan hem niet openen")
    if action in _RULES:
        raise RuleError(f"'{action.value}' heeft al een regel")
    made = Rule(action=action, label=label, section=section, grants=recorded, read=read, audience=audience)
    _RULES[action] = made
    return made


def rule_for(action: Action) -> Rule:
    """The rule of *action*.

    Raises ``RuleError`` when the action has no rule. That is an error in the register, not
    a refusal of this call.
    """
    found = _RULES.get(action)
    if found is None:
        raise RuleError(f"'{action.value}' heeft geen regel in opi/core/authz/rules.py")
    return found


def all_rules() -> tuple[Rule, ...]:
    """The whole register, in the order it was declared in."""
    return tuple(_RULES.values())


class Cause(StrEnum):
    """Why a call was refused."""

    INSUFFICIENT_ROLE = "insufficient-role"
    PRINCIPAL_KIND_NOT_ALLOWED = "principal-kind-not-allowed"
    STEP_UP_REQUIRED = "step-up-required"


@dataclass(frozen=True)
class Decision:
    """The outcome, with the grant that carried it or the reason for the refusal."""

    allowed: bool
    cause: Cause | None = None
    grant: Grant | None = None
    #: Whether there was a grant that only lacked a fresh step-up. The enforcement point
    #: lets such a refusal through, because the step-up has an answer of its own with the
    #: dialog or the problem detail; a refusal on the role does not.
    step_up_would_help: bool = False

    @property
    def needs_step_up(self) -> bool:
        return self.cause is Cause.STEP_UP_REQUIRED

    @property
    def requires_step_up(self) -> bool:
        """Whether a fresh second factor belongs with THIS call, allowed or not.

        An allowed call carries the grant that opened it, and that one says so; a refusal
        says so with the reason. ``@requires_step_up`` reads this, so the matrix page and
        what is actually enforced have the same source.
        """
        if self.grant is not None:
            return self.grant.needs_step_up
        return self.needs_step_up or self.step_up_would_help


def decide(principal: Principal, action: Action, resource: Resource, *, step_up_fresh: bool) -> Decision:
    """Whether this caller may do this action on this resource.

    Raises ``RuleError`` when the action has no rule.
    """
    candidates = [
        grant
        for grant in rule_for(action).grants
        if grant.role in effective_roles(principal.roles) and grant.scope.predicate(resource)
    ]
    if not candidates:
        return Decision(allowed=False, cause=Cause.INSUFFICIENT_ROLE)

    for grant in candidates:
        if grant.needs_human and not principal.is_human:
            continue
        if grant.needs_step_up and not step_up_fresh:
            continue
        return Decision(allowed=True, grant=grant)

    step_up_helps = any(grant.needs_step_up for grant in candidates)
    if not principal.is_human:
        return Decision(allowed=False, cause=Cause.PRINCIPAL_KIND_NOT_ALLOWED, step_up_would_help=step_up_helps)
    return Decision(allowed=False, cause=Cause.STEP_UP_REQUIRED, step_up_would_help=True)


@dataclass(frozen=True)
class RecordedDecision:
    """A decision as the audit records it. A request can take more than one."""

    action: Action
    resource: Resource
    decision: Decision
    step_up: bool


def record_decision(
    request: Request, *, action: Action, resource: Resource, decision: Decision, step_up_fresh: bool
) -> None:
    """Put the four attributes of design 8.3 on the request, and add the decision to the list.

    On a refusal as well: that is precisely what the audit records. Here and not in the web
    or API layer, so there is only one place that writes them.

    The four separate attributes carry the LAST decision, because ``@requires_step_up`` reads
    them for THIS call. The list carries all of them: an upsert with ``cloneFrom`` weighs the
    target first and the source after, and the audit wants both rows.

    A decision already in the list for this request does not go in a second time: the audit
    records an action once. That leaves the two rows of ``cloneFrom`` standing, because
    target and source are different resources. A helper that decides twice is caught here
    and not by ``tests/test_een_beslissing_per_handeling.py``, which cannot look inside one.
    """
    request.state.authz_action = action
    request.state.authz_resource = resource
    request.state.authz_decision = decision
    request.state.authz_step_up = step_up_fresh
    earlier: list[RecordedDecision] = getattr(request.state, "authz_decisions", [])
    this_decision = RecordedDecision(action, resource, decision, step_up_fresh)
    request.state.authz_decisions = earlier if this_decision in earlier else [*earlier, this_decision]
