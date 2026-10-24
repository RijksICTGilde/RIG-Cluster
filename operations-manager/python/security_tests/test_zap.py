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

from security_tests import baseline as baseline_module
from security_tests import zap_scan
from security_tests.doel import Doel
from security_tests.zap_scan import (
    HOSTNAAM_IN_CONTAINER,
    ZAP_IMAGE,
    commando,
    containerdoel,
    lees_alerts,
    ontbrekende_herkenning,
    proefnaam,
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
                    "pluginid": "90004",
                    "name": "Cross-Origin-Resource-Policy Header Missing or Invalid",
                    "riskcode": "1",
                    "riskdesc": "Low (Medium)",
                    "instances": [{"uri": "http://127.0.0.1:8311/version"}],
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
    """Het kenmerk is pluginid en route, dus dezelfde vorm als de baseline."""
    rapport = tmp_path / "zap.json"
    rapport.write_text(json.dumps(VOORBEELDRAPPORT))
    bevindingen, _ = lees_alerts(rapport)
    assert bevindingen == {
        "10038:/": "Content Security Policy Header Not Set (risico Medium (High))",
        "90004:/version": "Cross-Origin-Resource-Policy Header Missing or Invalid (risico Low (Medium))",
    }


def test_een_warn_telt_mee_en_een_info_blijft_informatie(tmp_path: Path) -> None:
    """Faalconditie: een WARN (riskcode 1) valt buiten de bevindingen en staat dus nergens.

    Dat was de stille waarschuwing: hij liet de gate niet falen en stond niet als geaccepteerd
    in de baseline. Een INFO blijft wel informatie.
    """
    rapport = tmp_path / "zap.json"
    alerts = VOORBEELDRAPPORT["site"][0]["alerts"]
    rapport.write_text(json.dumps({"site": [{"alerts": [alerts[1], alerts[2]]}]}))
    bevindingen, informatie = lees_alerts(rapport)
    assert set(bevindingen) == {"90004:/version"}
    assert set(informatie) == {"10015:/about"}


def test_het_commando_richt_zich_op_de_eigen_sandbox(doel: Doel, proefproject: str) -> None:
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
    assert any("/health" in regel for regel in regels)


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


def test_op_macos_houdt_een_hostnaam_zijn_naam(monkeypatch: pytest.MonkeyPatch) -> None:
    """Faalconditie: de sandbox achter zijn ingress wordt host.docker.internal, en dan past het
    certificaat van *.sandbox.rijksapp.dev niet meer (gemeten 24-10: CERTIFICATE_VERIFY_FAILED)."""
    monkeypatch.setattr(zap_scan.sys, "platform", "darwin")
    url, netwerk = containerdoel("https://zad.sandbox.rijksapp.dev")
    assert url == "https://zad.sandbox.rijksapp.dev"
    assert netwerk == ["--add-host", "zad.sandbox.rijksapp.dev:host-gateway"]


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


def test_de_baseline_kent_elke_pas_onder_zijn_eigen_naam() -> None:
    assert proefnaam("zad-admin") == "zap-zad-admin"


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
        # Een lege baseline, zodat deze toets over de passen gaat en niet over wat er nu in
        # bekende-bevindingen.tsv staat.
        monkeypatch.setattr(baseline_module, "lees_baseline", lambda _proef, _soort="", pad=None: {})
        uitkomst = zap_scan.draai(_fake_doel(tmp_path / "aanroepen.jsonl"), passen=("ongeauthenticeerd", "zad-admin"))
        assert uitkomst.gemeten is True
        assert set(uitkomst.bevindingen) == {
            "ongeauthenticeerd/10038:/",
            "ongeauthenticeerd/90004:/version",
            "zad-admin/10038:/",
            "zad-admin/90004:/version",
        }
        assert set(uitkomst.informatie) == {"ongeauthenticeerd/10015:/about", "zad-admin/10015:/about"}
        # toets_tegen_baseline ziet geen van deze kenmerken in de baseline, dus ze komen alle
        # vier als melding terug: zonder die stap zou een nieuwe bevinding stil verdwijnen.
        assert len(uitkomst.meldingen) == 4
        assert all(melding.startswith(("ongeauthenticeerd:", "zad-admin:")) for melding in uitkomst.meldingen)

    def test_een_warn_zonder_baselineregel_laat_de_ronde_falen(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Faalconditie: een WARN die niemand heeft uitgelegd levert geen melding op."""
        uitkomst = self._ronde(monkeypatch, tmp_path, {"zap-ongeauthenticeerd": {}})
        assert any("90004:/version" in melding for melding in uitkomst.meldingen)

    def test_een_uitgelegde_warn_is_geen_melding_meer(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Comply or explain: met een reden in de baseline blijft de ronde groen."""
        baseline = {"zap-ongeauthenticeerd": {"90004:/version": "uitgelegd", "10038:/": "uitgelegd"}}
        assert self._ronde(monkeypatch, tmp_path, baseline).meldingen == []

    def test_een_baselineregel_die_niets_dekt_laat_de_ronde_falen(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """De andere richting: een uitleg voor iets dat niet meer voorkomt hoort weg."""
        baseline = {
            "zap-ongeauthenticeerd": {
                "90004:/version": "uitgelegd",
                "10038:/": "uitgelegd",
                "10038:/weg": "deze bevinding is er niet meer",
            }
        }
        uitkomst = self._ronde(monkeypatch, tmp_path, baseline)
        assert any("OPGELOST 10038:/weg" in melding for melding in uitkomst.meldingen)

    def _ronde(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, baseline: dict[str, dict[str, str]]
    ) -> zap_scan.ZapUitkomst:
        """Eén pas met het voorbeeldrapport, tegen een gegeven baseline."""
        monkeypatch.setattr(zap_scan, "RAPPORTMAP", tmp_path)
        monkeypatch.setattr(zap_scan, "zap_beschikbaar", lambda: (True, ""))
        monkeypatch.setattr(zap_scan, "verifieer_containerdoel", lambda _doel: "")
        monkeypatch.setattr(
            baseline_module, "lees_baseline", lambda proef, _soort="", pad=None: baseline.get(proef, {})
        )

        def nep_run(commando_regels: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            rapport = Path(commando_regels[commando_regels.index("-J") + 1])
            (_mountmap(commando_regels) / rapport.name).write_text(json.dumps(VOORBEELDRAPPORT))
            return subprocess.CompletedProcess(commando_regels, 0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", nep_run)
        return zap_scan.draai(_fake_doel(tmp_path / "aanroepen.jsonl"), passen=("ongeauthenticeerd",))

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


class TestOntbrekendeHerkenning:
    """Of een pas op dit doel iets kan meten, en wat er gebeurt als dat niet zo is."""

    def test_een_pas_met_zijn_herkenning_kan_meten(self, tmp_path: Path) -> None:
        doel = _fake_doel(tmp_path / "aanroepen.jsonl")
        assert ontbrekende_herkenning(doel, "ongeauthenticeerd") == ""
        assert ontbrekende_herkenning(doel, "projectlid") == ""
        assert ontbrekende_herkenning(doel, "zad-admin") == ""

    def test_zonder_projectsleutel_of_sessie_noemt_de_pas_zijn_reden(self, tmp_path: Path) -> None:
        kaal = _kaal_doel(tmp_path / "aanroepen.jsonl")
        assert ontbrekende_herkenning(kaal, "ongeauthenticeerd") == ""
        assert "API-sleutel" in ontbrekende_herkenning(kaal, "projectlid")
        assert "sessie" in ontbrekende_herkenning(kaal, "zad-admin")

    def test_een_pas_zonder_herkenning_is_niet_gemeten_en_geen_bevinding(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Faalconditie: een pas die bewust niets meet komt als bevinding uit de laag.

        Dat is de omkering van de groene lijst: NIET GEMETEN hoort exitcode 3 te geven en niet
        1, en tegen een cluster-sandbox zonder proefsleutels zou de gate anders altijd rood zijn.
        """
        monkeypatch.setattr(zap_scan, "RAPPORTMAP", tmp_path)
        monkeypatch.setattr(zap_scan, "zap_beschikbaar", lambda: (True, ""))
        monkeypatch.setattr(zap_scan, "verifieer_containerdoel", lambda _doel: "")
        monkeypatch.setattr(subprocess, "run", _niet_aangeroepen)
        uitkomst = zap_scan.draai(_kaal_doel(tmp_path / "aanroepen.jsonl"), passen=("projectlid", "zad-admin"))
        assert uitkomst.gemeten is True
        assert uitkomst.meldingen == [], "een niet gemeten pas is geen bevinding"
        assert set(uitkomst.niet_gemeten) == {"projectlid", "zad-admin"}


def _kaal_doel(aanroeplog: Path) -> Doel:
    """Een doel zonder proefprojecten en zonder sessies, zoals een verse cluster-sandbox."""
    return Doel(url="http://127.0.0.1:8311", token="proef-token", pid=0, sleutels={}, sessies={}, aanroeplog=aanroeplog)


def _niet_aangeroepen(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
    raise AssertionError("er hoort geen pas te draaien als de herkenning ontbreekt")
