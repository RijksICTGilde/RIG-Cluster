"""Welke aanroepers de verificatie overslaan, en dat het er precies die zijn (RC-191).

``create_user`` laat sinds RC-191 iedereen met een adres eerst bevestigen. De uitzondering
hoort zichtbaar te staan bij wie hem nodig heeft, en niet als verborgen
``if realm_name == "master"`` in de connector. Dat maakt hem ook meetbaar: dit is de sweep
over ALLE aanroepen in ``opi/``, want een vierde aanroeper die de vlag meekrijgt haalt de
grendel voor die weg net zo stil weg als een ``if`` in de connector.

De reden dat de master-beheerders hem nodig hebben: hun adres is ``{admin}@local.invalid``
of ``{username}@localhost`` en bestaat niet. Verificatie afdwingen sluit elke
projectbeheerder buiten zijn eigen realm.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

OPI = Path(__file__).resolve().parents[1] / "opi"

#: De aanroepen die de verificatie overslaan: (bestand, realm_name) -> hoeveel.
#: Het AANTAL telt mee, want ``keycloak_manager`` maakt de realm-admin op twee plekken aan
#: (bij het inrichten en bij de OTP-retrofit) en een set zou het verlies van een van de
#: twee niet zien.
VERWACHTE_UITZONDERINGEN = {
    ("bootstrap/keycloak_setup.py", "master"): 1,
    ("manager/keycloak_manager.py", "master"): 2,
}


def _keyword(call: ast.Call, naam: str) -> ast.expr | None:
    return next((kw.value for kw in call.keywords if kw.arg == naam), None)


def _create_user_calls() -> list[tuple[str, ast.Call]]:
    """Elke ``<iets>.create_user(...)``-aanroep in opi/, met zijn pad."""
    gevonden: list[tuple[str, ast.Call]] = []
    for pad in sorted(OPI.rglob("*.py")):
        boom = ast.parse(pad.read_text())
        gevonden.extend(
            (str(pad.relative_to(OPI)), knoop)
            for knoop in ast.walk(boom)
            if isinstance(knoop, ast.Call)
            and isinstance(knoop.func, ast.Attribute)
            and knoop.func.attr == "create_user"
        )
    return gevonden


def _keycloak_calls() -> list[tuple[str, ast.Call]]:
    """De Keycloak-aanroepen: alleen die kennen ``realm_name``.

    postgres, minio en de gebruikersbeheerdienst hebben een eigen ``create_user`` met een
    heel andere handtekening.
    """
    return [(pad, call) for pad, call in _create_user_calls() if _keyword(call, "realm_name") is not None]


def test_er_zijn_keycloak_aanroepen_om_te_meten() -> None:
    """Zonder deze regel zou een hernoeming deze hele toets stil groen laten."""
    assert len(_keycloak_calls()) >= 5


def test_alleen_de_master_beheerders_slaan_de_verificatie_over() -> None:
    uitzonderingen: Counter[tuple[str, str]] = Counter()
    for pad, call in _keycloak_calls():
        vlag = _keyword(call, "skip_email_verification")
        if vlag is None:
            continue
        letterlijk_waar = isinstance(vlag, ast.Constant) and vlag.value is True
        assert letterlijk_waar, f"{pad}: skip_email_verification hoort een letterlijke True te zijn"
        realm = _keyword(call, "realm_name")
        assert isinstance(realm, ast.Constant), f"{pad}: een uitzondering hoort op een VASTE realm te staan"
        uitzonderingen[(pad, realm.value)] += 1

    assert dict(uitzonderingen) == VERWACHTE_UITZONDERINGEN


def test_geen_enkele_andere_aanroep_zet_de_vlag() -> None:
    """De invite-weg is de reden dat deze taak bestaat; die mag hem nooit meekrijgen."""
    for pad, call in _keycloak_calls():
        if pad in {p for p, _ in VERWACHTE_UITZONDERINGEN}:
            continue
        assert _keyword(call, "skip_email_verification") is None, f"{pad} slaat de bevestiging over"


def test_de_connector_kent_geen_verborgen_uitzondering_op_master() -> None:
    bron = (OPI / "connectors" / "keycloak.py").read_text()
    create_user = bron[bron.index("    async def create_user(") : bron.index("    async def send_verify_email(")]

    assert 'realm_name == "master"' not in create_user
    assert "skip_email_verification" in create_user
