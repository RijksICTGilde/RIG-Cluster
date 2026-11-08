"""The role matrix, read out of the rule register.

Nothing is executed or decided here: the matrix is another view of
:mod:`opi.core.authz.rules`, so the page and the code can never drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass

from opi.core.authz.engine import (
    ANY,
    ROLE_DESCRIPTIONS,
    Audience,
    Grant,
    Role,
    Rule,
    Scope,
    all_rules,
    effective_roles,
    role_label,
)

#: The order of the columns, as section 4 of the design lists them.
COLUMNS: tuple[Role, ...] = (
    Role.VIEWER,
    Role.DEVELOPER,
    Role.OPS,
    Role.ADMIN,
    Role.DEVELOPER_CI,
    Role.OPS_CI,
)


@dataclass(frozen=True)
class Cell:
    """What a role may do on this row."""

    allowed: bool
    step_up: bool = False

    @property
    def mark(self) -> str:
        """What the icon in the table says, for a screen reader."""
        if not self.allowed:
            return ""
        return "mag, met 2-factor" if self.step_up else "mag"


@dataclass(frozen=True)
class Row:
    """One rule out of the register, for one scope."""

    label: str
    section: str
    scope_label: str
    action: str
    cells: tuple[Cell, ...]


@dataclass(frozen=True)
class RoleColumn:
    """One column of the matrix, with what a person needs to know about it."""

    role: Role
    label: str
    description: str
    service_account_reachable: bool


def _cell(rule: Rule, scope: Scope, role: Role) -> Cell:
    """What *role* may do on the row (rule, scope).

    A grant covers the row when its scope asks no more than the row's, and when the grant's
    role sits in the covering closure of *role*.
    """
    covering: list[Grant] = [
        grant for grant in rule.grants if grant.scope.covers(scope) and grant.role in effective_roles((role,))
    ]
    if not covering:
        return Cell(allowed=False)
    return Cell(allowed=True, step_up=all(grant.needs_step_up for grant in covering))


def rows() -> tuple[Row, ...]:
    """Every PROJECT rule, with one row per scope it names.

    What does not work on a project (the portal pages, the platform administration) has no
    role to weigh and sits in :func:`other_rules`.
    """
    return tuple(
        Row(
            label=rule.label,
            section=rule.section,
            scope_label="" if scope is ANY else scope.label,
            action=rule.action.value,
            cells=tuple(_cell(rule, scope, role) for role in COLUMNS),
        )
        for rule in all_rules()
        if rule.audience is Audience.PROJECT
        for scope in rule.row_scopes()
    )


def other_rules() -> tuple[tuple[str, tuple[Rule, ...]], ...]:
    """The rules that are not a project permission, per audience.

    They are on the page because their absence would read as "those routes are free".
    """
    per_audience = {
        "Elke ingelogde gebruiker": tuple(r for r in all_rules() if r.audience is Audience.SIGNED_IN),
        "Alleen een platformbeheerder": tuple(r for r in all_rules() if r.audience is Audience.PLATFORM_ADMIN),
    }
    return tuple((name, rules) for name, rules in per_audience.items() if rules)


def columns() -> tuple[RoleColumn, ...]:
    """The columns, with per role whether a service account can carry it.

    A service account can never give a step-up, so a role of which EVERY grant asks for a
    human is unreachable for it.
    """
    reachable = {
        role: any(
            not grant.needs_human
            for rule in all_rules()
            for grant in rule.grants
            if grant.role in effective_roles((role,))
        )
        for role in COLUMNS
    }
    return tuple(
        RoleColumn(
            role=role,
            label=role_label(role),
            description=ROLE_DESCRIPTIONS[role],
            service_account_reachable=reachable[role],
        )
        for role in COLUMNS
    )


def sections() -> tuple[tuple[str, tuple[Row, ...]], ...]:
    """The rows grouped per section, in the order of the register."""
    order: list[str] = []
    per_section: dict[str, list[Row]] = {}
    for row in rows():
        if row.section not in per_section:
            order.append(row.section)
            per_section[row.section] = []
        per_section[row.section].append(row)
    return tuple((name, tuple(per_section[name])) for name in order)
