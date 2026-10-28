"""De rolmatrix, gelezen uit het regelregister.

Niets wordt hier uitgevoerd of beslist: de matrix is een andere weergave van
:mod:`opi.core.authz.rules`, zodat de pagina en de code nooit uit elkaar kunnen lopen.
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

#: De volgorde van de kolommen, zoals ontwerp paragraaf 4 ze opsomt.
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
    """Wat een rol op deze rij mag."""

    allowed: bool
    step_up: bool = False

    @property
    def mark(self) -> str:
        """De markering in de tabel: leeg, ``v`` of ``v s``."""
        if not self.allowed:
            return ""
        return "v s" if self.step_up else "v"


@dataclass(frozen=True)
class Row:
    """Eén regel uit het register, voor één scope."""

    label: str
    section: str
    scope_label: str
    action: str
    cells: tuple[Cell, ...]


@dataclass(frozen=True)
class RoleColumn:
    """Een kolom van de matrix, met wat een mens erover moet weten."""

    role: Role
    label: str
    description: str
    service_account_reachable: bool


def _cell(rule: Rule, scope: Scope, role: Role) -> Cell:
    """Wat *role* op de rij (regel, scope) mag.

    Een grant dekt de rij als zijn scope niet meer vraagt dan die van de rij, en als de
    rol van de grant in de omvat-sluiting van *role* zit.
    """
    dekkend: list[Grant] = [
        grant for grant in rule.grants if grant.scope.covers(scope) and grant.role in effective_roles((role,))
    ]
    if not dekkend:
        return Cell(allowed=False)
    return Cell(allowed=True, step_up=all(grant.needs_step_up for grant in dekkend))


def rows() -> tuple[Row, ...]:
    """Elke PROJECTregel, met een rij per scope die hij noemt.

    Wat niet op een project werkt (de portaalpagina's, het platformbeheer) heeft geen
    rol om te wegen en staat in :func:`other_rules`.
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
    """De regels die geen projectrecht zijn, per publiek.

    Ze staan op de pagina omdat hun afwezigheid zou lezen als "die routes zijn vrij".
    """
    per_publiek = {
        "Elke ingelogde gebruiker": tuple(r for r in all_rules() if r.audience is Audience.SIGNED_IN),
        "Alleen een platformbeheerder": tuple(r for r in all_rules() if r.audience is Audience.PLATFORM_ADMIN),
    }
    return tuple((naam, regels) for naam, regels in per_publiek.items() if regels)


def columns() -> tuple[RoleColumn, ...]:
    """De kolommen, met per rol of een service account hem kan dragen.

    Een service account kan nooit een step-up geven, dus een rol waarvan ELKE grant een
    mens vraagt is voor hem onbereikbaar.
    """
    bereikbaar = {
        rol: any(
            not grant.needs_human
            for rule in all_rules()
            for grant in rule.grants
            if grant.role in effective_roles((rol,))
        )
        for rol in COLUMNS
    }
    return tuple(
        RoleColumn(
            role=rol,
            label=role_label(rol),
            description=ROLE_DESCRIPTIONS[rol],
            service_account_reachable=bereikbaar[rol],
        )
        for rol in COLUMNS
    )


def sections() -> tuple[tuple[str, tuple[Row, ...]], ...]:
    """De rijen gegroepeerd per sectie, in de volgorde van het register."""
    volgorde: list[str] = []
    per_sectie: dict[str, list[Row]] = {}
    for row in rows():
        if row.section not in per_sectie:
            volgorde.append(row.section)
            per_sectie[row.section] = []
        per_sectie[row.section].append(row)
    return tuple((naam, tuple(per_sectie[naam])) for naam in volgorde)
