"""Wat de opstart laat falen, zodat een gat niet pas bij een aanroep opvalt.

Drie dingen kunnen een rechtensysteem stilzwijgend uithollen: een route die geen action
noemt (die staat dan open voor alles wat de aanroeper al mocht), een action zonder regel
(die faalt pas bij de aanroep), en een CI-rol waarvan elke grant een mens vraagt (die
opent voor een pijplijn niets, terwijl hij ervoor bedoeld is). Alle drie stoppen hier de
start.
"""

from __future__ import annotations

from typing import Any, ClassVar
from unittest.mock import patch

import pytest
from opi.api.endpoint_util import REQUIRES_PROJECT_NAME, controleer_handelingen
from opi.core.authz.actions import ACTION_ATTR, Action
from opi.core.authz.engine import ANY, Audience, Grant, Requirement, Role, Rule, RuleError, all_rules, rule
from opi.core.authz.rules import check_register
from opi.server import app


class _Route:
    """Een geregistreerde route, zoals de controle hem leest."""

    methods: ClassVar[set[str]] = {"POST"}

    def __init__(self, endpoint: Any, path: str = "/api/v2/projects/{project_name}/iets") -> None:
        self.endpoint = endpoint
        self.path = path


async def _nieuwe_route(project_name: str) -> None: ...


def test_de_echte_routetabel_komt_door_de_controle() -> None:
    """Elke bewaakte route van deze applicatie noemt een action met een regel."""
    controleer_handelingen(app.routes)


def test_een_route_zonder_action_stopt_de_start() -> None:
    setattr(_nieuwe_route, REQUIRES_PROJECT_NAME, True)
    try:
        with pytest.raises(RuntimeError, match="noemen geen action"):
            controleer_handelingen([_Route(_nieuwe_route)])
    finally:
        delattr(_nieuwe_route, REQUIRES_PROJECT_NAME)


def test_een_action_zonder_regel_stopt_de_start() -> None:
    """Een action die niemand in het register noemt is een gat, geen weigering."""

    async def route(project_name: str) -> None: ...

    setattr(route, REQUIRES_PROJECT_NAME, True)
    setattr(route, ACTION_ATTR, Action.PROJECT_READ)

    with (
        patch("opi.api.endpoint_util.rule_for", side_effect=RuleError("geen regel")),
        pytest.raises(RuntimeError, match="zonder regel"),
    ):
        controleer_handelingen([_Route(route)])


def test_het_echte_register_komt_door_de_controle() -> None:
    check_register()


def test_een_rol_zonder_enkele_grant_stopt_de_start() -> None:
    """Een rol die nergens voorkomt opent niets, en niemand zou dat merken."""
    zonder_ops_ci = tuple(
        regel for regel in all_rules() if all(grant.role is not Role.OPS_CI for grant in regel.grants)
    )
    with (
        patch("opi.core.authz.rules.all_rules", return_value=zonder_ops_ci),
        pytest.raises(RuleError, match="ops-ci"),
    ):
        check_register()


def test_een_ci_rol_die_alleen_een_mens_toelaat_stopt_de_start() -> None:
    """Een pijplijn kan geen tweede factor geven, dus zo'n CI-rol opent niets."""
    aangepast = tuple(
        Rule(
            action=regel.action,
            label=regel.label,
            section=regel.section,
            read=regel.read,
            audience=regel.audience,
            grants=tuple(
                Grant(grant.role, grant.scope, requires=frozenset({Requirement.HUMAN_ONLY}))
                if grant.role is Role.OPS_CI
                else grant
                for grant in regel.grants
            ),
        )
        for regel in all_rules()
    )
    with (
        patch("opi.core.authz.rules.all_rules", return_value=aangepast),
        pytest.raises(RuleError, match="openen dus niets"),
    ):
        check_register()


def test_een_grant_op_een_regel_zonder_projectrecht_faalt() -> None:
    """Een portaal- of platformregel heeft geen projectrol die hem opent."""
    with pytest.raises(RuleError, match="geen projectrecht"):
        rule(
            Action.PORTAL_READ,
            label="Iets",
            section="Het portaal",
            audience=Audience.SIGNED_IN,
            grants=[Grant(Role.VIEWER, ANY)],
        )
