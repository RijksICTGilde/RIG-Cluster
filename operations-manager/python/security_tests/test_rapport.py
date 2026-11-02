"""Het rapport: lezen van het junit-bestand van de fuzzers, en de weergave als markdown.

Deze module heeft geen draaiende sandbox nodig: ``uit_junit`` en ``telling_uit_junit`` lezen
een bestand, en ``render``/``dekkingstabel`` zetten gegevens om in tekst. Zonder een proef
hierop blijft onopgemerkt als een bevinding uit het junit-rapport zoekraakt of als de
dekkingstabel een doel uit de BIV-inventaris mist.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import TYPE_CHECKING

from security_tests.rapport import (
    AFWIJKING,
    BEVINDING_REGEL,
    GROEN,
    NIET_GEMETEN,
    PROEVEN,
    Bevinding,
    Crash,
    Regel,
    Telling,
    dekkingstabel,
    groene_lijst,
    regels_uit_junit,
    regels_uit_zap,
    render,
    telling_uit_junit,
    uit_junit,
)
from security_tests.sandboxlog import AsgiFout, lees, vensters
from security_tests.zap_scan import ZapUitkomst

if TYPE_CHECKING:
    from pathlib import Path


def _schrijf_junit(pad: Path, testsuites: list[dict]) -> None:
    """Een junit-rapport zoals pytest het schrijft, met zoveel velden als een proef gebruikt."""
    wortel = ET.Element("testsuites")
    for suite in testsuites:
        element = ET.SubElement(
            wortel,
            "testsuite",
            {"tests": str(suite["tests"]), "skipped": str(suite.get("skipped", 0))},
        )
        for geval in suite.get("gevallen", []):
            testcase = ET.SubElement(
                element,
                "testcase",
                {"classname": geval["classname"], "name": geval["name"]},
            )
            if "failure" in geval:
                fout = ET.SubElement(testcase, "failure")
                if geval["failure"].get("message") is not None:
                    fout.set("message", geval["failure"]["message"])
                if geval["failure"].get("text") is not None:
                    fout.text = geval["failure"]["text"]
            if "error" in geval:
                fout = ET.SubElement(testcase, "error", {"message": geval["error"]})
            if "skipped" in geval:
                ET.SubElement(testcase, "skipped", {"message": geval["skipped"]})
    ET.ElementTree(wortel).write(pad)


def test_uit_junit_leest_een_mislukte_testcase(tmp_path: Path) -> None:
    pad = tmp_path / "fuzzers.xml"
    _schrijf_junit(
        pad,
        [
            {
                "tests": 2,
                "skipped": 0,
                "gevallen": [
                    {
                        "classname": "security_tests.test_uploads",
                        "name": "test_een_grote_upload[1]",
                        "failure": {"message": "AssertionError: geheugen liep vol"},
                    },
                    {"classname": "security_tests.test_uploads", "name": "test_normaal"},
                ],
            }
        ],
    )
    bevindingen = uit_junit(pad)
    assert bevindingen == [
        Bevinding(
            proef="test_uploads",
            naam="test_een_grote_upload[1]",
            melding="AssertionError: geheugen liep vol",
            reproductie="uv run pytest security_tests/test_uploads.py -k 'test_een_grote_upload'",
        )
    ]


def test_uit_junit_haalt_de_melding_uit_de_tekst_zonder_attribuut(tmp_path: Path) -> None:
    """Faalconditie: een failure zonder ``message``-attribuut levert een lege melding op."""
    pad = tmp_path / "fuzzers.xml"
    _schrijf_junit(
        pad,
        [
            {
                "tests": 1,
                "gevallen": [
                    {
                        "classname": "security_tests.test_injectie",
                        "name": "test_payload",
                        "failure": {"text": "  AssertionError: payload bereikte kubectl  \n"},
                    }
                ],
            }
        ],
    )
    assert uit_junit(pad)[0].melding == "AssertionError: payload bereikte kubectl"


def test_uit_junit_neemt_een_error_mee_als_bevinding(tmp_path: Path) -> None:
    pad = tmp_path / "fuzzers.xml"
    _schrijf_junit(
        pad,
        [
            {
                "tests": 1,
                "gevallen": [
                    {
                        "classname": "security_tests.test_stortvloed",
                        "name": "test_gok_weerbaarheid",
                        "error": "ConnectionError: sandbox niet bereikbaar",
                    }
                ],
            }
        ],
    )
    bevinding = uit_junit(pad)[0]
    assert bevinding.proef == "test_stortvloed"
    assert bevinding.melding == "ConnectionError: sandbox niet bereikbaar"


def test_uit_junit_slaat_een_geslaagde_testcase_over(tmp_path: Path) -> None:
    """Negatief voorbeeld: een geslaagde testcase levert geen bevinding op."""
    pad = tmp_path / "fuzzers.xml"
    _schrijf_junit(
        pad,
        [{"tests": 1, "gevallen": [{"classname": "security_tests.test_zap", "name": "test_ok"}]}],
    )
    assert uit_junit(pad) == []


def test_telling_uit_junit_telt_over_meerdere_testsuites(tmp_path: Path) -> None:
    pad = tmp_path / "fuzzers.xml"
    _schrijf_junit(pad, [{"tests": 5, "skipped": 2}, {"tests": 3, "skipped": 0}])
    telling = telling_uit_junit(pad)
    assert telling == Telling(toetsen=8, overgeslagen=2)
    assert telling.gemeten == 6


def test_telling_uit_junit_zonder_bestand_is_leeg(tmp_path: Path) -> None:
    """Faalconditie: een ontbrekend rapport zou verward kunnen worden met een lege, groene run."""
    telling = telling_uit_junit(tmp_path / "bestaat-niet.xml")
    assert telling == Telling(0, 0)
    assert telling.gemeten == 0


def test_dekkingstabel_noemt_elk_doel_van_de_aanvalsproef_uit_de_inventaris() -> None:
    """De dekkingstabel leest dezelfde doelenlijst als de BIV-inventaris, niet een eigen kopie."""
    regels = dekkingstabel()
    inhoud = "\n".join(regels)
    kop = regels.index("| component | beproefd door | beproefbaar tegen |")
    assert regels[kop + 1] == "|---|---|---|"
    # "opi" wordt door meerdere proeven gedekt volgens PROEVEN; die namen staan alfabetisch.
    opi_regel = next(regel for regel in regels if regel.startswith("| opi "))
    for naam, proef in PROEVEN.items():
        if "opi" in proef.componenten:
            assert naam in opi_regel
    # Een doel dat geen proef in PROEVEN noemt, komt expliciet als gat terug, niet als lege cel.
    assert "nog niet beproefd" in inhoud


def test_dekkingstabel_is_deterministisch() -> None:
    """Faalconditie: de volgorde van proeven per component wisselt tussen runs."""
    assert dekkingstabel() == dekkingstabel()


def test_bevinding_details_valt_terug_op_onbekend_voor_een_vreemde_proefnaam() -> None:
    bevinding = Bevinding(proef="geen-bestaande-proef", naam="x", melding="y", reproductie="z")
    assert bevinding.details is None


def test_render_noemt_bevindingen_met_hun_biv_as_en_reproductiepad() -> None:
    bevinding = Bevinding(
        proef="test_uploads",
        naam="test_een_grote_upload",
        melding="AssertionError: geheugen liep vol\nmeer context",
        reproductie="uv run pytest security_tests/test_uploads.py -k 'test_een_grote_upload'",
    )
    tekst = render(
        [bevinding], ZapUitkomst(gemeten=False, reden="docker ontbreekt"), Telling(toetsen=4, overgeslagen=0)
    )
    assert "## Bevindingen" in tekst
    assert "### test_uploads: test_een_grote_upload" in tekst
    assert "wat: AssertionError: geheugen liep vol" in tekst
    assert "meer context" not in tekst, "alleen de eerste regel van de melding hoort in het rapport"
    assert f"- BIV-as: {PROEVEN['test_uploads'].as_naam}" in tekst
    assert bevinding.reproductie in tekst
    assert "NIET GEMETEN: docker ontbreekt" in tekst
    assert "## Dekking van de aanvalsproef-doelen" in tekst


def test_render_zonder_bevindingen_laat_de_bevindingensectie_weg() -> None:
    """Negatief voorbeeld: een schone run schrijft geen '## Bevindingen'-kop."""
    tekst = render([], ZapUitkomst(gemeten=True, meldingen=[]), Telling(toetsen=10, overgeslagen=0))
    assert "## Bevindingen" not in tekst
    assert "gerichte proeven: 10 gemeten van 10 (0 overgeslagen)" in tekst
    assert "ZAP-basislaag: gemeten, 0 meldingen" in tekst


def test_render_meldt_zap_bevindingen_in_een_eigen_sectie() -> None:
    tekst = render(
        [],
        ZapUitkomst(gemeten=True, meldingen=["NIEUW 10038 Content Security Policy Header Not Set: ..."]),
        Telling(toetsen=1, overgeslagen=0),
    )
    assert "## ZAP" in tekst
    assert "- NIEUW 10038 Content Security Policy Header Not Set: ..." in tekst


def test_uit_junit_hangt_de_opi_logregels_aan_de_bevinding(tmp_path: Path) -> None:
    """Faalconditie: een bevinding komt zonder het WAAROM uit OPI in het rapport."""
    log = tmp_path / "opi.log"
    log.write_text(
        "2026-10-23 07:45:00,001 INFO zad.proef marker start security_tests/test_uploads.py::test_een_grote_upload\n"
        "2026-10-23 07:45:00,002 ERROR zad.proef verzoek POST /api/v2/upload gaf 500\n"
        "2026-10-23 07:45:00,003 INFO zad.proef marker einde security_tests/test_uploads.py::test_een_grote_upload\n"
        "2026-10-23 07:45:00,004 ERROR opi.web.router buiten het venster van deze proef\n"
    )
    pad = tmp_path / "fuzzers.xml"
    _schrijf_junit(
        pad,
        [
            {
                "tests": 1,
                "gevallen": [
                    {
                        "classname": "security_tests.test_uploads",
                        "name": "test_een_grote_upload",
                        "failure": {"message": "AssertionError: 500 waar 413 hoort"},
                    }
                ],
            }
        ],
    )
    bevinding = uit_junit(pad, vensters(lees(log)))[0]
    assert bevinding.route == "POST /api/v2/upload"
    assert any("gaf 500" in regel for regel in bevinding.logregels)
    assert not any("buiten het venster" in regel for regel in bevinding.logregels)

    tekst = render([bevinding], ZapUitkomst(gemeten=True, meldingen=[]), Telling(toetsen=1, overgeslagen=0))
    assert "- route: POST /api/v2/upload" in tekst
    assert "waarom OPI zwichtte" in tekst
    assert "gaf 500" in tekst


def test_render_zonder_logregels_zegt_dat_opi_niets_logde() -> None:
    """Negatief voorbeeld: een leeg venster mag niet als een lege codeblok in het rapport komen."""
    bevinding = Bevinding("test_grendel", "test_x", "AssertionError", "uv run ...")
    tekst = render([bevinding], ZapUitkomst(gemeten=True, meldingen=[]), Telling(toetsen=1, overgeslagen=0))
    assert "OPI logde niets in het venster van deze proef" in tekst


def test_render_groepeert_dezelfde_asgi_fout_en_noemt_het_oordeel() -> None:
    fouten = [
        AsgiFout("test_stortvloed.py::test_limiter", tijd, "GET /check", ("socket.gaierror",), True, "geen database")
        for tijd in ("07:45:01", "07:45:02")
    ]
    tekst = render([], ZapUitkomst(gemeten=True, meldingen=[]), Telling(toetsen=1, overgeslagen=0), fouten)
    assert "## Exception in ASGI application" in tekst
    assert "2 keer in deze run, waarvan 0 een bevinding" in tekst
    assert "- 2x, 07:45:01 tot 07:45:02 `GET /check`" in tekst
    assert "verwachte uitkomst van de wegwerp-sandbox" in tekst


def test_render_noemt_een_onverklaarde_asgi_fout_als_bevinding() -> None:
    """Faalconditie: een ASGI-fout met een andere oorzaak leest als de verwachte uitkomst."""
    fout = AsgiFout("buiten een proefvenster", "07:45:01", "GET /x", ("ValueError",), False, "hoort hier niet")
    tekst = render([], ZapUitkomst(gemeten=True, meldingen=[]), Telling(toetsen=1, overgeslagen=0), [fout])
    assert "1 keer in deze run, waarvan 1 een bevinding" in tekst
    assert "BEVINDING, hoort hier niet" in tekst


def test_render_meldt_een_crash_met_exitcode_en_de_laatste_logregels() -> None:
    tekst = render(
        [],
        ZapUitkomst(gemeten=True, meldingen=[]),
        Telling(toetsen=0, overgeslagen=0),
        [],
        Crash(exitcode=2, laatste_regels=("2026-10-23 07:45:00,001 ERROR opi.server laatste woord",)),
    )
    assert "## Crash van de proef zelf" in tekst
    assert "exitcode 2" in tekst
    assert "laatste woord" in tekst


def test_de_groene_lijst_zegt_per_toets_wat_eruit_kwam(tmp_path: Path) -> None:
    """Een geslaagde toets is groen, een overgeslagen toets is niet gemeten met de reden."""
    pad = tmp_path / "fuzzers.xml"
    _schrijf_junit(
        pad,
        [
            {
                "tests": 3,
                "skipped": 1,
                "gevallen": [
                    {"classname": "security_tests.test_uploads", "name": "test_groot"},
                    {
                        "classname": "security_tests.test_sandboxdoel",
                        "name": "test_netpol",
                        "skipped": "te meten met task netpol:meet",
                    },
                    {
                        "classname": "security_tests.test_schemathesis",
                        "name": "test_vreemde",
                        "failure": {"message": "laat een vreemde door met 200"},
                    },
                ],
            }
        ],
    )
    regels = regels_uit_junit(pad)
    assert Regel("gerichte proeven", "test_uploads::test_groot", GROEN, "") in regels
    niet_gemeten = next(regel for regel in regels if regel.uitkomst == NIET_GEMETEN)
    assert niet_gemeten.toelichting == "te meten met task netpol:meet", "een skip zonder reden leest als groen"
    bevinding = next(regel for regel in regels if regel.uitkomst == BEVINDING_REGEL)
    assert bevinding.laag == "schemathesis"


def test_een_warn_van_zap_staat_in_de_lijst_met_zijn_uitleg() -> None:
    """Faalconditie: een WARN met een reden in de baseline staat nergens, of leest als groen."""
    zap = ZapUitkomst(
        gemeten=True,
        bevindingen={
            "ongeauthenticeerd/10023:/openapi.json": "Information Disclosure (risico Low)",
            "ongeauthenticeerd/90004:/version": "Cross-Origin-Resource-Policy (risico Low)",
        },
        informatie={"ongeauthenticeerd/10015:/about": "Cache-control (risico Informational)"},
    )
    regels = {regel.regel: regel for regel in regels_uit_zap(zap)}
    # 10023 staat met een reden in bekende-bevindingen.tsv, 90004 niet (die is opgelost).
    assert regels["ongeauthenticeerd 10023:/openapi.json"].uitkomst == AFWIJKING
    assert regels["ongeauthenticeerd 10023:/openapi.json"].toelichting, "een afwijking zonder reden is geen uitleg"
    assert regels["ongeauthenticeerd 90004:/version"].uitkomst == BEVINDING_REGEL
    assert regels["ongeauthenticeerd 10015:/about"].uitkomst == "informatie"


def test_een_niet_gemeten_zaplaag_staat_als_regel_in_de_lijst() -> None:
    zap = ZapUitkomst(gemeten=False, reden="docker staat niet op het PATH")
    assert regels_uit_zap(zap) == [Regel("ZAP", "alle passen", NIET_GEMETEN, "docker staat niet op het PATH")]


def test_de_groene_lijst_telt_per_laag_hoeveel_groen_is() -> None:
    regels = [
        Regel("ZAP", "a", GROEN),
        Regel("ZAP", "b", BEVINDING_REGEL, "iets"),
        Regel("gerichte proeven", "c", GROEN),
    ]
    tekst = "\n".join(groene_lijst(regels))
    assert "### ZAP (1 groen van 2)" in tekst
    assert "### gerichte proeven (1 groen van 1)" in tekst


def test_een_pas_zonder_herkenning_staat_als_niet_gemeten_in_de_lijst() -> None:
    """Faalconditie: een bewust ongemeten pas staat nergens, of staat er als bevinding."""
    zap = ZapUitkomst(gemeten=True, niet_gemeten={"projectlid": "geen proefproject met een API-sleutel"})
    regels = regels_uit_zap(zap)
    assert regels == [Regel("ZAP", "projectlid alle regels", NIET_GEMETEN, "geen proefproject met een API-sleutel")]


def test_een_uitleg_die_niets_dekt_staat_met_zijn_kenmerk_in_de_lijst() -> None:
    """Faalconditie: de regel noemt alleen de pasnaam, dus een lezer ziet niet welke uitleg weg moet."""
    zap = ZapUitkomst(gemeten=True, verouderd={"ongeauthenticeerd/10038:/weg": "deze bevinding is er niet meer"})
    regels = regels_uit_zap(zap)
    assert len(regels) == 1
    assert regels[0].regel == "ongeauthenticeerd 10038:/weg"
    assert regels[0].uitkomst == BEVINDING_REGEL
    assert "OPGELOST" in regels[0].toelichting


def test_een_toets_in_een_klasse_staat_in_de_laag_van_zijn_bestand(tmp_path: Path) -> None:
    """Faalconditie: de klasse wordt voor het bestand aangezien, en dan valt de laag weg."""
    pad = tmp_path / "fuzzers.xml"
    _schrijf_junit(
        pad,
        [{"tests": 1, "gevallen": [{"classname": "security_tests.test_zap.TestPas", "name": "test_actief"}]}],
    )
    assert regels_uit_junit(pad) == [Regel("ZAP-lezing", "test_zap::TestPas::test_actief", GROEN, "")]
