"""De drie grenzen op de publieke uitnodigingsroutes (RC-313).

Een uitnodigingssleutel is een bearer-geheim: wie hem heeft kan in het projectrealm een
account met de rollen van de uitnodiging krijgen. Wat hier gemeten wordt:

1. De routes zijn begrensd, en ``POST /invite/{key}/register`` strenger dan de leesweg, met
   een emmer per SLEUTEL erbij die ook houdt als het client-adres niet te vertrouwen is.
2. De sleutel komt in geen enkele logregel terecht, ook niet via het pad.
3. Een NIEUWE zelfgekozen sleutel is minstens 16 tekens; een sleutel die al in het
   projectbestand stond blijft geldig.
"""

import ast
import itertools
import logging
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.api import invite_routes
from opi.api.invite_routes import (
    invite_landing,
    invite_register_form,
    invite_register_submit,
    invite_sso_start,
)
from opi.api.router import IPRateLimiter
from opi.core.project_schema import ProjectIntegrityError
from opi.manager.project_validation import validate_project_structure
from opi.middleware.authorization import AuthorizationMiddleware
from opi.services.catalog.invite import INVITE_KEY_MIN_LENGTH, InviteService
from opi.services.registry import change_validating_services
from opi.utils import logging_config
from opi.utils.logging_redact import fingerprint_for_log, redact_invite_key_in_path
from starlette.requests import Request

if TYPE_CHECKING:
    from collections.abc import Iterator

PROJECT = ("demo", {"display-name": "Demo-applicatie"}, {"contact_email": "beheer@example.org"}, "sandbox")

FORM = {
    "email": "iemand@example.org",
    "first_name": "Ie",
    "last_name": "Mand",
    "password": "GeheimGeheim1",
    "password_confirm": "GeheimGeheim1",
}

#: Elke test krijgt een eigen client-adres: de begrenzers staan op moduleniveau en houden
#: hun emmers tussen tests vast.
_adressen = itertools.count(1)


def _adres() -> str:
    return f"10.0.0.{next(_adressen)}"


_EMMERS = ("_READ_LIMITER", "_REGISTER_PER_MINUTE", "_REGISTER_PER_HOUR", "_REGISTER_PER_KEY_PER_HOUR")


@contextmanager
def _verse_emmers() -> Iterator[None]:
    """Verse emmers voor deze test, met de burst die de routes echt gebruiken.

    De emmers staan op moduleniveau, dus zonder dit meet een test wat de vorige overliet.
    En ze vullen zich op de klok: een pagina renderen kost op een belaste machine bijna een
    seconde, en bij 30 per minuut is dat een halve token terug per aanroep. Daarom vullen
    de verse emmers niet bij (``requests_per_minute=0``): dan meet de test precies de burst.
    De ingestelde snelheid zelf staat in ``test_de_ingestelde_grenzen_staan_waar_het_plan_ze_zette``.
    """
    with ExitStack() as stack:
        for naam in _EMMERS:
            burst = getattr(invite_routes, naam).burst
            stack.enter_context(patch.object(invite_routes, naam, IPRateLimiter(requests_per_minute=0, burst=burst)))
        yield


def _request(ip: str) -> MagicMock:
    request = MagicMock()
    request.session = {}
    request.query_params = {}
    request.headers = {"x-forwarded-for": ip, "user-agent": "test", "accept-language": "nl"}
    request.form = AsyncMock(return_value=dict(FORM))
    return request


def _invite_manager() -> MagicMock:
    manager = MagicMock()
    manager.validate_auth_method.return_value = True
    manager.get_realm_auth_methods = AsyncMock(return_value={"sso": True, "local": True, "identity_providers": []})
    manager.complete_local_invite = AsyncMock(
        return_value={
            "user_id": "u1",
            "email": "iemand@example.org",
            "created": True,
            "assigned": {"roles": [], "errors": []},
            "verification_mail_sent": True,
        }
    )
    return manager


def _volledig_project(*keys: str) -> dict[str, Any]:
    """Een projectbestand dat de hele poort haalt, met deze uitnodigingssleutels."""
    return {
        "schema-version": 2,
        "name": "demo",
        "services": ["keycloak", {"name": "invite", "config": {"active": [{"key": key} for key in keys]}}],
        "components": [{"name": "api", "type": "single"}],
        "deployments": [
            {
                "name": "deployment-1",
                "cluster": "local",
                "namespace": "demo",
                "components": [{"reference": "api"}],
            }
        ],
    }


def _project_met_sleutels(*keys: str) -> dict[str, Any]:
    return {
        "name": "demo",
        "services": [{"name": "invite", "config": {"active": [{"key": key} for key in keys]}}],
    }


# --- 1. de snelheidsgrens ----------------------------------------------------


@pytest.mark.asyncio
async def test_de_zesde_registratie_binnen_een_minuut_wordt_geweigerd() -> None:
    """Vijf pogingen passen in de emmer, de zesde krijgt 429 en maakt geen gebruiker aan."""
    request = _request(_adres())
    manager = _invite_manager()
    with (
        _verse_emmers(),
        patch("opi.api.invite_routes._find_project_by_invite_key", AsyncMock(return_value=PROJECT)),
        patch("opi.api.invite_routes.InviteManager", return_value=manager),
        patch("opi.api.invite_routes._get_keycloak_url_for_cluster", return_value="https://kc.example.org"),
    ):
        for _ in range(5):
            assert (await invite_register_submit(request, "sleutel-van-genoeg-lengte")).status_code == 302
        geweigerd = await invite_register_submit(request, "sleutel-van-genoeg-lengte")

    assert geweigerd.status_code == 429
    assert manager.complete_local_invite.await_count == 5


@pytest.mark.asyncio
async def test_de_emmer_per_sleutel_houdt_ook_als_elk_verzoek_een_ander_adres_heeft() -> None:
    """Twintig registraties per uur per sleutel, ongeacht van welk adres ze komen.

    Dit is de grens die blijft staan zolang ``IPRateLimiter`` het linker XFF-adres leest
    (issue #195): het adres is per verzoek te kiezen, de sleutel niet.
    """
    manager = _invite_manager()
    sleutel = "sleutel-per-uur-grens"
    with (
        _verse_emmers(),
        patch("opi.api.invite_routes._find_project_by_invite_key", AsyncMock(return_value=PROJECT)),
        patch("opi.api.invite_routes.InviteManager", return_value=manager),
        patch("opi.api.invite_routes._get_keycloak_url_for_cluster", return_value="https://kc.example.org"),
    ):
        for _ in range(20):
            assert (await invite_register_submit(_request(_adres()), sleutel)).status_code == 302
        geweigerd = await invite_register_submit(_request(_adres()), sleutel)

    assert geweigerd.status_code == 429
    assert manager.complete_local_invite.await_count == 20


@pytest.mark.asyncio
@pytest.mark.parametrize("route", [invite_landing, invite_register_form, invite_sso_start])
async def test_de_leesroutes_weigeren_na_de_burst(route: Any) -> None:
    """Tien verzoeken passen in de leesemmer, het elfde krijgt 429."""
    request = _request(_adres())
    opzoeken = AsyncMock(return_value=PROJECT)
    with (
        _verse_emmers(),
        patch("opi.api.invite_routes._find_project_by_invite_key", opzoeken),
        patch("opi.api.invite_routes.InviteManager", return_value=_invite_manager()),
        patch("opi.api.invite_routes._get_keycloak_url_for_cluster", return_value="https://kc.example.org"),
    ):
        for _ in range(10):
            assert (await route(request, "sleutel-van-genoeg-lengte")).status_code != 429
        geweigerd = await route(request, "sleutel-van-genoeg-lengte")

    assert geweigerd.status_code == 429
    assert opzoeken.await_count == 10


def test_de_ingestelde_grenzen_staan_waar_het_plan_ze_zette() -> None:
    """De getallen zelf, want de tests hierboven meten het gedrag van de emmers."""
    per_minuut = pytest.approx(30), 10
    assert (invite_routes._READ_LIMITER.rate * 60, invite_routes._READ_LIMITER.burst) == per_minuut
    assert (invite_routes._REGISTER_PER_MINUTE.rate * 60, invite_routes._REGISTER_PER_MINUTE.burst) == (
        pytest.approx(5),
        5,
    )
    for emmer in (invite_routes._REGISTER_PER_HOUR, invite_routes._REGISTER_PER_KEY_PER_HOUR):
        assert (emmer.rate * 3600, emmer.burst) == (pytest.approx(20), 20)


# --- 2. de sleutel uit de log ------------------------------------------------


@pytest.mark.asyncio
async def test_geen_enkele_logregel_van_de_routes_bevat_de_sleutel(caplog: pytest.LogCaptureFixture) -> None:
    sleutel = "ZeerGeheimeUitnodigingsSleutel"
    manager = _invite_manager()
    with (
        caplog.at_level(logging.DEBUG, logger="opi"),
        patch("opi.api.invite_routes._find_project_by_invite_key", AsyncMock(return_value=PROJECT)),
        patch("opi.api.invite_routes.InviteManager", return_value=manager),
        patch("opi.api.invite_routes._get_keycloak_url_for_cluster", return_value="https://kc.example.org"),
    ):
        await invite_landing(_request(_adres()), sleutel)
        await invite_register_form(_request(_adres()), sleutel)
        await invite_sso_start(_request(_adres()), sleutel)
        await invite_register_submit(_request(_adres()), sleutel)

    regels = caplog.text
    assert sleutel not in regels
    assert fingerprint_for_log(sleutel) in regels


@pytest.mark.asyncio
async def test_het_opzoeken_logt_de_sleutel_niet_als_hij_niet_bestaat(caplog: pytest.LogCaptureFixture) -> None:
    """Het 'niet gevonden'-pad logt op DEBUG en droeg de sleutel letterlijk."""
    sleutel = "BestaatNietMaarIsGeheim"
    with (
        caplog.at_level(logging.DEBUG, logger="opi"),
        patch("opi.api.invite_routes.get_project_store", MagicMock(return_value=MagicMock(get_all=list))),
    ):
        assert await invite_routes._find_project_by_invite_key(sleutel) is None

    assert sleutel not in caplog.text
    assert fingerprint_for_log(sleutel) in caplog.text


def test_geen_logregel_in_de_uitnodigingscode_interpoleert_de_sleutel_nog() -> None:
    """Guard: de sleutel mag in geen enkele ``logger.``-regel van deze twee modules staan."""
    for pad in ("opi/api/invite_routes.py", "opi/manager/invite_manager.py"):
        with open(pad) as bestand:
            for nummer, regel in enumerate(bestand, start=1):
                if "logger." in regel:
                    assert "{key}" not in regel, f"{pad}:{nummer} logt de sleutel letterlijk"


def test_geen_logregel_in_opi_schrijft_een_verzoekpad_zonder_de_omzetting() -> None:
    """Guard: de sleutel staat in het PAD, dus elke logregel met een pad zet hem om.

    Dit is een blanket-regel, ook voor een regel die vandaag geen uitnodigingspad ziet
    (de wizard-guard in ``csrf.py``): een uitzonderingenlijst is duurder te onderhouden dan
    een omzetting die voor elk ander pad niets doet.
    """
    overtredingen = []
    for pad in sorted(Path("opi").rglob("*.py")):
        boom = ast.parse(pad.read_text())
        # Ook een naam die het RUWE pad opbergt telt mee: de regel die de sleutel nog
        # schreef las ``path``, niet ``request.url.path``. Een naam die de omzetting al
        # heeft ondergaan is juist de bedoeling en telt niet mee.
        paddragers = {
            doel.id
            for node in ast.walk(boom)
            if isinstance(node, ast.Assign)
            and "url.path" in (waarde := ast.unparse(node.value))
            and "redact_invite_key_in_path" not in waarde
            for doel in node.targets
            if isinstance(doel, ast.Name)
        }
        for node in ast.walk(boom):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "logger"
            ):
                continue
            for arg in node.args:
                bron = ast.unparse(arg)
                namen = {n.id for n in ast.walk(arg) if isinstance(n, ast.Name)}
                draagt_pad = "url.path" in bron or namen & paddragers
                if draagt_pad and "redact_invite_key_in_path" not in bron:
                    overtredingen.append(f"{pad}:{node.lineno} {bron}")

    assert overtredingen == []


@pytest.mark.asyncio
async def test_een_uitnodigingspad_zonder_route_logt_de_sleutel_niet(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Een getypte of aangevulde uitnodigings-URL matcht geen route, en dat pad werd gelogd.

    De middleware stuurt een niet-gematchte route naar de login en schreef daarbij het hele
    pad, dus een verzoek op ``/invite/<sleutel>/typefout`` zette de sleutel op INFO in de log.
    """
    sleutel = "ZeerGeheimeUitnodigingsSleutel"
    scope = {
        "type": "http",
        "method": "GET",
        "path": f"/invite/{sleutel}/typefout",
        "query_string": b"",
        "headers": [],
        "root_path": "",
        "scheme": "http",
        "server": ("test", 80),
        "client": ("10.0.0.1", 1234),
        "app": MagicMock(router=MagicMock(routes=[])),
        "session": {},
    }
    middleware = AuthorizationMiddleware(app=MagicMock())

    async def volgende(_request: Request) -> Any:
        return MagicMock()

    with (
        caplog.at_level(logging.DEBUG, logger="opi"),
        patch("opi.middleware.authorization.get_user", return_value=None),
    ):
        await middleware.dispatch(Request(scope), volgende)

    assert sleutel not in caplog.text
    assert fingerprint_for_log(sleutel) in caplog.text


def test_het_pad_van_een_uitnodiging_wordt_omgezet_naar_de_vingerafdruk() -> None:
    """De access-log van uvicorn schrijft het hele pad, dus daar staat de sleutel ook in."""
    sleutel = "ZeerGeheimeUitnodigingsSleutel"
    vingerafdruk = fingerprint_for_log(sleutel)

    assert redact_invite_key_in_path(f"/invite/{sleutel}") == f"/invite/{vingerafdruk}"
    assert redact_invite_key_in_path(f"/invite/{sleutel}/register") == f"/invite/{vingerafdruk}/register"
    assert redact_invite_key_in_path(f"/invite/{sleutel}/sso/callback?code=x") == (
        f"/invite/{vingerafdruk}/sso/callback?code=x"
    )
    assert redact_invite_key_in_path("/projects/demo") == "/projects/demo"


def test_het_filter_zet_het_pad_in_de_access_regel_om() -> None:
    sleutel = "ZeerGeheimeUitnodigingsSleutel"
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "uvicorn",
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("10.0.0.1:1", "GET", f"/invite/{sleutel}/register", "1.1", 200),
        None,
    )

    assert logging_config.InviteKeyPathFilter().filter(record) is True
    assert sleutel not in record.getMessage()
    assert fingerprint_for_log(sleutel) in record.getMessage()


def test_het_padfilter_wordt_maar_een_keer_aangehangen() -> None:
    """``setup_logging`` draait twee keer, en dit filter SCHRIJFT in de regel.

    Twee instanties zouden het pad dubbel omzetten, en dan klopt de vingerafdruk in de
    access-log niet meer met die in de regels van de routes.
    """
    doelwit = logging.Filterer()
    doelwit.addFilter(logging_config._INVITE_KEY_PATH_FILTER)
    doelwit.addFilter(logging_config._INVITE_KEY_PATH_FILTER)

    assert doelwit.filters.count(logging_config._INVITE_KEY_PATH_FILTER) == 1


# --- 3. de minimale sleutellengte --------------------------------------------


def test_de_uitnodigingsdienst_doet_mee_aan_de_wijzigingscontrole() -> None:
    assert any(isinstance(service, InviteService) for service in change_validating_services())


@pytest.mark.asyncio
async def test_de_poort_weigert_een_nieuwe_korte_sleutel() -> None:
    """Op de POORT gemeten: de haak is pas iets waard als de lus hem aanroept."""
    with pytest.raises(ProjectIntegrityError, match="uitnodigingssleutel"):
        await validate_project_structure(_volledig_project("te-kort"))


@pytest.mark.asyncio
async def test_de_poort_laat_een_sleutel_door_die_er_al_stond() -> None:
    bestaand = _volledig_project("invulhulpen")

    await validate_project_structure(bestaand, previous=bestaand)


def test_een_nieuwe_sleutel_korter_dan_de_ondergrens_wordt_geweigerd() -> None:
    kort = "a" * (INVITE_KEY_MIN_LENGTH - 1)
    meldingen = InviteService().validate_project_change({}, _project_met_sleutels(kort))

    assert len(meldingen) == 1
    assert str(INVITE_KEY_MIN_LENGTH) in meldingen[0]
    assert kort not in meldingen[0]


def test_een_nieuwe_sleutel_op_de_ondergrens_wordt_geaccepteerd() -> None:
    lang = "a" * INVITE_KEY_MIN_LENGTH
    assert InviteService().validate_project_change({}, _project_met_sleutels(lang)) == []


@pytest.mark.parametrize("bestaand", ["invulhulpen", "welcome-to-docs"])
def test_een_sleutel_die_er_al_stond_blijft_geldig(bestaand: str) -> None:
    """Een andere wijziging aan een uitnodiging met een korte sleutel mag door.

    Dit zijn twee van de vier ECHTE productiesleutels (11 en 15 tekens, zie
    ``tests/test_invite_service.py``). Zouden ze geweigerd worden, dan konden die twee
    projecten hun uitnodiging niet meer opslaan, en verlengen kan de eigenaar niet: dat
    maakt de link die al rondgaat ongeldig.
    """
    vorige = _project_met_sleutels(bestaand)
    nu = _project_met_sleutels(bestaand)
    nu["services"][0]["config"]["active"][0]["contact-email"] = "nieuw@example.org"

    assert InviteService().validate_project_change(vorige, nu) == []


def test_een_tweede_korte_sleutel_naast_een_bestaande_wordt_wel_geweigerd() -> None:
    vorige = _project_met_sleutels("invulhulpen")
    nu = _project_met_sleutels("invulhulpen", "ook-kort")

    assert len(InviteService().validate_project_change(vorige, nu)) == 1
