"""Het audittabblad: achter ``Action.AUDIT_READ``, en per project.

Drie dingen staan hier vast. De route is de ENIGE guard op ``list_actions``, die zelf
niets controleert: het project komt uit het PAD, dezelfde naam waarover ``decide()``
besliste, nooit een naam uit de querystring. ``limit`` heeft een plafond. En de pagina
toont ``origin`` niet: die kolom is voor platformbeheerders, in de database.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from opi.core.authz.actions import Action
from opi.core.leden import LidSoort
from opi.services.audit import LIST_DEFAULT_LIMIT, LIST_MAX_LIMIT
from opi.services.project_service import ProjectSummary, ProjectUser
from opi.web.authz import enforce_route_action, may
from opi.web.lotc_switch import PROJECT_TABS, audit_limit, build_lotc_audit, project_tab_url, tab_from_path
from opi.web.router import project_audit

BEHEERDER = "beheerder@rijksoverheid.nl"
BOUWER = "bouwer@rijksoverheid.nl"

WORTEL = Path(__file__).resolve().parent.parent
TABS = WORTEL / "opi" / "templates_lotc" / "bg" / "project-tabs.html.j2"


def _project(naam: str = "demo") -> ProjectSummary:
    return ProjectSummary(
        name=naam,
        filename=f"{naam}.yaml",
        data={"name": naam},
        users=[
            ProjectUser(email=BEHEERDER, roles=["admin"], type=LidSoort.PERSOON),
            ProjectUser(email=BOUWER, roles=["developer"], type=LidSoort.PERSOON),
        ],
    )


def _request(email: str, project_name: str, query: dict[str, str] | None = None) -> Any:
    return SimpleNamespace(
        state=SimpleNamespace(user={"email": email}),
        session={},
        headers={},
        method="GET",
        url=SimpleNamespace(path=f"/projects/{project_name}/audit"),
        scope={"route": SimpleNamespace(endpoint=project_audit)},
        path_params={"project_name": project_name},
        query_params=query or {},
    )


async def _poort(email: str, project_name: str, bestaand: str = "demo", query: dict[str, str] | None = None) -> None:
    """De routepoort over deze aanroep, met EEN project in de store."""
    store = MagicMock()
    store.get.side_effect = lambda naam: _project(naam) if naam == bestaand else None
    gebruikers = MagicMock()
    gebruikers.is_platform_admin.return_value = False
    with (
        patch("opi.web.authz.get_project_store", return_value=store),
        patch("opi.web.authz.get_user_service", return_value=gebruikers),
    ):
        await enforce_route_action(_request(email, project_name, query))


# --- het tabblad en zijn route ----------------------------------------------


def test_audit_is_een_tabblad_met_een_eigen_pad() -> None:
    assert PROJECT_TABS["audit"] == {"label": "Audit", "path": "audit"}
    assert project_tab_url("demo", "audit") == "/projects/demo/audit"
    assert tab_from_path("/projects/demo/audit") == "audit"


def test_de_route_draagt_de_auditaction() -> None:
    from opi.core.authz.actions import action_of

    assert action_of(project_audit) is Action.AUDIT_READ


# --- wie mag ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_een_admin_mag_de_audit_van_zijn_eigen_project_lezen() -> None:
    await _poort(BEHEERDER, "demo")


@pytest.mark.asyncio
async def test_een_developer_krijgt_403_en_ziet_het_tabblad_niet() -> None:
    with pytest.raises(HTTPException) as fout:
        await _poort(BOUWER, "demo")
    assert fout.value.status_code == 403

    store = MagicMock()
    store.get.return_value = _project()
    gebruikers = MagicMock()
    gebruikers.is_platform_admin.return_value = False
    with (
        patch("opi.web.authz.get_project_store", return_value=store),
        patch("opi.web.authz.get_user_service", return_value=gebruikers),
    ):
        assert may(_request(BOUWER, "demo"), action=Action.AUDIT_READ, project_name="demo") is False
        assert may(_request(BEHEERDER, "demo"), action=Action.AUDIT_READ, project_name="demo") is True


@pytest.mark.asyncio
async def test_een_admin_van_project_a_krijgt_403_op_project_b() -> None:
    """Ook met ``?project=demo`` erbij: de poort en de lezing gaan over het PAD."""
    store = MagicMock()
    store.get.side_effect = lambda naam: _project(naam)
    gebruikers = MagicMock()
    gebruikers.is_platform_admin.return_value = False

    # Op project 'ander' is deze aanroeper geen lid, dus geen enkele rol.
    def _ander(naam: str) -> ProjectSummary:
        project = _project(naam)
        project.users = [ProjectUser(email="iemand@rijksoverheid.nl", roles=["admin"], type=LidSoort.PERSOON)]
        return project

    store.get.side_effect = _ander
    with (
        patch("opi.web.authz.get_project_store", return_value=store),
        patch("opi.web.authz.get_user_service", return_value=gebruikers),
        pytest.raises(HTTPException) as fout,
    ):
        await enforce_route_action(_request(BEHEERDER, "ander", {"project": "demo"}))
    assert fout.value.status_code == 403


# --- het plafond op limit ---------------------------------------------------


@pytest.mark.parametrize(
    ("gevraagd", "verwacht"),
    [
        (None, LIST_DEFAULT_LIMIT),
        ("", LIST_DEFAULT_LIMIT),
        ("rommel", LIST_DEFAULT_LIMIT),
        ("10", 10),
        ("1000000", LIST_MAX_LIMIT),
        ("0", 1),
        ("-5", 1),
    ],
)
def test_limit_heeft_een_plafond_en_geeft_geen_fout(gevraagd: str | None, verwacht: int) -> None:
    query = {} if gevraagd is None else {"limit": gevraagd}
    assert audit_limit(_request(BEHEERDER, "demo", query)) == verwacht


# --- wat de pagina toont ----------------------------------------------------


def _rij(**extra: Any) -> Any:
    gegevens = {
        "action": "deployment.write",
        "allowed": True,
        "cause": None,
        "commit_shas": ["abc1234def"],
        **extra.pop("data", {}),
    }
    return SimpleNamespace(
        time=datetime(2026, 10, 28, 12, 30, tzinfo=UTC),
        actor=BEHEERDER,
        actor_kind="user",
        subject="demo/live",
        origin="10.0.0.7",
        data=gegevens,
        **extra,
    )


def test_de_rijen_dragen_wat_de_tabel_toont_en_geen_oorsprong() -> None:
    [rij] = build_lotc_audit([_rij()])
    assert rij["actor"] == BEHEERDER
    assert rij["action"] == "deployment.write"
    assert rij["subject"] == "demo/live"
    assert rij["allowed"] is True
    assert rij["commits"][0]["short"] == "abc1234d"
    assert "origin" not in rij


def test_een_weigering_draagt_zijn_reden() -> None:
    [rij] = build_lotc_audit([_rij(data={"allowed": False, "cause": "insufficient-role"})])
    assert rij["allowed"] is False
    assert rij["cause"] == "insufficient-role"


def test_een_commit_zonder_webadres_blijft_tekst() -> None:
    """Een ``git://``-herkomst heeft geen pagina om naartoe te wijzen."""
    with patch("opi.web.lotc_switch.settings") as instellingen:
        instellingen.GIT_PROJECTS_SERVER_URL = "git://localhost:9090/"
        [rij] = build_lotc_audit([_rij()])
    assert rij["commits"][0]["url"] == ""


def test_een_https_herkomst_levert_een_commitadres() -> None:
    with patch("opi.web.lotc_switch.settings") as instellingen:
        instellingen.GIT_PROJECTS_SERVER_URL = "https://github.com/RijksICTGilde/rig-cluster-projects.git"
        [rij] = build_lotc_audit([_rij()])
    assert rij["commits"][0]["url"] == "https://github.com/RijksICTGilde/rig-cluster-projects/commit/abc1234def"


# --- de route in de context van de pagina -----------------------------------


def test_de_auditrijen_worden_alleen_op_het_tabblad_audit_opgehaald() -> None:
    """Anders wordt er per paginaweergave een audittrail opgevraagd die niemand ziet."""
    import inspect

    from opi.web import router

    bron = inspect.getsource(router.render_project_page)
    aanroep = bron.index("build_lotc_audit(")
    voorwaarde = bron.rindex('tab_from_path(request.url.path) == "audit"', 0, aanroep)
    assert voorwaarde > 0, "de audittrail wordt niet achter het tabblad gehouden"


def test_het_audittabblad_valt_uit_de_balk_zonder_leesrecht() -> None:
    """Geen lege tab, maar een recht: het tabblad hoort niet in de balk voor wie 403 krijgt."""
    import inspect

    from opi.web import router

    bron = inspect.getsource(router.render_project_page)
    toevoeging = bron.index('lege_tabs = (*lege_tabs, "audit")')
    voorwaarde = bron.rindex("if not may(request, action=Action.AUDIT_READ", 0, toevoeging)
    assert voorwaarde > 0, "de audittab valt niet uit de balk op basis van AUDIT_READ"


def test_het_sjabloon_noemt_de_oorsprong_niet() -> None:
    """``origin`` is een persoonsgegeven en hoort niet op de projectpagina."""
    bron = TABS.read_text()
    start = bron.index("{% elif active_tab == 'audit' %}")
    volgende = bron.find("{% elif active_tab ==", start + 10)
    stuk = re.sub(r"\{#.*?#\}", "", bron[start : volgende if volgende != -1 else len(bron)], flags=re.DOTALL)
    assert "rij.action" in stuk
    assert "origin" not in stuk
