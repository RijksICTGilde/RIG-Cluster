"""Het audittabblad: achter ``Action.AUDIT_READ``, en per project.

Drie dingen staan hier vast. De route is de ENIGE guard op ``list_actions``, die zelf
niets controleert: het project komt uit het PAD, dezelfde naam waarover ``decide()``
besliste, nooit een naam uit de querystring. ``limit`` heeft een plafond. En de pagina
toont ``origin`` niet: die kolom is voor platformbeheerders, in de database.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.parse import unquote
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException
from opi.core.authz.actions import Action
from opi.core.db import session_scope
from opi.core.leden import LidSoort
from opi.services import audit, persistence  # noqa: F401  -- registreert de ORM-modellen
from opi.services.audit import LIST_DEFAULT_LIMIT, LIST_MAX_LIMIT
from opi.services.persistence.notification_events import NotificationEvent
from opi.services.project_service import ProjectSummary, ProjectUser
from opi.web.authz import enforce_route_action, may
from opi.web.lotc_switch import (
    AUDIT_QUERY_MAX,
    PROJECT_TABS,
    AuditFilters,
    audit_cursor_text,
    audit_filters,
    audit_limit,
    audit_pagina,
    audit_url,
    build_lotc_audit,
    project_tab_url,
    tab_from_path,
)
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
    assert PROJECT_TABS["audit"] == {"label": "Audit", "path": "audit", "icon": "clock-arrow-counter-clockwise"}
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


def _commit_url(*, server: str, web: str = "") -> str:
    with patch("opi.web.lotc_switch.settings") as instellingen:
        instellingen.GIT_PROJECTS_SERVER_URL = server
        instellingen.GIT_PROJECTS_WEB_URL = web
        [rij] = build_lotc_audit([_rij()])
    return str(rij["commits"][0]["url"])


def test_een_commit_zonder_webadres_blijft_tekst() -> None:
    """Een ``git://``-herkomst heeft geen pagina om naartoe te wijzen."""
    assert _commit_url(server="git://localhost:9090/") == ""


def test_een_https_herkomst_levert_een_commitadres() -> None:
    url = _commit_url(server="https://github.com/RijksICTGilde/rig-cluster-projects.git")
    assert url == "https://github.com/RijksICTGilde/rig-cluster-projects/commit/abc1234def"


def test_een_clusterintern_adres_blijft_tekst() -> None:
    """De sandbox kloont van een hostnaam die geen browser kan openen."""
    server = "http://forgejo.rig-system.svc.cluster.local:3000/rig-admin/zad-projects.git"
    assert _commit_url(server=server) == ""


def test_het_webadres_gaat_voor_de_kloon_url() -> None:
    url = _commit_url(
        server="http://forgejo.rig-system.svc.cluster.local:3000/rig-admin/zad-projects.git",
        web="https://forgejo.sandbox.rijksapp.dev/rig-admin/zad-projects",
    )
    assert url == "https://forgejo.sandbox.rijksapp.dev/rig-admin/zad-projects/commit/abc1234def"


# --- de route in de context van de pagina -----------------------------------


def test_de_auditrijen_worden_alleen_op_het_tabblad_audit_opgehaald() -> None:
    """Anders wordt er per paginaweergave een audittrail opgevraagd die niemand ziet."""
    import inspect

    from opi.web import router

    bron = inspect.getsource(router.render_project_page)
    aanroep = bron.index("audit_pagina(")
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


# --- zoeken, periode en bladeren (RC-405) -----------------------------------
#
# English per RC-405: new names, test names and comments are English, also in this
# otherwise Dutch module.


def _filters(**query: str) -> AuditFilters:
    return audit_filters(_request(BEHEERDER, "demo", query))


def test_a_readable_period_becomes_two_dutch_day_boundaries() -> None:
    filters = _filters(**{"from": "2026-07-01", "until": "2026-07-03"})

    assert filters.since is not None
    assert filters.since.isoformat() == "2026-07-01T00:00:00+02:00"
    # Exclusive, and the day after: "tot en met 3 juli" has to include 3 juli 23:59.
    assert filters.until is not None
    assert filters.until.isoformat() == "2026-07-04T00:00:00+02:00"
    assert (filters.since_text, filters.until_text) == ("2026-07-01", "2026-07-03")


def test_an_unreadable_date_drops_that_filter_without_an_error() -> None:
    filters = _filters(**{"from": "gisteren", "until": "2026-13-45"})

    assert (filters.since, filters.until) == (None, None)
    assert (filters.since_text, filters.until_text) == ("", "")


def test_a_date_whose_next_day_does_not_exist_drops_that_filter() -> None:
    # ``9999-12-31`` leest wel als datum, maar de dag erna loopt buiten date.max.
    filters = _filters(until="9999-12-31")

    assert (filters.until, filters.until_text) == (None, "")
    assert _filters(**{"from": "9999-12-31"}).since is not None


@pytest.mark.parametrize(
    ("dag", "binnen", "buiten"),
    [
        # Zomertijd, wintertijd, en de twee dagen waarop de klok verspringt.
        ("2026-07-01", "2026-07-01T23:59:00+02:00", "2026-07-02T00:00:00+02:00"),
        ("2026-01-15", "2026-01-15T23:59:00+01:00", "2026-01-16T00:00:00+01:00"),
        ("2026-03-29", "2026-03-29T23:59:00+02:00", "2026-03-30T00:00:00+02:00"),
        ("2026-10-25", "2026-10-25T23:59:00+01:00", "2026-10-26T00:00:00+01:00"),
    ],
)
def test_until_takes_the_whole_dutch_day(dag: str, binnen: str, buiten: str) -> None:
    grens = _filters(until=dag).until

    assert grens is not None
    assert datetime.fromisoformat(binnen) < grens
    assert datetime.fromisoformat(buiten) >= grens


def test_a_cursor_round_trips_and_a_broken_one_is_ignored() -> None:
    rij = SimpleNamespace(time=datetime(2026, 10, 28, 12, 30, tzinfo=UTC), id=uuid.UUID(int=7))
    tekst = audit_cursor_text(rij)

    heen = _filters(before=tekst).before
    assert heen is not None
    assert (heen.time, heen.id) == (rij.time, rij.id)

    for rommel in ("", "rommel", "2026-10-28T12:30:00+00:00_geen-uuid", "geen-tijd_" + str(rij.id)):
        assert _filters(before=rommel).before is None
    # Een naief tijdstip komt niet van ons en gaat niet tegen een timestamptz aan.
    assert _filters(before=f"2026-10-28T12:30:00_{rij.id}").before is None


def test_two_cursors_at_once_keep_only_before() -> None:
    # list_actions behandelt beide samen als programmeerfout, dus de querystring mag die
    # combinatie niet doorlaten.
    rij = SimpleNamespace(time=datetime(2026, 10, 28, 12, 30, tzinfo=UTC), id=uuid.UUID(int=7))
    tekst = audit_cursor_text(rij)

    filters = _filters(before=tekst, after=tekst)

    assert filters.before is not None
    assert filters.after is None


def test_a_very_long_search_term_is_cut_off() -> None:
    filters = _filters(q=" " + "a" * 400 + " ")

    assert filters.query == "a" * AUDIT_QUERY_MAX


def test_paging_carries_the_filters_and_at_most_one_cursor() -> None:
    filters = _filters(q="jansen", limit="10", **{"from": "2026-07-01"})
    url = audit_url("/projects/demo/audit", filters, before="2026-07-01T00%3A00_x")

    assert url.startswith("/projects/demo/audit?")
    assert "q=jansen" in url
    assert "from=2026-07-01" in url
    assert "limit=10" in url
    assert "before=" in url
    assert "after=" not in url
    assert "until=" not in url


def test_the_time_in_the_table_is_dutch_time() -> None:
    """Een handeling om 00:30 Nederlandse tijd stond als UTC op de dag ervoor."""
    moment = datetime(2026, 7, 1, 0, 30, tzinfo=ZoneInfo("Europe/Amsterdam"))
    rij_uit_de_database = SimpleNamespace(
        time=moment.astimezone(UTC),
        actor=BEHEERDER,
        actor_kind="user",
        subject="demo",
        data={"action": "project.refresh", "allowed": True},
    )
    [rij] = build_lotc_audit([rij_uit_de_database])

    assert rij["time"].strftime("%d-%m-%Y %H:%M") == "01-07-2026 00:30"


# --- wat het tabblad oplevert, tegen een echte database ---------------------
#
# Via audit_pagina en niet door render_project_page aan te roepen: een render van die
# pagina vraagt het halve projectmodel mee, zoals tests/test_argo_connect_retry.py en
# tests/test_lotc_backups_tabblad.py voor dezelfde functie al vaststellen.


async def _schrijf(**overrides: Any) -> None:
    velden: dict[str, Any] = {
        "source": audit.event_source(),
        "type": audit.TYPE_PERFORMED,
        "subject": "demo",
        "time": datetime.now(UTC),
        "category": audit.CATEGORY,
        "severity": audit.SEVERITY_ALLOWED,
        "cluster": "local",
        "project": "demo",
        "actor": "ops@rijksoverheid.nl",
        "actor_kind": "user",
        "summary": "demo: project.refresh door ops",
        "dedup_key": "audit:project.refresh:demo:allowed",
        "data": {"action": "project.refresh", "allowed": True},
    }
    velden.update(overrides)
    async with session_scope() as db:
        db.add(NotificationEvent(**velden))


async def test_the_tab_shows_only_the_rows_the_query_asks_for(orm_db: None) -> None:
    binnen = datetime(2026, 7, 2, 10, 0, tzinfo=UTC)
    await _schrijf(time=binnen, actor="jansen@rijksoverheid.nl", data={"action": "component.write"})
    await _schrijf(time=binnen, actor="pietersen@rijksoverheid.nl", data={"action": "component.write"})
    await _schrijf(time=datetime(2026, 6, 1, 10, 0, tzinfo=UTC), actor="jansen@rijksoverheid.nl")

    context = await audit_pagina(
        _request(BEHEERDER, "demo", {"q": "jansen", "from": "2026-07-01", "until": "2026-07-03"}), "demo"
    )

    assert [rij["actor"] for rij in context["audit_rijen"]] == ["jansen@rijksoverheid.nl"]
    assert context["audit_filter_actief"] is True
    assert context["audit_query"] == "jansen"


async def test_the_older_link_carries_the_filters_and_a_cursor(orm_db: None) -> None:
    for minuut in range(3):
        await _schrijf(time=datetime(2026, 7, 2, 10, minuut, tzinfo=UTC), data={"action": f"actie{minuut}"})

    eerste = await audit_pagina(_request(BEHEERDER, "demo", {"q": "actie", "limit": "2"}), "demo")

    assert [rij["action"] for rij in eerste["audit_rijen"]] == ["actie2", "actie1"]
    assert "q=actie" in eerste["audit_ouder_url"]
    assert "limit=2" in eerste["audit_ouder_url"]
    assert "before=" in eerste["audit_ouder_url"]
    # Op de nieuwste pagina is er niets nieuwer.
    assert eerste["audit_nieuwer_url"] == ""

    cursor = eerste["audit_ouder_url"].split("before=")[1]
    tweede = await audit_pagina(
        _request(BEHEERDER, "demo", {"q": "actie", "limit": "2", "before": unquote(cursor)}), "demo"
    )

    assert [rij["action"] for rij in tweede["audit_rijen"]] == ["actie0"]
    assert tweede["audit_ouder_url"] == ""
    assert "after=" in tweede["audit_nieuwer_url"]


async def test_an_empty_outcome_offers_a_way_back(orm_db: None) -> None:
    await _schrijf()

    context = await audit_pagina(_request(BEHEERDER, "demo", {"q": "bestaat-niet"}), "demo")

    assert context["audit_rijen"] == []
    assert context["audit_filter_actief"] is True
    assert context["audit_wis_url"] == "/projects/demo/audit"
