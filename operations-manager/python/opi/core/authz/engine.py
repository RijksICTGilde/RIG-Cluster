"""De woordenlijst van het rechtensysteem en de beslisfunctie.

Hier staan de rollen, de scopes en de vorm van een regel; het register zelf staat in
:mod:`opi.core.authz.rules` en de matrix die eruit volgt in
:mod:`opi.core.authz.matrix`. Eén functie beslist, voor API en portaal:
:func:`decide`.

De principal hoort hier en niet in de API-laag, want het portaal bouwt er ook een en
``opi.core`` mag niet op ``opi.api`` leunen.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable  # noqa: TC003 -- dataclass-velden op runtime
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from opi.core.actor import ACTOR_UNKNOWN, Actor

if TYPE_CHECKING:
    from opi.core.authz.actions import Action


class Role(StrEnum):
    """Een rol op EEN project. De waarde staat in het projectbestand (B13)."""

    VIEWER = "viewer"
    DEVELOPER = "developer"
    OPS = "ops"
    ADMIN = "admin"
    DEVELOPER_CI = "developer-ci"
    OPS_CI = "ops-ci"


#: Wat een mens op het scherm ziet. Hernoemen is een wijziging hier en geen migratie
#: van projectbestanden (B13).
ROLE_LABELS: dict[Role, str] = {
    Role.VIEWER: "Viewer",
    Role.DEVELOPER: "Ontwikkelaar",
    Role.OPS: "Ops",
    Role.ADMIN: "Admin",
    Role.DEVELOPER_CI: "Developer CI",
    Role.OPS_CI: "Ops CI",
}

#: Een zin per rol, voor de Team-kaart en de matrixpagina.
ROLE_DESCRIPTIONS: dict[Role, str] = {
    Role.VIEWER: "Leest het project mee, behalve de waarden van omgevingsvariabelen en secrets.",
    Role.DEVELOPER: "Bouwt aan alles wat niet op productie staat: deployments, componenten, services en env-vars.",
    Role.OPS: "Doet hetzelfde als een ontwikkelaar, ook op productie, en zet de OTAP-markering.",
    Role.ADMIN: "Beheert leden, rollen, tokens en service accounts, en kan het project verwijderen.",
    Role.DEVELOPER_CI: "Voor een pijplijn: deployments die niet op productie staan, en het image daarvan.",
    Role.OPS_CI: "Voor een pijplijn: het image vervangen op elke deployment, ook op productie.",
}


def role_label(role: Role | str) -> str:
    """Het label van een rol. Een opgeslagen sleutel zonder label toont zichzelf."""
    try:
        return ROLE_LABELS[Role(role)]
    except ValueError:
        return str(role)


#: Welke rol welke andere omvat, op dezelfde plek. Transitief uitgerekend door
#: :func:`effective_roles`.
INCLUDES: dict[Role, frozenset[Role]] = {
    Role.DEVELOPER: frozenset({Role.VIEWER}),
    Role.OPS: frozenset({Role.DEVELOPER}),
    Role.ADMIN: frozenset({Role.VIEWER}),
}


def effective_roles(roles: Iterable[Role]) -> frozenset[Role]:
    """De transitieve sluiting van *roles* over :data:`INCLUDES`."""
    gevonden: set[Role] = set()
    te_doen = list(roles)
    while te_doen:
        rol = te_doen.pop()
        if rol in gevonden:
            continue
        gevonden.add(rol)
        te_doen.extend(INCLUDES.get(rol, frozenset()))
    return frozenset(gevonden)


class PrincipalKind(StrEnum):
    """Welk bewijsmiddel de aanroeper gebruikte."""

    PROJECT_KEY = "project-key"
    HUMAN = "human"
    SERVICE_ACCOUNT = "service-account"


@dataclass(frozen=True)
class Principal:
    """Een herkende aanroeper, los van hoe hij zich legitimeerde."""

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
    """Waarop de aanroep werkt, voor zover een scope of de audit het weet."""

    project: str
    path: str
    deployment: str | None = None
    environment: str | None = None
    component: str | None = None
    component_in_production: bool = False
    project_has_production: bool = False


#: De markering die "productie" heet. Dezelfde waarde als
#: :attr:`opi.core.project_environment.Omgeving.PRODUCTIE`, hier als tekst omdat een
#: scope alleen de markering van de resource leest.
PRODUCTION_MARKING = "productie"


@dataclass(frozen=True)
class Scope:
    """Een voorwaarde op de resource, en de rij die hij in de matrix krijgt.

    ``parts`` bepaalt of een grant een rij dekt: een grant dekt een rij als zijn parts een
    deelverzameling zijn van die van de rij. ``ANY`` heeft er geen en dekt dus alles.
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

#: Elke scope die een regel mag noemen. Een grant met een scope die hier niet in staat
#: laat :func:`rule` falen, zodat de matrix geen rij kan missen.
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
    """Een extra voorwaarde op een grant, boven de rol en de scope."""

    HUMAN_ONLY = "human-only"
    STEP_UP = "step-up"


STEP_UP = frozenset({Requirement.STEP_UP})


@dataclass(frozen=True)
class Grant:
    """Wie deze action mag, waar, en onder welke voorwaarde."""

    role: Role
    scope: Scope = ANY
    requires: frozenset[Requirement] = frozenset()

    def __str__(self) -> str:
        """De vorm waarin de audit een grant vastlegt: ``ops@production``."""
        return f"{self.role.value}@{self.scope.name}"

    @property
    def needs_step_up(self) -> bool:
        return Requirement.STEP_UP in self.requires

    @property
    def needs_human(self) -> bool:
        return bool(self.requires & {Requirement.STEP_UP, Requirement.HUMAN_ONLY})


class Audience(StrEnum):
    """Wie over deze action beslist.

    De rolmatrix gaat over :attr:`PROJECT`; de andere twee bestaan omdat niet elke route
    op een project werkt. Een route zonder action zou stil onbewaakt zijn, dus ook de
    portaalpagina's en het platformbeheer dragen er een.
    """

    #: De grants beslissen, met de rollen van het lid op dit project.
    PROJECT = "project"
    #: Elke ingelogde gebruiker; er is geen project om een rol op te hebben.
    SIGNED_IN = "signed-in"
    #: Alleen een platformbeheerder (``require_platform_admin``). Geen projectrol opent dit.
    PLATFORM_ADMIN = "platform-admin"


@dataclass(frozen=True)
class Rule:
    """Eén action met alles wat haar opent. Dit is ook de rij in de matrix."""

    action: Action
    label: str
    section: str
    grants: tuple[Grant, ...]
    read: bool = False
    audience: Audience = Audience.PROJECT

    def row_scopes(self) -> tuple[Scope, ...]:
        """De scopes waarop deze regel een rij krijgt, in de volgorde van SCOPES."""
        genoemd = {grant.scope for grant in self.grants} or {ANY}
        return tuple(scope for scope in SCOPES if scope in genoemd)


_RULES: dict[Action, Rule] = {}


class RuleError(Exception):
    """Een regel die zo niet kan bestaan. Faalt bij het laden van het register."""


def rule(
    action: Action,
    *,
    label: str,
    section: str,
    grants: Iterable[Grant] = (),
    read: bool = False,
    audience: Audience = Audience.PROJECT,
) -> Rule:
    """Registreer wat *action* opent.

    Lege grants mag: dan opent de action voor niemand, en de matrix toont die rij leeg.

    Raises:
        RuleError: Bij een tweede regel voor dezelfde action, een leeg label of een lege
            sectie, of een grant met een scope buiten :data:`SCOPES`.
    """
    # De vorm eerst, dan de dubbeling: een regel met een leeg label of een eigen scope
    # moet dat te horen krijgen, ook als iemand hem per ongeluk twee keer declareert.
    if not label.strip():
        raise RuleError(f"'{action.value}' heeft geen label, en kan dus geen rij in de matrix krijgen")
    if not section.strip():
        raise RuleError(f"'{action.value}' heeft geen sectie")
    vastgelegd = tuple(grants)
    for grant in vastgelegd:
        if grant.scope not in SCOPES:
            raise RuleError(f"'{action.value}' noemt scope '{grant.scope.name}', die niet in SCOPES staat")
    if audience is not Audience.PROJECT and vastgelegd:
        raise RuleError(f"'{action.value}' is geen projectrecht, dus een projectrol kan hem niet openen")
    if action in _RULES:
        raise RuleError(f"'{action.value}' heeft al een regel")
    gemaakt = Rule(action=action, label=label, section=section, grants=vastgelegd, read=read, audience=audience)
    _RULES[action] = gemaakt
    return gemaakt


def rule_for(action: Action) -> Rule:
    """De regel van *action*.

    Raises:
        RuleError: Als de action geen regel heeft. Dat is een fout in het register, niet
            een weigering van deze aanroep.
    """
    gevonden = _RULES.get(action)
    if gevonden is None:
        raise RuleError(f"'{action.value}' heeft geen regel in opi/core/authz/rules.py")
    return gevonden


def all_rules() -> tuple[Rule, ...]:
    """Het hele register, in de volgorde waarin het is gedeclareerd."""
    return tuple(_RULES.values())


class Cause(StrEnum):
    """Waarom een aanroep werd geweigerd."""

    INSUFFICIENT_ROLE = "insufficient-role"
    PRINCIPAL_KIND_NOT_ALLOWED = "principal-kind-not-allowed"
    STEP_UP_REQUIRED = "step-up-required"


@dataclass(frozen=True)
class Decision:
    """De uitkomst, met de grant die hem droeg of de reden van de weigering."""

    allowed: bool
    cause: Cause | None = None
    grant: Grant | None = None
    #: Of er een grant was die alleen nog een verse step-up miste. Het handhavingspunt
    #: laat zo'n weigering door, want de step-up heeft een eigen antwoord met de dialoog
    #: of het problem-detail; een weigering op de rol heeft dat niet.
    step_up_would_help: bool = False

    @property
    def needs_step_up(self) -> bool:
        return self.cause is Cause.STEP_UP_REQUIRED

    @property
    def requires_step_up(self) -> bool:
        """Of een verse tweede factor bij DEZE aanroep hoort, toegestaan of niet.

        Een toegestane aanroep draagt de grant die hem opende, en die zegt het; een
        weigering zegt het met de reden. ``@requires_step_up`` leest dit, zodat de
        matrixpagina en wat er wordt afgedwongen dezelfde bron hebben.
        """
        if self.grant is not None:
            return self.grant.needs_step_up
        return self.needs_step_up or self.step_up_would_help


def decide(principal: Principal, action: Action, resource: Resource, *, step_up_fresh: bool) -> Decision:
    """Mag deze aanroeper deze action op deze resource.

    Raises:
        RuleError: Als de action geen regel heeft.
    """
    kandidaten = [
        grant
        for grant in rule_for(action).grants
        if grant.role in effective_roles(principal.roles) and grant.scope.predicate(resource)
    ]
    if not kandidaten:
        return Decision(allowed=False, cause=Cause.INSUFFICIENT_ROLE)

    for grant in kandidaten:
        if grant.needs_human and not principal.is_human:
            continue
        if grant.needs_step_up and not step_up_fresh:
            continue
        return Decision(allowed=True, grant=grant)

    step_up_helpt = any(grant.needs_step_up for grant in kandidaten)
    if not principal.is_human:
        return Decision(allowed=False, cause=Cause.PRINCIPAL_KIND_NOT_ALLOWED, step_up_would_help=step_up_helpt)
    return Decision(allowed=False, cause=Cause.STEP_UP_REQUIRED, step_up_would_help=True)


def record_decision(
    request: Any, *, action: Action, resource: Resource, decision: Decision, step_up_fresh: bool
) -> None:
    """Zet de vier attributen uit ontwerp 8.3 op het verzoek.

    Ook bij een weigering: de audit van taak 3 legt juist die vast. Hier en niet in de
    web- of API-laag, zodat er maar een plek is die ze schrijft.
    """
    request.state.authz_action = action
    request.state.authz_resource = resource
    request.state.authz_decision = decision
    request.state.authz_step_up = step_up_fresh
