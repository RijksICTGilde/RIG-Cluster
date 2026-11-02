"""De hook: elke beslissing van een verzoek wordt een rij in de audit.

Zonder code per route, dus de toets gaat over de ROUTETABEL: elke route die een action
draagt die geen leesactie is, levert toegestaan en geweigerd precies een rij per
beslissing. Wat de rij verder draagt (de statuscode, de commit-SHA's, de taak-id, de
velden) komt uit het spoor van het verzoek (``opi/core/audit_trail.py``).
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from opi.api.endpoint_util import handhaaf
from opi.core.actor import Actor, ActorKind, get_actor, service_account_actor, user_actor
from opi.core.audit_trail import MAX_VELDWAARDE, begin_trail, capture_fields, record_commit, record_task, trail
from opi.core.auth_decorators import requires_step_up
from opi.core.authz.actions import Action, action, action_of, audit_fields_of
from opi.core.authz.engine import (
    ANY,
    PRODUCTION,
    Cause,
    Decision,
    Grant,
    RecordedDecision,
    Resource,
    Role,
    all_rules,
    rule_for,
)
from opi.core.leden import LidSoort
from opi.core.step_up import StepUpAction
from opi.middleware.audit import AuditMiddleware, _schrijvers
from opi.server import app
from opi.services.project_leden import hash_van, vervaldatum
from opi.services.project_service import ProjectSummary, ProjectToken, ProjectUser
from opi.utils.client_ip import PEER_SCOPE_KEY
from opi.web.authz import enforce_route_action
from starlette.requests import Request

GEBRUIKER = user_actor("beheerder@rijksoverheid.nl")

#: Velden die een persoon aanwijzen. Een route mag ze niet in de audit noemen: ``fields``
#: blijft een jaar staan, ook nadat de bewaarlus ``actor`` en ``origin`` heeft leeggemaakt.
PERSOONSVELDEN = frozenset(
    {
        "email",
        "emails",
        "e-mail",
        "gebruiker",
        "gebruikersnaam",
        "username",
        "user",
        "leden",
        "lid",
        "member",
        "members",
        "actor",
        "origin",
        "ip",
    }
)


def _resource(project: str = "demo", deployment: str | None = "live") -> Resource:
    pad = f"project/{project}" + (f"/deployment/{deployment}" if deployment else "")
    return Resource(project=project, path=pad, deployment=deployment, environment="productie")


def _scope(**extra: Any) -> dict[str, Any]:
    return {
        "type": "http",
        "method": "POST",
        "path": "/api/v2/projects/demo/deployments",
        "headers": [],
        "query_string": b"",
        "path_params": {},
        "state": {},
        PEER_SCOPE_KEY: "10.0.0.7",
        **extra,
    }


async def _afgeschreven() -> None:
    """Wacht tot de achtergrondtaken van de hook klaar zijn.

    De rij wordt met opzet NA het antwoord geschreven, dus zonder deze wacht meet een
    toets de wachtrij in plaats van de rij.
    """
    await asyncio.gather(*list(_schrijvers), return_exceptions=True)


async def _draai(
    beslissingen: list[RecordedDecision],
    *,
    status: int = 200,
    actor: Actor = GEBRUIKER,
    spoor: Any = None,
    rijen: AsyncMock | None = None,
    scope: dict[str, Any] | None = None,
) -> AsyncMock:
    """Laat de middleware een verzoek afhandelen dat deze beslissingen nam."""
    geschreven = rijen or AsyncMock()

    async def app_met_beslissingen(verzoek_scope: dict[str, Any], receive: Any, send: Any) -> None:
        verzoek_scope["state"]["authz_decisions"] = beslissingen
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def send(_message: dict[str, Any]) -> None: ...

    middleware = AuditMiddleware(app_met_beslissingen)
    with (
        patch("opi.middleware.audit.record_action", geschreven),
        patch("opi.middleware.audit.get_actor", return_value=actor),
        patch("opi.middleware.audit.trail", return_value=spoor or _spoor()),
    ):
        await middleware(scope or _scope(), AsyncMock(), send)
        await _afgeschreven()
    return geschreven


def _spoor(commits: list[str] | None = None, task_id: uuid.UUID | None = None, fields: dict[str, object] | None = None):
    from opi.core.audit_trail import Trail

    return Trail(commit_shas=commits or [], task_id=task_id, fields=fields or {})


# --- de routetabel ----------------------------------------------------------


def _schrijfacties() -> list[Action]:
    """Elke action uit de ECHTE routetabel waarvan de regel hem niet als lezen markeert."""
    gevonden = {
        handeling
        for route in app.routes
        if (handeling := action_of(getattr(route, "endpoint", None))) is not None and not rule_for(handeling).read
    }
    return sorted(gevonden, key=lambda handeling: handeling.value)


def test_de_routetabel_heeft_schrijfacties_om_te_toetsen() -> None:
    """Een lege lijst zou de toets hieronder stilzwijgend over niets laten lopen."""
    assert len(_schrijfacties()) > 20


@pytest.mark.asyncio
@pytest.mark.parametrize("toegestaan", [True, False])
async def test_elke_schrijfroute_levert_een_rij_per_beslissing(toegestaan: bool) -> None:
    for handeling in _schrijfacties():
        beslissing = (
            Decision(allowed=True, grant=Grant(Role.OPS, PRODUCTION))
            if toegestaan
            else Decision(allowed=False, cause=Cause.INSUFFICIENT_ROLE)
        )
        geschreven = await _draai(
            [RecordedDecision(handeling, _resource(), beslissing, step_up=True)],
            status=200 if toegestaan else 403,
        )
        assert geschreven.await_count == 1, handeling.value
        rij = geschreven.await_args.kwargs
        assert rij["action"] == handeling.value
        assert rij["actor"] is GEBRUIKER
        assert rij["resource_path"] == "project/demo/deployment/live"
        assert rij["project"] == "demo"
        assert rij["deployment"] == "live"
        assert rij["environment"] == "productie"
        assert rij["allowed"] is toegestaan
        assert rij["origin"] == "10.0.0.7"
        assert rij["status_code"] == (200 if toegestaan else 403)
        if toegestaan:
            assert rij["grant"] == "ops@production"
            assert rij["cause"] is None
        else:
            assert rij["cause"] == Cause.INSUFFICIENT_ROLE.value


# --- lezen en weigeren ------------------------------------------------------


@pytest.mark.asyncio
async def test_een_toegestane_leesactie_levert_geen_rij() -> None:
    geschreven = await _draai(
        [RecordedDecision(Action.PROJECT_READ, _resource(), Decision(allowed=True, grant=Grant(Role.VIEWER)), True)]
    )
    assert geschreven.await_count == 0


@pytest.mark.asyncio
async def test_een_geweigerde_leesactie_levert_wel_een_rij() -> None:
    geschreven = await _draai(
        [
            RecordedDecision(
                Action.PROJECT_READ, _resource(), Decision(allowed=False, cause=Cause.INSUFFICIENT_ROLE), True
            )
        ],
        status=403,
    )
    assert geschreven.await_count == 1
    assert geschreven.await_args.kwargs["allowed"] is False


@pytest.mark.asyncio
async def test_een_verzoek_zonder_beslissing_schrijft_niets() -> None:
    geschreven = await _draai([])
    assert geschreven.await_count == 0


# --- twee beslissingen in een verzoek ---------------------------------------


@pytest.mark.asyncio
async def test_een_upsert_met_clonefrom_levert_twee_rijen() -> None:
    """Het doel en de bron wegen apart, en de audit wil ze beide zien."""
    geschreven = await _draai(
        [
            RecordedDecision(
                Action.DEPLOYMENT_WRITE, _resource(deployment="live"), Decision(True, grant=Grant(Role.OPS)), True
            ),
            RecordedDecision(
                Action.DEPLOYMENT_CLONE_FROM, _resource(deployment="bron"), Decision(True, grant=Grant(Role.OPS)), True
            ),
        ]
    )
    assert geschreven.await_count == 2
    acties = [aanroep.kwargs["action"] for aanroep in geschreven.await_args_list]
    assert acties == ["deployment.write", "deployment.clone-from"]
    assert [aanroep.kwargs["deployment"] for aanroep in geschreven.await_args_list] == ["live", "bron"]


@pytest.mark.asyncio
async def test_een_geweigerde_bron_staat_als_eigen_rij_in_de_audit() -> None:
    geschreven = await _draai(
        [
            RecordedDecision(Action.DEPLOYMENT_WRITE, _resource(), Decision(True, grant=Grant(Role.OPS)), True),
            RecordedDecision(
                Action.DEPLOYMENT_CLONE_FROM,
                _resource(deployment="bron"),
                Decision(False, cause=Cause.INSUFFICIENT_ROLE),
                True,
            ),
        ],
        status=403,
    )
    uitkomsten = [(aanroep.kwargs["action"], aanroep.kwargs["allowed"]) for aanroep in geschreven.await_args_list]
    assert uitkomsten == [("deployment.write", True), ("deployment.clone-from", False)]


# --- het spoor van het verzoek ----------------------------------------------


@pytest.mark.asyncio
async def test_de_commit_de_taak_en_de_velden_staan_op_elke_rij() -> None:
    taak = uuid.uuid4()
    geschreven = await _draai(
        [
            RecordedDecision(Action.DEPLOYMENT_WRITE, _resource(), Decision(True, grant=Grant(Role.OPS)), True),
            RecordedDecision(Action.ENVIRONMENT_SET, _resource(), Decision(True, grant=Grant(Role.OPS)), True),
        ],
        spoor=_spoor(commits=["abc1234", "def5678"], task_id=taak, fields={"image": "app:1"}),
    )
    assert geschreven.await_count == 2
    for aanroep in geschreven.await_args_list:
        assert aanroep.kwargs["commit_shas"] == ["abc1234", "def5678"]
        assert aanroep.kwargs["task_id"] == taak
        assert aanroep.kwargs["fields"] == {"image": "app:1"}


@pytest.mark.asyncio
async def test_een_verzoek_zonder_velden_geeft_fields_niet_mee() -> None:
    geschreven = await _draai(
        [RecordedDecision(Action.DEPLOYMENT_WRITE, _resource(), Decision(True, grant=Grant(Role.OPS)), True)]
    )
    assert geschreven.await_args.kwargs["fields"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "actor",
    [
        user_actor("lid@rijksoverheid.nl"),
        service_account_actor("demo", "pijplijn"),
        Actor(ActorKind.SCHEDULER, "backup"),
    ],
)
async def test_elke_soort_aanroeper_levert_een_gevulde_actor(actor: Actor) -> None:
    geschreven = await _draai(
        [RecordedDecision(Action.DEPLOYMENT_WRITE, _resource(), Decision(True, grant=Grant(Role.OPS)), True)],
        actor=actor,
    )
    rij_actor = geschreven.await_args.kwargs["actor"]
    assert rij_actor.identity
    assert rij_actor.kind is actor.kind


@pytest.mark.asyncio
async def test_een_gefaalde_schrijfactie_laat_het_antwoord_ongemoeid() -> None:
    """``record_action`` gooit niet, maar als het ooit toch gebeurt is het antwoord al weg."""
    stuk = AsyncMock(side_effect=RuntimeError("database weg"))
    verstuurd: list[dict[str, Any]] = []

    async def app_met_beslissing(scope: dict[str, Any], receive: Any, send: Any) -> None:
        scope["state"]["authz_decisions"] = [
            RecordedDecision(Action.DEPLOYMENT_WRITE, _resource(), Decision(True, grant=Grant(Role.OPS)), True)
        ]
        await send({"type": "http.response.start", "status": 200, "headers": []})

    async def send(message: dict[str, Any]) -> None:
        verstuurd.append(message)

    middleware = AuditMiddleware(app_met_beslissing)
    with (
        patch("opi.middleware.audit.record_action", stuk),
        patch("opi.middleware.audit.get_actor", return_value=GEBRUIKER),
        patch("opi.middleware.audit.trail", return_value=_spoor()),
    ):
        await middleware(_scope(), AsyncMock(), send)
        await _afgeschreven()

    assert verstuurd == [{"type": "http.response.start", "status": 200, "headers": []}]


@pytest.mark.asyncio
async def test_een_websocket_komt_niet_in_de_audit() -> None:
    """De hook leest een antwoordstatus; een websocket heeft die niet."""
    geschreven = AsyncMock()
    gezien: list[str] = []

    async def binnenste(scope: dict[str, Any], receive: Any, send: Any) -> None:
        gezien.append(scope["type"])

    with patch("opi.middleware.audit.record_action", geschreven):
        await AuditMiddleware(binnenste)({"type": "websocket"}, AsyncMock(), AsyncMock())

    assert gezien == ["websocket"]
    assert geschreven.await_count == 0


# --- de velden die een route noemt ------------------------------------------


def test_geen_route_noemt_een_veld_dat_een_persoon_aanwijst() -> None:
    """``fields`` overleeft de pseudonimisering, dus er mag nooit een persoon in staan."""
    gevonden: dict[str, tuple[str, ...]] = {}
    for route in app.routes:
        endpoint = getattr(route, "endpoint", None)
        velden = audit_fields_of(endpoint) if endpoint is not None else ()
        verboden = tuple(veld for veld in velden if veld.lower() in PERSOONSVELDEN)
        if verboden:
            gevonden[getattr(endpoint, "__name__", str(endpoint))] = verboden
    assert not gevonden, f"deze routes noemen een persoonsveld in de audit: {gevonden}"


def test_een_step_up_route_noemt_daarmee_geen_auditvelden() -> None:
    """``velden=`` is er voor de dialoog, niet voor de rij.

    De twee lijsten stonden gekoppeld, en daarmee liftte de env-var-route mee: zijn veld
    ``vars`` is de hele platte env-var-tekst, met wachtwoorden en adressen erin, en die
    zou een jaar in ``data.fields`` blijven staan.
    """

    @action(Action.TOKEN_MANAGE)
    @requires_step_up(actie=StepUpAction.TOKEN_ISSUE, velden=("tokennaam", "dagen"))
    async def route(request: Any, project_name: str) -> None: ...

    assert audit_fields_of(route) == ()


def test_alleen_deze_routes_noemen_velden_in_de_audit() -> None:
    """Een nieuwe route komt hier bewust bij: ``fields`` overleeft de pseudonimisering.

    Alle vier noemen een waarde die NIET in het projectbestand landt, dus waar de
    commit-SHA van de rij niets aanwijst. Elke andere schrijfroute laat de velden weg.
    """
    gevonden = {
        getattr(route.endpoint, "__name__", ""): audit_fields_of(route.endpoint)
        for route in app.routes
        if audit_fields_of(getattr(route, "endpoint", None))
    }

    assert gevonden == {
        "delete_snapshot": ("snapshot_id",),
        "push_image": ("image_name", "tag"),
        "restore_backup_run": ("backup_run_id",),
        "job_start": ("deployment", "image"),
    }


def test_audit_fields_op_action_markeert_een_route_zonder_step_up() -> None:
    @action(Action.BACKUP_DELETE, audit_fields=("snapshot_id",))
    async def route(project_name: str) -> None: ...

    assert audit_fields_of(route) == ("snapshot_id",)


def test_een_route_zonder_markering_noemt_geen_velden() -> None:
    @action(Action.DEPLOYMENT_WRITE)
    async def route(project_name: str) -> None: ...

    assert audit_fields_of(route) == ()


def test_elke_regel_in_het_register_is_te_beoordelen_op_lezen() -> None:
    """De hook valt terug op "wel vastleggen" als een regel ontbreekt; dat mag nooit nodig zijn."""
    assert all(isinstance(regel.read, bool) for regel in all_rules())
    assert rule_for(Action.AUDIT_READ).read is True
    assert Grant(Role.ADMIN, ANY) in rule_for(Action.AUDIT_READ).grants


# --- de plek in de middlewarestapel ----------------------------------------


def test_de_hook_staat_als_binnenste_om_de_router() -> None:
    """Een ``BaseHTTPMiddleware`` ertussen zou het spoor van het verzoek wegsnijden.

    Die draait de rest van de applicatie in een eigen taak, en een contextvar die
    daarbinnen wordt gezet (de commit-SHA's, de taak-id) komt dan niet meer terug.
    """
    assert app.user_middleware[-1].cls is AuditMiddleware


@pytest.mark.asyncio
async def test_het_spoor_van_het_verzoek_bereikt_de_rij_echt() -> None:
    """Zonder nagemaakt spoor: wat de route in de contextvars zet, staat in de rij."""
    taak = uuid.uuid4()
    geschreven = AsyncMock()

    async def app_dat_opslaat(scope: dict[str, Any], receive: Any, send: Any) -> None:
        record_commit("abc1234")
        record_task(taak)
        scope["state"]["authz_decisions"] = [
            RecordedDecision(Action.DEPLOYMENT_WRITE, _resource(), Decision(True, grant=Grant(Role.OPS)), True)
        ]
        await send({"type": "http.response.start", "status": 201, "headers": []})

    async def send(_message: dict[str, Any]) -> None: ...

    with (
        patch("opi.middleware.audit.record_action", geschreven),
        patch("opi.middleware.audit.get_actor", return_value=GEBRUIKER),
    ):
        await AuditMiddleware(app_dat_opslaat)(_scope(), AsyncMock(), send)
        await _afgeschreven()

    rij = geschreven.await_args.kwargs
    assert rij["commit_shas"] == ["abc1234"]
    assert rij["task_id"] == taak
    assert rij["status_code"] == 201


# --- de actor op een weigering ----------------------------------------------


@pytest.mark.asyncio
async def test_een_geweigerde_api_aanroep_draagt_de_actor() -> None:
    """Gemeten op de sandbox: zonder ``set_actor`` vóór de beslissing bleef de kolom leeg.

    Juist een weigering hoort te zeggen WIE hem deed (BIO2 8.15.01), en de aanroeper is
    bekend zodra het bewijsmiddel herkend is.
    """
    token = "zad_kijker"
    project = ProjectSummary(
        name="demo",
        filename="demo.yaml",
        data={"name": "demo"},
        users=[
            ProjectUser(
                email="kijker@rijksoverheid.nl",
                roles=["viewer"],
                type=LidSoort.PERSOON,
                tokens=[ProjectToken(name="t", hash=hash_van(token), expires=vervaldatum(30))],
            )
        ],
    )
    verzoek = SimpleNamespace(state=SimpleNamespace(), session={}, path_params={}, headers={}, query_params={})
    gebruikers = MagicMock()
    gebruikers.is_platform_admin.return_value = False
    gebruikers.is_email_allowed.return_value = True

    @action(Action.PROJECT_REFRESH)
    async def route(project_name: str) -> None: ...

    with (
        patch("opi.api.principal.get_user_service", return_value=gebruikers),
        pytest.raises(HTTPException) as fout,
    ):
        await handhaaf(verzoek, token, route, project)

    assert fout.value.status_code == 403
    assert get_actor().identity == "kijker@rijksoverheid.nl"


# --- de velden die DIT verzoek meebrengt --------------------------------------


def _verzoek_voor_velden(
    *, path_params: dict[str, str] | None = None, query_params: dict[str, str] | None = None, content_type: str = ""
) -> Any:
    return SimpleNamespace(
        path_params=path_params or {},
        query_params=query_params or {},
        headers={"content-type": content_type} if content_type else {},
    )


@pytest.mark.asyncio
async def test_capture_fields_zonder_markering_doet_niets() -> None:
    @action(Action.DEPLOYMENT_WRITE)
    async def route() -> None: ...

    begin_trail()
    await capture_fields(_verzoek_voor_velden(query_params={"image_name": "app"}), route)
    assert trail().fields == {}


@pytest.mark.asyncio
async def test_capture_fields_neemt_path_en_query_params() -> None:
    @action(Action.BACKUP_DELETE, audit_fields=("snapshot_id", "image_name"))
    async def route() -> None: ...

    begin_trail()
    await capture_fields(
        _verzoek_voor_velden(path_params={"snapshot_id": "snap-1"}, query_params={"image_name": "app"}),
        route,
    )
    assert trail().fields == {"snapshot_id": "snap-1", "image_name": "app"}


@pytest.mark.asyncio
async def test_capture_fields_valt_terug_op_het_formulier_bij_ontbrekend_veld() -> None:
    @action(Action.JOB_RUN, audit_fields=("deployment", "image"))
    async def route() -> None: ...

    begin_trail()
    verzoek = _verzoek_voor_velden(path_params={"deployment": "live"}, content_type="application/x-www-form-urlencoded")
    verzoek.form = AsyncMock(return_value={"image": "app:1"})

    await capture_fields(verzoek, route)
    assert trail().fields == {"deployment": "live", "image": "app:1"}


@pytest.mark.asyncio
async def test_capture_fields_laat_een_ontbrekend_veld_weg_zonder_formulier_content_type() -> None:
    """Een multipart- of JSON-body wordt niet ontleed om er een veldnaam uit te halen."""

    @action(Action.JOB_RUN, audit_fields=("deployment", "image"))
    async def route() -> None: ...

    begin_trail()
    verzoek = _verzoek_voor_velden(path_params={"deployment": "live"}, content_type="multipart/form-data")
    verzoek.form = AsyncMock(side_effect=AssertionError("het formulier hoort hier niet gelezen te worden"))

    await capture_fields(verzoek, route)
    assert trail().fields == {"deployment": "live"}


@pytest.mark.asyncio
async def test_capture_fields_buiten_een_verzoek_legt_niets_vast() -> None:
    """Het spoor wordt door de hook geopend; buiten een verzoek is er niets om in te zetten."""

    @action(Action.BACKUP_DELETE, audit_fields=("snapshot_id",))
    async def route() -> None: ...

    await capture_fields(_verzoek_voor_velden(path_params={"snapshot_id": "snap-1"}), route)
    assert trail().fields == {}


@pytest.mark.asyncio
async def test_twee_rondes_buiten_een_verzoek_laten_het_spoor_leeg() -> None:
    """De nachtelijke tuner commit in EEN langlevende taak, en niemand leest daar het spoor.

    Zonder deze grens groeide de contextvar van die taak elke nacht door.
    """
    for ronde in range(2):
        record_commit(f"sha-{ronde}")
        record_task(uuid.uuid4())

    assert trail().commit_shas == []
    assert trail().task_id is None


@pytest.mark.asyncio
async def test_de_hook_opent_het_spoor_van_dit_verzoek() -> None:
    """Een SHA uit een vorige ronde hoort niet in de rij van dit verzoek."""
    begin_trail()
    record_commit("van-ervoor")
    gezien: list[list[str]] = []

    async def binnenste(scope: dict[str, Any], receive: Any, send: Any) -> None:
        record_commit("van-dit-verzoek")
        gezien.append(list(trail().commit_shas))

    await AuditMiddleware(binnenste)(_scope(), AsyncMock(), AsyncMock())

    assert gezien == [["van-dit-verzoek"]]


@pytest.mark.asyncio
async def test_een_veldwaarde_wordt_afgekapt() -> None:
    """``data`` is JSONB en heeft geen kolombreedte, dus de afkapping zit hier.

    Zonder die grens zette een aanroeper er net zoveel in als zijn body groot is, en dat
    blijft een jaar staan: gemeten 400 kB uit een urlencoded body in een enkele rij.
    """

    @action(Action.JOB_RUN, audit_fields=("deployment", "image"))
    async def route() -> None: ...

    begin_trail()
    verzoek = _verzoek_voor_velden(
        query_params={"deployment": "x" * 5000}, content_type="application/x-www-form-urlencoded"
    )
    verzoek.form = AsyncMock(return_value={"image": "y" * 5000})

    await capture_fields(verzoek, route)
    assert len(trail().fields["deployment"]) == MAX_VELDWAARDE
    assert len(trail().fields["image"]) == MAX_VELDWAARDE


@pytest.mark.asyncio
async def test_de_poort_leest_het_formulier_pas_na_het_toelaten() -> None:
    """Een aanroeper die niets mag hoort geen body te laten ontleden, net als op de API.

    ``POST /projects/{p}/jobs`` draagt auditvelden die uit het formulier komen. Stond de
    lezing voor ``authorize``, dan haalde een geweigerde (ook anonieme) aanroep de hele
    urlencoded body door het geheugen en in de rij van de weigering.
    """
    route = next(r for r in app.routes if getattr(r, "path", "") == "/projects/{project_name}/jobs")
    assert audit_fields_of(route.endpoint) == ("deployment", "image")

    scope = _scope(
        path="/projects/demo/jobs",
        headers=[(b"content-type", b"application/x-www-form-urlencoded")],
        path_params={"project_name": "demo"},
        route=route,
    )
    verzoek = Request(scope)
    begin_trail()

    with patch("opi.web.authz.authorize", side_effect=HTTPException(status_code=403)), pytest.raises(HTTPException):
        await enforce_route_action(verzoek)

    assert trail().fields == {}
