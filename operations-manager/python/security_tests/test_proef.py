"""De ene ingang: de exitcode die beslist of ``task security-proof`` als gate slaagt.

``main`` zet een echte sandbox op en draait de fuzzers in een eigen pytest-subprocess; dat
hoort bij ``task security-proof`` zelf. Wat hier staat is de beslissing erna: elke stap die
``main`` samenbrengt (de sandbox, de fuzzers, ZAP) wordt nagemaakt, zodat de exitcode-logica
zelf te beproeven is, los van of er een sandbox of docker beschikbaar is.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from security_tests import proef
from security_tests.doel import Doel
from security_tests.rapport import Bevinding, Telling
from security_tests.sandboxlog import AsgiFout
from security_tests.zap_scan import ZapUitkomst

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    import pytest

#: Een doel is hier alleen een plaatshouder: huidig_doel() is nagemaakt en draait geen
#: verzoek naar een echte sandbox.
_DOEL = Doel(url="http://127.0.0.1:8311", token="x", pid=0, sleutels={}, sessies={}, aanroeplog=None)  # type: ignore[arg-type]


@contextlib.contextmanager
def _nep_sandbox() -> Iterator[str]:
    yield _DOEL.url


def _stub(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    fuzzercode: int,
    bevindingen: list[Bevinding],
    telling: Telling,
    zap: ZapUitkomst,
    asgi: list[AsgiFout] | None = None,
    laatste_regels: tuple[str, ...] = (),
) -> None:
    monkeypatch.setattr(proef, "JUNIT", tmp_path / "fuzzers.xml")
    monkeypatch.setattr(proef, "RAPPORT", tmp_path / "aanvalsproef.md")
    monkeypatch.setattr(proef, "proefsandbox", _nep_sandbox)
    monkeypatch.setattr(proef, "huidig_doel", lambda: _DOEL)
    monkeypatch.setattr(proef, "draai_fuzzers", lambda: fuzzercode)
    monkeypatch.setattr(proef.zap_scan, "draai", lambda _doel: zap)
    monkeypatch.setattr(proef, "uit_junit", lambda _pad, _vensters=None: bevindingen)
    monkeypatch.setattr(proef, "telling_uit_junit", lambda _pad: telling)
    monkeypatch.setattr(proef.sandboxlog, "lees", lambda _pad: [])
    monkeypatch.setattr(proef.sandboxlog, "vensters", lambda _records: {})
    monkeypatch.setattr(proef.sandboxlog, "asgi_fouten", lambda _records: asgi or [])
    monkeypatch.setattr(proef.sandboxlog, "laatste_regels", lambda _pad: laatste_regels)


def test_een_schone_run_eindigt_groen(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _stub(
        monkeypatch,
        tmp_path,
        fuzzercode=0,
        bevindingen=[],
        telling=Telling(toetsen=12, overgeslagen=0),
        zap=ZapUitkomst(gemeten=True, meldingen=[]),
    )
    assert proef.main() == 0
    assert (tmp_path / "aanvalsproef.md").exists(), "het rapport hoort ook op een groene run te staan"


def test_een_bevinding_uit_de_fuzzers_eindigt_rood(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Faalconditie: een echte bevinding wordt niet doorgegeven als rode exitcode."""
    _stub(
        monkeypatch,
        tmp_path,
        fuzzercode=1,
        bevindingen=[Bevinding("test_uploads", "test_x", "geheugen liep vol", "uv run ...")],
        telling=Telling(toetsen=12, overgeslagen=0),
        zap=ZapUitkomst(gemeten=True, meldingen=[]),
    )
    assert proef.main() == 1


def test_een_zap_melding_eindigt_ook_rood(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _stub(
        monkeypatch,
        tmp_path,
        fuzzercode=0,
        bevindingen=[],
        telling=Telling(toetsen=12, overgeslagen=0),
        zap=ZapUitkomst(gemeten=True, meldingen=["NIEUW 10038 ..."]),
    )
    assert proef.main() == 1


def test_niets_gemeten_eindigt_rood_ook_zonder_bevindingen(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Faalconditie: alles overslaan (bijv. de grendel herkende het doel niet) leest als groen."""
    _stub(
        monkeypatch,
        tmp_path,
        fuzzercode=0,
        bevindingen=[],
        telling=Telling(toetsen=5, overgeslagen=5),
        zap=ZapUitkomst(gemeten=True, meldingen=[]),
    )
    assert proef.main() == proef.NIET_GEMETEN


def test_een_pytest_uitslag_buiten_0_of_1_eindigt_rood(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Faalconditie: pytest zelf omvalt (exitcode 2+) en dat leest als een schone, lege run."""
    _stub(
        monkeypatch,
        tmp_path,
        fuzzercode=2,
        bevindingen=[],
        telling=Telling(toetsen=12, overgeslagen=0),
        zap=ZapUitkomst(gemeten=True, meldingen=[]),
    )
    assert proef.main() == proef.NIET_GEMETEN


def test_een_ongemeten_zap_laag_eindigt_niet_groen(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Faalconditie: de gate eindigt op 0 terwijl de ZAP-laag niets heeft kunnen meten."""
    _stub(
        monkeypatch,
        tmp_path,
        fuzzercode=0,
        bevindingen=[],
        telling=Telling(toetsen=12, overgeslagen=0),
        zap=ZapUitkomst(gemeten=False, reden="docker staat niet op het PATH"),
    )
    assert proef.main() == proef.NIET_GEMETEN


def test_een_bevinding_gaat_voor_op_niet_gemeten(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Een bevinding is het sterkste signaal, ook als er daarnaast een laag niets meette."""
    _stub(
        monkeypatch,
        tmp_path,
        fuzzercode=1,
        bevindingen=[Bevinding("test_uploads", "test_x", "geheugen liep vol", "uv run ...")],
        telling=Telling(toetsen=12, overgeslagen=0),
        zap=ZapUitkomst(gemeten=False, reden="docker staat niet op het PATH"),
    )
    assert proef.main() == proef.BEVINDING


def test_een_onverklaarde_asgi_fout_eindigt_rood_zonder_andere_bevinding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Faalconditie: een onverwachte ASGI-fout wordt niet doorgegeven als bevinding."""
    fout = AsgiFout("buiten een proefvenster", "07:45:01", "GET /x", ("ValueError",), False, "hoort hier niet")
    _stub(
        monkeypatch,
        tmp_path,
        fuzzercode=0,
        bevindingen=[],
        telling=Telling(toetsen=12, overgeslagen=0),
        zap=ZapUitkomst(gemeten=True, meldingen=[]),
        asgi=[fout],
    )
    assert proef.main() == proef.BEVINDING
    rapport = (tmp_path / "aanvalsproef.md").read_text()
    assert "BEVINDING" in rapport
    assert "hoort hier niet" in rapport


def test_een_verwachte_asgi_fout_blijft_groen(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Negatief voorbeeld: de verwachte uitkomst van een wegwerp-sandbox mag niet roodmaken."""
    fout = AsgiFout("test_a.py::test_x", "07:45:01", "GET /x", ("socket.gaierror",), True, "geen database")
    _stub(
        monkeypatch,
        tmp_path,
        fuzzercode=0,
        bevindingen=[],
        telling=Telling(toetsen=12, overgeslagen=0),
        zap=ZapUitkomst(gemeten=True, meldingen=[]),
        asgi=[fout],
    )
    assert proef.main() == proef.GROEN


def test_een_pytest_crash_komt_met_de_laatste_logregels_in_het_rapport(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Faalconditie: pytest valt om zonder dat het rapport zegt waarop OPI strandde."""
    _stub(
        monkeypatch,
        tmp_path,
        fuzzercode=2,
        bevindingen=[],
        telling=Telling(toetsen=12, overgeslagen=0),
        zap=ZapUitkomst(gemeten=True, meldingen=[]),
        laatste_regels=("2026-10-23 07:45:00,001 ERROR opi.server laatste woord",),
    )
    assert proef.main() == proef.NIET_GEMETEN
    rapport = (tmp_path / "aanvalsproef.md").read_text()
    assert "Crash van de proef zelf" in rapport
    assert "exitcode 2" in rapport
    assert "laatste woord" in rapport
