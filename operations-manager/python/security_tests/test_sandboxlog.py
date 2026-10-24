"""Het logboek van de proefsandbox: records, vensters, en het oordeel per ASGI-fout.

Zonder een proef hierop kan een bevinding stil zonder OPI-logregels in het rapport komen, of
kan een ``Exception in ASGI application`` die wel een bevinding is als verwacht worden
weggezet. Deze module leest en schrijft alleen bestanden, dus er is geen sandbox voor nodig.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from security_tests.sandboxlog import (
    asgi_fouten,
    beoordeel,
    bestand_van,
    junit_sleutel,
    laatste_regels,
    lees,
    markeer,
    route_uit,
    toetsnaam,
    vensters,
    waarom,
)

if TYPE_CHECKING:
    from pathlib import Path

_GAIERROR = """2026-10-23 07:45:01,101 ERROR zad.proef verzoek GET /api/v2/subdomein viel om op ExceptionGroup
2026-10-23 07:45:01,102 ERROR uvicorn.error Exception in ASGI application
Traceback (most recent call last):
  File "starlette/_utils.py", line 76, in collapse_excgroups
    yield
  + Exception Group Traceback (most recent call last):
  |   File "asyncpg/connect_utils.py", line 1075, in __connect_addr
  | ExceptionGroup: unhandled errors in a TaskGroup (1 sub-exception)
  +-+---------------- 1 ----------------
    | socket.gaierror: [Errno -2] Name or service not known
    +------------------------------------
"""


def _schrijf(pad: Path, tekst: str) -> Path:
    pad.write_text(tekst)
    return pad


def test_lees_knoopt_een_traceback_aan_zijn_kopregel(tmp_path: Path) -> None:
    records = lees(_schrijf(tmp_path / "opi.log", _GAIERROR))
    assert len(records) == 2, "de traceback hoort bij de ERROR-regel, niet als eigen records"
    assert records[1].niveau == "ERROR"
    assert records[1].tijd == "2026-10-23 07:45:01,102"
    assert "socket.gaierror" in records[1].tekst


def test_lees_maskeert_een_geheim_in_een_logregel(tmp_path: Path) -> None:
    """Faalconditie: een sleutel uit een logregel van OPI komt letterlijk in het rapport."""
    regel = "2026-10-23 07:45:01,101 INFO opi.web.router X-API-Key: zeer-geheime-sleutel\n"
    records = lees(_schrijf(tmp_path / "opi.log", regel))
    assert "zeer-geheime-sleutel" not in records[0].tekst
    assert "REDACTED" in records[0].tekst


def test_vensters_geeft_per_proef_alleen_zijn_eigen_regels(tmp_path: Path) -> None:
    pad = tmp_path / "opi.log"
    pad.write_text("")
    markeer("security_tests/test_a.py::test_een", "start", pad)
    with pad.open("a") as bestand:
        bestand.write("2026-10-23 07:45:01,101 ERROR opi.web.router iets van test_een\n")
    markeer("security_tests/test_a.py::test_een", "einde", pad)
    with pad.open("a") as bestand:
        bestand.write("2026-10-23 07:45:02,101 ERROR opi.web.router buiten elk venster\n")
    markeer("security_tests/test_a.py::test_twee", "start", pad)
    markeer("security_tests/test_a.py::test_twee", "einde", pad)

    per_proef = vensters(lees(pad))
    assert [record.kop for record in per_proef["security_tests/test_a.py::test_een"]] == [
        "2026-10-23 07:45:01,101 ERROR opi.web.router iets van test_een"
    ]
    assert per_proef["security_tests/test_a.py::test_twee"] == []


def test_markeer_zonder_proefmap_doet_niets(tmp_path: Path) -> None:
    """De fuzzers kunnen zonder sandbox draaien; dan is er geen logboek om in te schrijven."""
    pad = tmp_path / "bestaat-niet" / "opi.log"
    markeer("security_tests/test_a.py::test_een", "start", pad)
    assert not pad.exists()


def test_junit_sleutel_is_de_nodeid_van_pytest() -> None:
    assert junit_sleutel("security_tests.test_uploads", "test_x[1]") == "security_tests/test_uploads.py::test_x[1]"


def test_waarom_neemt_de_regels_voor_een_fout_mee_als_context(tmp_path: Path) -> None:
    pad = tmp_path / "opi.log"
    pad.write_text(
        "2026-10-23 07:45:00,001 INFO opi.web.router een verzoek kwam binnen\n"
        "2026-10-23 07:45:00,002 INFO opi.web.router en nog een regel\n"
        "2026-10-23 07:45:01,101 ERROR opi.web.router het ging mis\n"
    )
    regels = waarom(lees(pad))
    assert "het ging mis" in regels[-1]
    assert any("een verzoek kwam binnen" in regel for regel in regels), "context voor de fout hoort erbij"


def test_waarom_zonder_fout_levert_geen_regels(tmp_path: Path) -> None:
    """Negatief voorbeeld: een venster zonder WARNING of erger hoort leeg te blijven."""
    pad = _schrijf(tmp_path / "opi.log", "2026-10-23 07:45:00,001 INFO opi.web.router alles goed\n")
    assert waarom(lees(pad)) == ()


def test_route_uit_leest_de_route_van_het_omgevallen_verzoek(tmp_path: Path) -> None:
    assert route_uit(lees(_schrijf(tmp_path / "opi.log", _GAIERROR))) == "GET /api/v2/subdomein"


def test_route_uit_zonder_verzoekregel_is_onbekend(tmp_path: Path) -> None:
    pad = _schrijf(tmp_path / "opi.log", "2026-10-23 07:45:00,001 ERROR opi.web.router zonder route\n")
    assert route_uit(lees(pad)) == "onbekend"


def test_een_ontbrekende_database_is_de_verwachte_uitkomst(tmp_path: Path) -> None:
    fouten = asgi_fouten(lees(_schrijf(tmp_path / "opi.log", _GAIERROR)))
    assert len(fouten) == 1
    assert fouten[0].verwacht
    assert fouten[0].oordeel == "verwachte uitkomst van de wegwerp-sandbox"
    assert fouten[0].route == "GET /api/v2/subdomein"
    assert "geen database" in fouten[0].reden


def test_een_andere_oorzaak_is_een_bevinding(tmp_path: Path) -> None:
    """Faalconditie: elke ASGI-fout wordt als verwacht weggezet en de gate blijft groen."""
    tekst = (
        "2026-10-23 07:45:01,102 ERROR uvicorn.error Exception in ASGI application\n"
        "Traceback (most recent call last):\n"
        "  File 'opi/web/router.py', line 1, in iets\n"
        "ValueError: invalid literal\n"
    )
    fout = asgi_fouten(lees(_schrijf(tmp_path / "opi.log", tekst)))[0]
    assert not fout.verwacht
    assert fout.oordeel == "BEVINDING"
    assert "ValueError" in fout.reden


def test_een_asgi_fout_zonder_traceback_is_geen_verklaring(tmp_path: Path) -> None:
    tekst = "2026-10-23 07:45:01,102 ERROR uvicorn.error Exception in ASGI application\n"
    fout = asgi_fouten(lees(_schrijf(tmp_path / "opi.log", tekst)))[0]
    assert not fout.verwacht
    assert fout.oorzaken == ()
    assert "niet vast te stellen" in fout.reden


def test_beoordeel_laat_een_wrapper_zonder_verwachte_oorzaak_niet_door() -> None:
    verwacht, reden = beoordeel(("ExceptionGroup",))
    assert not verwacht
    assert "ExceptionGroup" in reden


def test_asgi_fouten_noemt_de_proef_waarin_ze_vielen(tmp_path: Path) -> None:
    pad = tmp_path / "opi.log"
    pad.write_text("")
    markeer("security_tests/test_stortvloed.py::test_limiter", "start", pad)
    with pad.open("a") as bestand:
        bestand.write(_GAIERROR)
    markeer("security_tests/test_stortvloed.py::test_limiter", "einde", pad)
    assert asgi_fouten(lees(pad))[0].proef == "security_tests/test_stortvloed.py::test_limiter"


def test_laatste_regels_geeft_het_staartje_voor_een_crash(tmp_path: Path) -> None:
    pad = _schrijf(
        tmp_path / "opi.log",
        "".join(f"2026-10-23 07:45:00,00{nummer} INFO opi.core.startup regel {nummer}\n" for nummer in range(5)),
    )
    assert laatste_regels(pad, aantal=2) == (
        "2026-10-23 07:45:00,003 INFO opi.core.startup regel 3",
        "2026-10-23 07:45:00,004 INFO opi.core.startup regel 4",
    )


def test_laatste_regels_zonder_logboek_is_leeg(tmp_path: Path) -> None:
    assert laatste_regels(tmp_path / "bestaat-niet.log") == ()


def test_junit_sleutel_zet_een_klasse_niet_in_het_pad() -> None:
    """Faalconditie: de klasse wordt een map, en dan vindt het venster zijn proef niet terug."""
    assert junit_sleutel("security_tests.test_zap.TestPas", "test_a") == "security_tests/test_zap.py::TestPas::test_a"
    assert bestand_van("security_tests.test_zap.TestPas") == "test_zap"
    assert toetsnaam("security_tests.test_zap.TestPas", "test_a") == "test_zap::TestPas::test_a"
    assert toetsnaam("security_tests.test_uploads", "test_a") == "test_uploads::test_a"
