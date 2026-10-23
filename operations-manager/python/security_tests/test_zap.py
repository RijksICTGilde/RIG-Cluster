"""De ZAP-basislaag, zover hij zonder docker te toetsen is.

Het draaien van ZAP zelf hoort bij ``task security-proof`` (en vraagt docker). Wat hier staat
is de lezing van zijn rapport en de vergelijking met de baseline: dat is de plek waar een
bevinding stil zou kunnen wegvallen.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from security_tests import zap_scan
from security_tests.doel import Doel
from security_tests.zap_scan import (
    HOSTNAAM_IN_CONTAINER,
    ZAP_IMAGE,
    commando,
    containerdoel,
    lees_bevindingen,
    tokenproef,
    zap_beschikbaar,
)

VOORBEELDRAPPORT = {
    "site": [
        {
            "@name": "http://127.0.0.1:8311",
            "alerts": [
                {
                    "pluginid": "10038",
                    "name": "Content Security Policy Header Not Set",
                    "riskcode": "2",
                    "riskdesc": "Medium (High)",
                    "instances": [{"uri": "http://127.0.0.1:8311/"}],
                },
                {
                    "pluginid": "10015",
                    "name": "Re-examine Cache-control Directives",
                    "riskcode": "0",
                    "riskdesc": "Informational (Low)",
                    "instances": [{"uri": "http://127.0.0.1:8311/about"}],
                },
            ],
        }
    ]
}


def test_een_bevinding_boven_de_drempel_komt_eruit(tmp_path: Path) -> None:
    rapport = tmp_path / "zap.json"
    rapport.write_text(json.dumps(VOORBEELDRAPPORT))
    bevindingen = lees_bevindingen(rapport)
    assert bevindingen == {
        "10038 Content Security Policy Header Not Set": "risico Medium (High) op http://127.0.0.1:8311/"
    }


def test_een_informatieve_bevinding_telt_niet_mee(tmp_path: Path) -> None:
    """Negatief voorbeeld: een rapport met alleen info-bevindingen hoort geen proef rood te maken."""
    rapport = tmp_path / "zap.json"
    rapport.write_text(json.dumps({"site": [{"alerts": [VOORBEELDRAPPORT["site"][0]["alerts"][1]]}]}))
    assert lees_bevindingen(rapport) == {}


def test_het_commando_richt_zich_op_de_eigen_sandbox(doel: Doel) -> None:
    """De doel-URL in het commando komt uit de grendel, niet uit een argument."""
    regels = commando(doel, "projectlid", Path("/tmp/zap.json"))
    assert ZAP_IMAGE in regels
    # Op macOS ziet de container de host als host.docker.internal; containerdoel kiest dat per platform.
    assert f"{containerdoel(doel.url)[0]}/openapi.json" in regels
    assert any(doel.sleutels[doel.projecten[0]] in regel for regel in regels)


def test_de_tokenproef_vraagt_het_token_van_de_grendel_op(doel: Doel) -> None:
    """De proef die een pas vooraf gaat, vraagt het token van dit runbestand op bij hetzelfde doel."""
    regels = tokenproef(doel)
    assert ZAP_IMAGE in regels
    assert any(doel.token in regel for regel in regels), "zonder het token is dit alleen een verbinding"
    assert any("8311" in regel and "/health" in regel for regel in regels)


def test_op_linux_blijft_het_doel_de_loopback_met_het_host_netwerk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Faalconditie: --network host valt weg, en dan kijkt de container naar zijn eigen localhost."""
    monkeypatch.setattr(zap_scan.sys, "platform", "linux")
    url, netwerk = containerdoel("http://127.0.0.1:8311")
    assert url == "http://127.0.0.1:8311"
    assert netwerk == ["--network", "host"]


def test_op_macos_wordt_alleen_de_host_vertaald(monkeypatch: pytest.MonkeyPatch) -> None:
    """Op Docker Desktop is er geen host-netwerk, dus de host wordt vertaald.

    Alleen de host: hetzelfde schema, dezelfde poort, hetzelfde pad. Een andere poort zou een
    andere dienst zijn dan de sandbox die het harnas opzette.
    """
    monkeypatch.setattr(zap_scan.sys, "platform", "darwin")
    url, netwerk = containerdoel("http://127.0.0.1:8311")
    assert url == f"http://{HOSTNAAM_IN_CONTAINER}:8311"
    assert netwerk == [], "--network host bestaat niet op Docker Desktop"


def test_verifieer_containerdoel_is_leeg_bij_een_geldig_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Een returncode 0 van de tokenproef betekent: de container ziet de eigen sandbox."""
    monkeypatch.setattr(subprocess, "run", lambda *_, **__: subprocess.CompletedProcess([], 0, stdout="", stderr=""))
    assert zap_scan.verifieer_containerdoel(_fake_doel(tmp_path / "aanroepen.jsonl")) == ""


def test_verifieer_containerdoel_meldt_het_token_en_de_exitcode_bij_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Faalconditie: een mismatch (exit 3) of een andere fout wordt stilgehouden in plaats van gemeld."""
    monkeypatch.setattr(
        subprocess, "run", lambda *_, **__: subprocess.CompletedProcess([], 3, stdout="", stderr="token mismatch")
    )
    reden = zap_scan.verifieer_containerdoel(_fake_doel(tmp_path / "aanroepen.jsonl"))
    assert "exit 3" in reden
    assert "token mismatch" in reden


def test_verifieer_containerdoel_meldt_een_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Faalconditie: een hangende tokenproef laat de laag stilvallen in plaats van NIET GEMETEN te melden."""

    def hangend(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=["docker"], timeout=zap_scan.TIJDBUDGET_PROEF_SECONDEN)

    monkeypatch.setattr(subprocess, "run", hangend)
    reden = zap_scan.verifieer_containerdoel(_fake_doel(tmp_path / "aanroepen.jsonl"))
    assert "kwam niet verder" in reden


def test_zonder_docker_meldt_de_laag_dat_hij_niet_gemeten_is() -> None:
    beschikbaar, reden = zap_beschikbaar()
    if not beschikbaar:
        assert reden, "een niet gemeten laag hoort te zeggen waarom"
        pytest.skip(f"ZAP niet beschikbaar: {reden}")


def _mountmap(commando_regels: list[str]) -> Path:
    """De map die als ``/zap/wrk`` gemount is, en waarin ZAP dus zijn rapport neerzet."""
    return Path(commando_regels[commando_regels.index("--volume") + 1].split(":")[0])


def _fake_doel(aanroeplog: Path) -> Doel:
    """Een handgebouwd doel, zodat ``draai`` te beproeven is zonder een draaiende sandbox."""
    return Doel(
        url="http://127.0.0.1:8311",
        token="proef-token",
        pid=0,
        sleutels={"proef-alfa": "sleutel-alfa", "proef-beta": "sleutel-beta"},
        sessies={"zad-admin": "sessie-admin"},
        aanroeplog=aanroeplog,
    )


class TestDraai:
    """``draai`` zonder docker: ``subprocess.run`` wordt nagemaakt, niet echt een container gestart."""

    def test_zonder_docker_draait_geen_enkele_pas(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Faalconditie: de laag probeert toch een pas te draaien zonder docker."""
        monkeypatch.setattr(zap_scan, "zap_beschikbaar", lambda: (False, "docker staat niet op het PATH"))

        def niet_draaien(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
            raise AssertionError("draai() startte een subprocess zonder docker")

        monkeypatch.setattr(subprocess, "run", niet_draaien)
        uitkomst = zap_scan.draai(_fake_doel(tmp_path / "aanroepen.jsonl"))
        assert uitkomst.gemeten is False
        assert uitkomst.reden == "docker staat niet op het PATH"
        assert uitkomst.bevindingen == {}

    def test_elke_pas_schrijft_zijn_eigen_rapport_en_telt_mee(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(zap_scan, "RAPPORTMAP", tmp_path)
        monkeypatch.setattr(zap_scan, "zap_beschikbaar", lambda: (True, ""))
        monkeypatch.setattr(zap_scan, "verifieer_containerdoel", lambda _doel: "")

        def nep_run(commando_regels: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            rapport = Path(commando_regels[commando_regels.index("-J") + 1])
            (_mountmap(commando_regels) / rapport.name).write_text(json.dumps(VOORBEELDRAPPORT))
            return subprocess.CompletedProcess(commando_regels, 0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", nep_run)
        uitkomst = zap_scan.draai(_fake_doel(tmp_path / "aanroepen.jsonl"), passen=("ongeauthenticeerd", "zad-admin"))
        assert uitkomst.gemeten is True
        assert set(uitkomst.bevindingen) == {
            "ongeauthenticeerd/10038 Content Security Policy Header Not Set",
            "zad-admin/10038 Content Security Policy Header Not Set",
        }
        # toets_tegen_baseline ziet geen van beide kenmerken in de (lege) baseline, dus beide
        # komen als melding terug: zonder die stap zou een nieuwe bevinding stil verdwijnen.
        assert len(uitkomst.meldingen) == 2

    def test_alleen_de_map_van_zap_gaat_open_voor_de_container(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Faalconditie: de rapportmap zelf staat open, en dan leidt een andere lokale gebruiker
        met een symlink op een vrije naam de log, de junit of het rapport om naar een eigen doel."""
        rapportmap = tmp_path / "report"
        # Zoals een eerdere versie hem achterliet: wereld-schrijfbaar, en dat hoort de laag
        # te herstellen in plaats van over te nemen.
        rapportmap.mkdir()
        rapportmap.chmod(0o1777)
        monkeypatch.setattr(zap_scan, "RAPPORTMAP", rapportmap)
        monkeypatch.setattr(zap_scan, "zap_beschikbaar", lambda: (True, ""))
        monkeypatch.setattr(zap_scan, "verifieer_containerdoel", lambda _doel: "")

        def nep_run(commando_regels: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            naam = commando_regels[commando_regels.index("-J") + 1]
            (_mountmap(commando_regels) / naam).write_text(json.dumps(VOORBEELDRAPPORT))
            return subprocess.CompletedProcess(commando_regels, 0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", nep_run)
        zap_scan.draai(_fake_doel(tmp_path / "aanroepen.jsonl"), passen=("ongeauthenticeerd",))
        assert (rapportmap / "zap").stat().st_mode & 0o7777 == 0o1777
        assert not rapportmap.stat().st_mode & 0o002, "de rapportmap zelf hoort dicht te blijven"
        assert (rapportmap / "zap-ongeauthenticeerd.log").exists(), "de log hoort buiten de open map"

    def test_een_pas_die_over_zijn_tijdbudget_loopt_stopt_de_hele_ronde(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Faalconditie: een vastgelopen pas laat de volgende passen alsnog draaien."""
        monkeypatch.setattr(zap_scan, "RAPPORTMAP", tmp_path)
        monkeypatch.setattr(zap_scan, "zap_beschikbaar", lambda: (True, ""))
        monkeypatch.setattr(zap_scan, "verifieer_containerdoel", lambda _doel: "")
        aanroepen = 0

        def hangende_run(
            commando_regels: list[str], *, timeout: float = 0, **_: object
        ) -> subprocess.CompletedProcess[str]:
            nonlocal aanroepen
            aanroepen += 1
            raise subprocess.TimeoutExpired(cmd=commando_regels, timeout=timeout)

        monkeypatch.setattr(subprocess, "run", hangende_run)
        uitkomst = zap_scan.draai(_fake_doel(tmp_path / "aanroepen.jsonl"), passen=("ongeauthenticeerd", "zad-admin"))
        assert uitkomst.gemeten is False
        assert "ongeauthenticeerd" in uitkomst.reden
        assert aanroepen == 1, "de ronde had na de eerste timeout moeten stoppen"

    def test_een_pas_zonder_rapport_meldt_niet_gemeten_met_een_verklaring(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Faalconditie: geen rapport wordt stilletjes als een schone, lege pas gelezen."""
        monkeypatch.setattr(zap_scan, "RAPPORTMAP", tmp_path)
        monkeypatch.setattr(zap_scan, "zap_beschikbaar", lambda: (True, ""))
        monkeypatch.setattr(zap_scan, "verifieer_containerdoel", lambda _doel: "")

        def geen_rapport(commando_regels: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(commando_regels, 1, stdout="iets ging mis", stderr="")

        monkeypatch.setattr(subprocess, "run", geen_rapport)
        uitkomst = zap_scan.draai(_fake_doel(tmp_path / "aanroepen.jsonl"), passen=("ongeauthenticeerd",))
        assert uitkomst.gemeten is False
        assert "ongeauthenticeerd" in uitkomst.reden
        assert "ZAP brak op iets anders af" in uitkomst.reden

    def test_een_oud_rapport_leest_niet_als_de_meting_van_nu(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Faalconditie: ZAP schrijft niets, en het rapport van de vorige run wordt gelezen.

        De map van ZAP staat wereld-schrijfbaar open, dus zo'n rapport kan ook van een andere
        lokale gebruiker komen. Beide gevallen horen NIET GEMETEN te zijn.
        """
        monkeypatch.setattr(zap_scan, "RAPPORTMAP", tmp_path)
        monkeypatch.setattr(zap_scan, "zap_beschikbaar", lambda: (True, ""))
        monkeypatch.setattr(zap_scan, "verifieer_containerdoel", lambda _doel: "")
        oud = tmp_path / "zap" / "zap-ongeauthenticeerd.json"
        oud.parent.mkdir(parents=True)
        oud.write_text(json.dumps({"site": []}))

        def geen_rapport(commando_regels: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(commando_regels, 0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", geen_rapport)
        uitkomst = zap_scan.draai(_fake_doel(tmp_path / "aanroepen.jsonl"), passen=("ongeauthenticeerd",))
        assert uitkomst.gemeten is False

    def test_een_rapport_dat_niet_weg_kan_is_niet_gemeten(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Faalconditie: een rapport van een andere eigenaar blijft staan en wordt alsnog gelezen."""
        monkeypatch.setattr(zap_scan, "RAPPORTMAP", tmp_path)
        monkeypatch.setattr(zap_scan, "zap_beschikbaar", lambda: (True, ""))
        monkeypatch.setattr(zap_scan, "verifieer_containerdoel", lambda _doel: "")

        def weigert(self: Path, missing_ok: bool = False) -> None:
            raise PermissionError(1, "Operation not permitted")

        monkeypatch.setattr(Path, "unlink", weigert)

        def niet_draaien(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
            raise AssertionError("draai() startte een pas met een rapport dat niet weg kon")

        monkeypatch.setattr(subprocess, "run", niet_draaien)
        uitkomst = zap_scan.draai(_fake_doel(tmp_path / "aanroepen.jsonl"), passen=("ongeauthenticeerd",))
        assert uitkomst.gemeten is False
        assert "niet weg te halen" in uitkomst.reden

    def test_een_onbereikbaar_containerdoel_meldt_niet_gemeten_zonder_een_pas(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Faalconditie: drie passen draaien tegen een doel dat de container niet als de sandbox ziet.

        Dit is wat er op Docker Desktop gebeurde: 127.0.0.1 in de container is de container zelf.
        """
        monkeypatch.setattr(zap_scan, "RAPPORTMAP", tmp_path)
        monkeypatch.setattr(zap_scan, "zap_beschikbaar", lambda: (True, ""))
        monkeypatch.setattr(zap_scan, "verifieer_containerdoel", lambda _doel: "de container ziet het doel niet")

        def niet_draaien(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
            raise AssertionError("draai() startte een pas tegen een onbereikbaar doel")

        monkeypatch.setattr(subprocess, "run", niet_draaien)
        uitkomst = zap_scan.draai(_fake_doel(tmp_path / "aanroepen.jsonl"), passen=("ongeauthenticeerd",))
        assert uitkomst.gemeten is False
        assert uitkomst.reden == "de container ziet het doel niet"
