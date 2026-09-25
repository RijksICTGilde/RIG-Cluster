"""De beslislogica van de sandbox-helpers, zonder cluster.

``tests/e2e/helpers/cluster.py`` en ``tests/e2e/helpers/zad_cli.py`` zijn toetscode, maar ze
dragen de beslissingen waarop de sandboxmodules steunen: wanneer een psql-run geldig is,
wanneer een lezing een fout is, en of de CLI-toetsen draaien of overslaan. Die modules zelf
draaien alleen op een cluster, dus breekt daar iets in, dan valt dat pas op als iemand de
sandbox claimt. Deze toetsen draaien wel in een gewone ronde.
"""

from __future__ import annotations

import subprocess
from typing import Any

import pytest
from _pytest.outcomes import Skipped
from tests.e2e import test_sandbox_migratie_006 as migratie_006
from tests.e2e.helpers import cluster, zad_cli


class TestRunPsql:
    """De afdichting rond ``kubectl run``: wanneer telt een uitkomst mee."""

    @staticmethod
    def _antwoorden(monkeypatch: pytest.MonkeyPatch, *uitkomsten: tuple[int, str]) -> list[str]:
        """Laat ``_run_psql_once`` de opgegeven uitkomsten op volgorde teruggeven.

        Geeft de lijst terug waarin elke aanroep zijn SQL achterlaat, zodat een toets zowel
        op het aantal pogingen als op het verstuurde statement kan meten.
        """
        verstuurd: list[str] = []
        beurten = iter(uitkomsten)

        def _nep(sql: str, **_: Any) -> tuple[int, str]:
            verstuurd.append(sql)
            return next(beurten)

        monkeypatch.setattr(cluster, "_run_psql_once", _nep)
        return verstuurd

    @staticmethod
    def _psql(**extra: Any) -> tuple[int, str]:
        return cluster.run_psql("SELECT 1", host="h", user="u", database="d", password="w", **extra)

    def test_een_geslaagde_run_geeft_de_uitvoer_zonder_het_sluitstuk(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._antwoorden(monkeypatch, (0, f"42\n{cluster.PSQL_SLUITSTUK}"))

        assert self._psql() == (0, "42")

    def test_een_pod_die_zijn_uitvoer_verliest_geeft_geen_nul(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """De kern: exit NUL zonder uitvoer mag geen groene worden.

        ``kubectl run --rm -i`` komt op een druk cluster terug met exitcode 0 en alleen zijn
        eigen opruimregel. Een SCHRIJFactie staat dan groen op werk dat niet gebeurde.
        """
        verstuurd = self._antwoorden(monkeypatch, *[(0, 'pod "psql-e2e-ab12" deleted')] * 3)

        code, _ = self._psql()

        assert code != 0, "een run zonder uitvoer kwam als geslaagd terug"
        assert len(verstuurd) == 3, "de verloren uitvoer werd niet opnieuw geprobeerd"

    def test_een_verloren_uitvoer_wordt_opnieuw_geprobeerd(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """En een geslaagde tweede poging telt gewoon, anders is de herhaling zinloos."""
        verstuurd = self._antwoorden(
            monkeypatch,
            (0, 'pod "psql-e2e-ab12" deleted'),
            (0, f"ok\n{cluster.PSQL_SLUITSTUK}"),
        )

        assert self._psql() == (0, "ok")
        assert len(verstuurd) == 2, "er werd doorgeprobeerd na een geslaagde poging"

    def test_een_sql_fout_gaat_meteen_terug(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Een fout in het statement mag niet achter de herhaling verdwijnen.

        Zonder deze regel kost een typefout drie pods en komt hij terug als "de pod verloor
        zijn uitvoer", en dat is een heel ander probleem dan het echte.
        """
        verstuurd = self._antwoorden(monkeypatch, (1, 'ERROR:  relatie "x" bestaat niet'))

        code, uit = self._psql()

        assert code == 1
        assert "ERROR:" in uit
        assert len(verstuurd) == 1, "een SQL-fout werd opnieuw geprobeerd"

    def test_het_sluitstuk_gaat_mee_met_het_statement(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """De aanroeper schrijft het niet zelf op, dus het moet er hier bij.

        Ook met een puntkomma van de aanroeper: die mag geen lege instructie opleveren.
        """
        verstuurd = self._antwoorden(monkeypatch, (0, cluster.PSQL_SLUITSTUK))

        cluster.run_psql("SELECT 1;", host="h", user="u", database="d", password="w")

        assert verstuurd[0] == f"SELECT 1; SELECT '{cluster.PSQL_SLUITSTUK}';"


class TestGetJsonStrict:
    """Een mislukte lezing is een fout, geen leeg antwoord."""

    @staticmethod
    def _kubectl(monkeypatch: pytest.MonkeyPatch, code: int, stdout: str, stderr: str = "") -> None:
        monkeypatch.setattr(
            cluster,
            "_run",
            lambda *_, **__: subprocess.CompletedProcess(args=[], returncode=code, stdout=stdout, stderr=stderr),
        )

    def test_een_mislukte_lezing_is_een_fout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``get_json`` geeft hier ``{"items": []}``, en dat leest een toets die op
        AFWEZIGHEID meet als "het staat er niet"."""
        self._kubectl(monkeypatch, 1, "", "Unable to connect to the server")

        with pytest.raises(RuntimeError, match="Unable to connect"):
            cluster.get_json_strict("get", "secrets", "-n", "rig-proef")

        assert cluster.get_json("secrets", "rig-proef") == {"items": []}

    def test_een_geslaagde_lezing_komt_geparst_terug(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._kubectl(monkeypatch, 0, '{"items": [{"metadata": {"name": "geheim"}}]}')

        gelezen = cluster.get_json_strict("get", "secrets", "-n", "rig-proef")

        assert [item["metadata"]["name"] for item in gelezen["items"]] == ["geheim"]


class TestZadCli:
    """Wat de CLI-toetsen aanroepen, en waarmee."""

    @staticmethod
    def _vang(monkeypatch: pytest.MonkeyPatch, *, stdout: str = "", code: int = 0) -> dict[str, Any]:
        """Vang de aanroep die ``ZadCli.run`` doet in plaats van hem uit te voeren."""
        gevangen: dict[str, Any] = {}

        def _nep(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            gevangen["argv"] = argv
            gevangen["env"] = kwargs["env"]
            return subprocess.CompletedProcess(args=argv, returncode=code, stdout=stdout, stderr="")

        monkeypatch.setattr(zad_cli.subprocess, "run", _nep)
        return gevangen

    def test_de_aanroep_wijst_naar_de_sandbox_en_draagt_de_projectsleutel(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Zonder deze drie waarden praat de CLI met een andere server of met niemand.

        En dan blijft ``test_cli_valt_om_op_een_dood_adres`` gewoon groen, want die meet
        alleen DAT de CLI omvalt.
        """
        gevangen = self._vang(monkeypatch)
        cli = zad_cli.ZadCli("/pad/zad", "https://zad.sandbox.rijksapp.dev/", api_key="sleutel", project="proef")

        cli.run("project", "status")

        assert gevangen["argv"] == ["/pad/zad", "project", "status"]
        assert gevangen["env"]["ZAD_API_URL"] == "https://zad.sandbox.rijksapp.dev/api"
        assert gevangen["env"]["ZAD_API_KEY"] == "sleutel"
        assert gevangen["env"]["ZAD_PROJECT_ID"] == "proef"

    def test_zonder_sleutel_gaat_er_geen_lege_sleutel_mee(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Een lege ``ZAD_API_KEY`` is iets anders dan geen sleutel: hij overschrijft de
        sleutel die de omgeving al had staan."""
        monkeypatch.setenv("ZAD_API_KEY", "uit-de-omgeving")
        gevangen = self._vang(monkeypatch)

        zad_cli.ZadCli("/pad/zad", "https://proef").run("project", "status")

        assert gevangen["env"]["ZAD_API_KEY"] == "uit-de-omgeving"

    def test_verwacht_json_vraagt_de_cli_om_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        gevangen = self._vang(monkeypatch)

        zad_cli.ZadCli("/pad/zad", "https://proef").run("component", "list", verwacht_json=True)

        assert gevangen["argv"][-2:] == ["-o", "json"]

    def test_een_antwoord_dat_geen_json_is_zegt_wat_er_dan_wel_stond(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Anders komt er een kale JSONDecodeError uit en moet je de toets openen om te
        zien welk commando het was."""
        self._vang(monkeypatch, stdout="Error: kon niet verbinden")

        resultaat = zad_cli.ZadCli("/pad/zad", "https://proef").run("project", "status")

        with pytest.raises(AssertionError, match="kon niet verbinden"):
            resultaat.json()

    def test_de_uitvoer_is_beide_stromen(self) -> None:
        """De CLI kiest zelf waar een melding heen gaat, dus een assertie op een melding
        mag niet van die keuze afhangen."""
        resultaat = zad_cli.CliResultaat(exitcode=1, stdout="op stdout", stderr="op stderr", argv=["zad"])

        assert "op stdout" in resultaat.uitvoer
        assert "op stderr" in resultaat.uitvoer

    @pytest.mark.parametrize(
        ("exitcode", "mag_ok", "mag_falen"),
        [(0, True, False), (1, False, True)],
    )
    def test_de_twee_beweringen_sluiten_elkaar_uit(self, exitcode: int, mag_ok: bool, mag_falen: bool) -> None:
        resultaat = zad_cli.CliResultaat(exitcode=exitcode, stdout="", stderr="", argv=["zad", "project", "status"])

        for bewering, mag in ((resultaat.assert_ok, mag_ok), (resultaat.assert_faalt, mag_falen)):
            if mag:
                bewering()
            else:
                with pytest.raises(AssertionError, match="project status"):
                    bewering()


class TestCliPad:
    """Of de CLI-toetsen draaien of overslaan hangt hieraan."""

    def test_de_omgevingsvariabele_gaat_voor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(zad_cli.CLI_ENV, "/eigen/bouw/zad")
        monkeypatch.setattr(zad_cli.shutil, "which", lambda naam: f"/usr/bin/{naam}")

        assert zad_cli.cli_pad() == "/eigen/bouw/zad"

    def test_zonder_variabele_wordt_het_pad_afgezocht(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Beide namen tellen: het plan noemt ``zadctl``, de entry point heet ``zad``."""
        monkeypatch.delenv(zad_cli.CLI_ENV, raising=False)
        monkeypatch.setattr(zad_cli.shutil, "which", lambda naam: "/usr/bin/zadctl" if naam == "zadctl" else None)

        assert zad_cli.cli_pad() == "/usr/bin/zadctl"

    def test_geen_cli_is_none_en_geen_uitzondering(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``pytestmark`` roept dit aan tijdens de COLLECTIE. Gooit het hier, dan valt de
        hele module om in plaats van over te slaan."""
        monkeypatch.delenv(zad_cli.CLI_ENV, raising=False)
        monkeypatch.setattr(zad_cli.shutil, "which", lambda _: None)

        assert zad_cli.cli_pad() is None


class TestSecretValues:
    """De secret-lezing waar twee sandboxmodules op steunen.

    ``test_sandbox_migratie_006`` haalt er het databasewachtwoord mee op, en die lezing
    beslist of de toetsen die op de database meten DRAAIEN of stilletjes overslaan;
    ``test_sandbox_kloon`` leest er de inloggegevens van een deployment mee.
    """

    @staticmethod
    def _kubectl(monkeypatch: pytest.MonkeyPatch, code: int, stdout: str, stderr: str = "") -> None:
        monkeypatch.setattr(
            cluster,
            "_run",
            lambda *_, **__: subprocess.CompletedProcess(args=[], returncode=code, stdout=stdout, stderr=stderr),
        )

    def test_de_waarden_komen_ontcijferd_terug(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Een secret draagt base64. Komt dat ongemoeid terug, dan gaat er een onbruikbaar
        wachtwoord naar psql en is de melding een authenticatiefout ver van de oorzaak."""
        self._kubectl(monkeypatch, 0, '{"data": {"password": "Z2Voei1tZXQtLcO6bmljw7bOng==", "user": "cG9zdGdyZXM="}}')

        assert cluster.secret_values("rig-system", "postgres-admin-credentials") == {
            "password": "gehz-met--únicöΞ",
            "user": "postgres",
        }

    def test_een_mislukte_lezing_is_een_fout_en_geen_leeg_secret(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Via ``get_json_strict``, en dat is het verschil dat telt: een lezing die niet kon
        mag er niet uitzien als een secret zonder velden. De aanroeper leest dat laatste als
        "de sleutel staat er niet" en die twee horen niet hetzelfde te doen."""
        self._kubectl(monkeypatch, 1, "", 'secrets "postgres-admin-credentials" not found')

        with pytest.raises(RuntimeError, match="not found"):
            cluster.secret_values("rig-system", "postgres-admin-credentials")

    def test_een_secret_zonder_data_geeft_een_lege_map(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``data`` mag ontbreken of null zijn; dat is geen uitzondering hier maar een
        KeyError bij de aanroeper, die er zijn eigen melding aan hangt."""
        self._kubectl(monkeypatch, 0, '{"metadata": {"name": "leeg"}, "data": null}')

        assert cluster.secret_values("rig-system", "leeg") == {}


class TestDbWachtwoord:
    """De fixture die beslist of migratie 006 op het cluster gemeten wordt."""

    @staticmethod
    def _lezing(monkeypatch: pytest.MonkeyPatch, uitkomst: Exception | dict[str, str]) -> None:
        def _nep(namespace: str, naam: str) -> dict[str, str]:
            if isinstance(uitkomst, Exception):
                raise uitkomst
            return uitkomst

        monkeypatch.setattr(migratie_006.cluster, "secret_values", _nep)

    @staticmethod
    def _roep_aan() -> str:
        """De fixture, met een overslag omgezet in een ROOD.

        Een kale aanroep zou dat niet doen: ``pytest.skip`` in de fixture gooit ``Skipped``,
        en die komt hier uit de toets omhoog, waarna pytest de toets zelf als overgeslagen
        wegschrijft. Een toets die op de mutatie overslaat in plaats van te falen bewaakt
        niets, want een overgeslagen toets leest in de uitslag als een die niets te melden had.
        """
        try:
            return migratie_006.db_wachtwoord.__wrapped__("https://zad.sandbox.rijksapp.dev")
        except Skipped as overgeslagen:
            raise AssertionError(f"de fixture sloeg over: {overgeslagen}") from overgeslagen

    def test_een_geslaagde_lezing_levert_het_wachtwoord(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """De kant die stil kan wegvallen: slaat deze fixture over, dan meldt elke toets die
        hem vraagt niets, en leest een groene ronde als bewijs dat de backfill klopt."""
        self._lezing(monkeypatch, {"password": "geheim", "username": "postgres"})

        assert self._roep_aan() == "geheim"

    @pytest.mark.parametrize(
        "fout",
        [RuntimeError("kubectl kon niet lezen"), KeyError("password"), FileNotFoundError("kubectl")],
        ids=["lezing-mislukt", "veld-ontbreekt", "geen-kubectl"],
    )
    def test_een_onbereikbaar_secret_is_een_skip_en_geen_fout(
        self, monkeypatch: pytest.MonkeyPatch, fout: Exception
    ) -> None:
        """Drie vormen, een uitkomst: geen cluster om op te meten is overslaan. Valt een van
        de drie erbuiten, dan wordt de hele module ERROR in plaats van overgeslagen."""
        self._lezing(monkeypatch, fout)

        with pytest.raises(Skipped):
            migratie_006.db_wachtwoord.__wrapped__("https://zad.sandbox.rijksapp.dev")

    def test_een_programmeerfout_wordt_geen_skip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """De andere helft van de vangst, en de gevaarlijkste: wordt hij ooit verbreed, dan
        verdwijnen de clustertoetsen van die module bij elke fout in stilte uit de uitslag, en
        dat is precies het beeld van een sandbox die niets te melden had."""
        self._lezing(monkeypatch, TypeError("secret_values kreeg het verkeerde aantal argumenten"))

        with pytest.raises(TypeError):
            self._roep_aan()
