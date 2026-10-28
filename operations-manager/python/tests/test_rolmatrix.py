"""De rolmatrix toont het register, en een onvolledige regel laat de opstart falen."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from opi.core.authz.actions import Action
from opi.core.authz.engine import (
    ANY,
    PRODUCTION,
    STEP_UP,
    Grant,
    Role,
    RuleError,
    Scope,
    all_rules,
    rule,
)
from opi.core.authz.matrix import COLUMNS, Cell, columns, other_rules, rows, sections
from opi.web.router_rolmatrix import rolmatrix


def test_elke_regel_staat_op_de_pagina() -> None:
    """Geen regel uit het register valt van de pagina af.

    Een projectrecht krijgt een rij in de matrix; wat geen projectrecht is staat in de
    eigen lijst eronder. Alles staat ergens, want een ontbrekende regel zou lezen als
    "die route is vrij".
    """
    op_de_pagina = {rij.action for rij in rows()}
    op_de_pagina |= {regel.action.value for _naam, regels in other_rules() for regel in regels}
    assert op_de_pagina == {regel.action.value for regel in all_rules()}


def test_een_regel_met_scopes_krijgt_een_rij_per_scope() -> None:
    """``deployment.delete`` heeft productie en niet-productie, dus twee rijen."""
    rijen = [rij for rij in rows() if rij.action == "deployment.delete"]
    assert sorted(rij.scope_label for rij in rijen) == ["niet-productie", "productie"]


def test_een_step_up_cel_is_gemarkeerd() -> None:
    """Een grant met een step-up toont ``v s``, een zonder ``v``."""
    per_scope = {rij.scope_label: rij for rij in rows() if rij.action == "deployment.delete"}
    ops = COLUMNS.index(Role.OPS)
    assert per_scope["productie"].cells[ops].mark == "v s"
    assert per_scope["niet-productie"].cells[ops].mark == "v"


def test_de_kolommen_dragen_het_label_en_de_bereikbaarheid() -> None:
    """De kop is het label (B13), en een rol die alleen een mens kan dragen zegt dat."""
    per_rol = {kolom.role: kolom for kolom in columns()}
    assert per_rol[Role.DEVELOPER].label == "Ontwikkelaar"
    assert all(kolom.description for kolom in columns())
    assert per_rol[Role.VIEWER].service_account_reachable is True


def test_elke_sectie_heeft_rijen() -> None:
    assert all(rijen for _naam, rijen in sections())


def test_een_regel_zonder_label_faalt() -> None:
    """Zonder label kan een recht geen rij in de matrix krijgen, dus bestaat het niet."""
    with pytest.raises(RuleError, match="geen label"):
        rule(Action.PROJECT_READ, label="  ", section="Project", grants=[Grant(Role.VIEWER)])


def test_een_tweede_regel_voor_dezelfde_action_faalt() -> None:
    with pytest.raises(RuleError, match="heeft al een regel"):
        rule(Action.PROJECT_READ, label="Nog een keer", section="Project", grants=[Grant(Role.VIEWER)])


def test_een_scope_buiten_scopes_faalt() -> None:
    """Een eigen scope zou een rij in de matrix zijn die niemand kan lezen."""
    verzonnen = Scope("verzonnen", "verzonnen", lambda _r: True, frozenset({"verzonnen"}))
    with pytest.raises(RuleError, match="niet in SCOPES"):
        rule(
            Action.PROJECT_LIST,
            label="Iets",
            section="Project",
            grants=[Grant(Role.VIEWER, verzonnen)],
        )


def test_de_markering_van_een_cel() -> None:
    """De drie vormen die een cel kan hebben."""
    assert Cell(allowed=False).mark == ""
    assert Cell(allowed=True).mark == "v"
    assert Cell(allowed=True, step_up=True).mark == "v s"


def test_een_grant_leest_als_rol_en_scope() -> None:
    """De vorm waarin de audit een grant vastlegt (ontwerp 8.2)."""
    assert str(Grant(Role.OPS, PRODUCTION, requires=STEP_UP)) == "ops@production"
    assert str(Grant(Role.ADMIN, ANY)) == "admin@any"


# --- de pagina --------------------------------------------------------------


def _verzoek(email: str | None) -> Any:
    user = {"email": email} if email else None
    return SimpleNamespace(state=SimpleNamespace(user=user), query_params={})


def _gebruikersdienst(admins: set[str]) -> Any:
    dienst = MagicMock()
    dienst.is_platform_admin.side_effect = lambda email: email in admins
    return dienst


@pytest.mark.asyncio
async def test_een_niet_beheerder_komt_niet_op_de_matrixpagina() -> None:
    """De matrix gaat over het hele platform, dus de URL is de grens, niet het menu."""
    with (
        patch("opi.services.user_service.get_user_service", return_value=_gebruikersdienst(set())),
        pytest.raises(HTTPException) as fout,
    ):
        await rolmatrix(_verzoek("ontwikkelaar@rijksoverheid.nl"))

    assert fout.value.status_code == 403


@pytest.mark.asyncio
async def test_de_pagina_krijgt_de_kolommen_en_de_secties() -> None:
    """De route geeft het register door; de opmaak zelf dekt de browsertest."""
    with (
        patch(
            "opi.services.user_service.get_user_service",
            return_value=_gebruikersdienst({"beheerder@rijksoverheid.nl"}),
        ),
        patch("opi.web.router_rolmatrix.render", return_value="gerenderd") as render,
        patch("opi.web.router_rolmatrix.get_menu_items", return_value=[]),
        patch("opi.web.router_rolmatrix.build_lotc_admin", return_value={}),
    ):
        antwoord = await rolmatrix(_verzoek("beheerder@rijksoverheid.nl"))

    assert antwoord == "gerenderd"
    context = render.call_args.kwargs["context"]
    assert len(context["kolommen"]) == len(COLUMNS)
    assert context["secties"]
