"""De beslislogica van de sandbox-helpers, zonder cluster.

``tests/e2e/helpers/cluster.py`` en ``tests/e2e/helpers/zad_cli.py`` zijn toetscode, maar ze
dragen de beslissingen waarop de sandboxmodules steunen: wanneer een psql-run geldig is,
wanneer een lezing een fout is, en of de CLI-toetsen draaien of overslaan. Die modules zelf
draaien alleen op een cluster, dus breekt daar iets in, dan valt dat pas op als iemand de
sandbox claimt. Deze toetsen draaien wel in een gewone ronde.
"""

from __future__ import annotations

import base64
import json
import subprocess
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from _pytest.outcomes import Skipped
from tests.e2e import test_sandbox_migratie_006 as migratie_006
from tests.e2e import test_sandbox_registry_pull as registry_pull
from tests.e2e.conftest import _houd_sandboxmodules_bij_elkaar
from tests.e2e.helpers import cluster, zad_cli

if TYPE_CHECKING:
    from pathlib import Path


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


class TestPsqlAanroep:
    """Waar het wachtwoord langs gaat, en waar het niet mag staan.

    ``TestRunPsql`` hierboven zet ``_run_psql_once`` weg om de herhaling te meten, en daarmee
    is de OPBOUW van de aanroep daar ongepind. Die opbouw is hier de vondst: langs deze weg
    gaat het superuser-wachtwoord van de gedeelde sandbox-database
    (``test_sandbox_migratie_006``) en het DATABASE_PASSWORD van een deployment
    (``test_sandbox_kloon``).
    """

    WACHTWOORD = "SuPeRgEhEiM-42"

    @staticmethod
    def _vang(monkeypatch: pytest.MonkeyPatch, *, stdout: str = "", tijd_op: bool = False) -> dict[str, Any]:
        """Vang de aanroep die ``_run`` naar subprocess doet in plaats van hem te doen."""
        gevangen: dict[str, Any] = {}

        def _nep(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            gevangen["argv"] = argv
            gevangen["invoer"] = kwargs.get("input")
            if tijd_op:
                raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs["timeout"])
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout=stdout, stderr="")

        monkeypatch.setattr(cluster.subprocess, "run", _nep)
        return gevangen

    def _psql(self, wachtwoord: str = "", sql: str = "SELECT 1") -> tuple[int, str]:
        return cluster.run_psql(
            sql,
            host="rig-db-rw",
            user="postgres",
            database="operations_manager",
            password=wachtwoord or self.WACHTWOORD,
        )

    def test_het_wachtwoord_staat_niet_in_de_argv(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """De vorm die in ``_run_psql_once`` eerst stond was ``--env PGPASSWORD=<waarde>``;
        waar die argv allemaal terechtkomt staat daar.
        """
        gevangen = self._vang(monkeypatch, stdout=f"1\n{cluster.PSQL_SLUITSTUK}")

        self._psql()

        argv = gevangen["argv"]
        assert self.WACHTWOORD not in " ".join(argv)
        assert not [deel for deel in argv if deel.startswith("--env")], f"--env staat er weer in: {argv}"
        assert argv[-2:] == ["sh", "-s"], f"de pod krijgt geen script over stdin: {argv}"

    def test_het_script_zet_het_wachtwoord_in_de_omgeving_en_het_statement_in_de_argv(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Gemeten door het script ECHT te draaien, met een psql die opschrijft wat hij kreeg.

        Een assertie op de tekst van het script zou de quoting niet meten, en die is hier de
        valkuil: een wachtwoord met een apostrof erin breekt een script dat er alleen
        aanhalingstekens omheen zet, en dan voert de pod iets anders uit dan er staat.

        Het statement draagt een `$$`, want de eis die ``run_psql`` daarover STELDE is met de
        verhuizing naar stdin vervallen: een benoemde dollar-quote is niet meer nodig. Komt
        het statement ooit weer als argument mee, dan is dat hier rood in plaats van pas op
        het cluster, waar vandaag geen enkele aanroeper een kale `$$` gebruikt.
        """
        wachtwoord = "hij's 'al' weg $HOME"
        statement = "DO $$ BEGIN RAISE NOTICE 'een citaat'; END $$"
        nep_psql = tmp_path / "psql"
        nep_psql.write_text('#!/bin/sh\nprintf "pgpassword=[%s]\\n" "$PGPASSWORD"\nprintf "argv=[%s]\\n" "$*"\n')
        nep_psql.chmod(0o755)
        gevangen = self._vang(monkeypatch, stdout=cluster.PSQL_SLUITSTUK)

        self._psql(wachtwoord=wachtwoord, sql=statement)

        # De vangst zit op het subprocess-MODULE en niet op een eigen naam in cluster.py, dus
        # zonder deze regel loopt de echte aanroep hieronder er ook in.
        monkeypatch.undo()
        # `/bin/sh` met naam en al, en een PATH die alleen de neppe psql bevat: zo kan het
        # script niets anders aanroepen dan de psql van deze toets.
        gedraaid = subprocess.run(
            ["/bin/sh", "-s"],
            input=gevangen["invoer"],
            capture_output=True,
            text=True,
            env={"PATH": str(tmp_path)},
            check=True,
        )
        assert f"pgpassword=[{wachtwoord}]" in gedraaid.stdout, gedraaid.stdout
        argv_regel = next(regel for regel in gedraaid.stdout.splitlines() if regel.startswith("argv="))
        assert wachtwoord not in argv_regel, f"het wachtwoord staat in de argv van psql: {argv_regel}"
        assert statement in argv_regel, argv_regel

    def test_een_time_out_schrijft_het_commando_niet_op(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Geen hoekgeval: ``run_psql`` zegt zelf dat de pod het op een druk cluster soms niet
        haalt, en er staan drie pogingen van 300s.
        """
        self._vang(monkeypatch, tijd_op=True)

        with pytest.raises(cluster.KubectlTimeout) as fout:
            self._psql()

        assert self.WACHTWOORD not in str(fout.value)
        assert "--image=postgres:16-alpine" not in str(fout.value), "de volledige argv staat in de melding"
        assert fout.value.__cause__ is None, "de oorspronkelijke uitzondering hangt eronder"
        assert fout.value.__suppress_context__, "de traceback drukt de gekoppelde uitzondering alsnog af"

    def test_een_time_out_bij_de_beschikbaarheidstoets_blijft_een_nee(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``kubectl_available`` ving de time-out als ``SubprocessError``. Erft
        ``KubectlTimeout`` daar niet van, dan valt de eerste lezing van elke sandboxmodule om
        met een fout in plaats van over te slaan."""
        cluster.kubectl_available.cache_clear()
        self._vang(monkeypatch, tijd_op=True)
        try:
            assert cluster.kubectl_available() is False
        finally:
            cluster.kubectl_available.cache_clear()


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


class TestGeheimeVlaggen:
    """Wat er van een aanroep in een melding terechtkomt.

    ``registry add --password <token>`` is de aanroep die dit nodig maakt: het token is echt,
    en alle vier de plekken hieronder zetten de argv in hun tekst. Die tekst komt in het
    pytest-verslag en in de uitvoer van de CI-stap.
    """

    GEHEIM = "gEh31m-t0k3n-uit-forgejo"

    def _argv(self, vorm: str) -> list[str]:
        waarde = ["--password", self.GEHEIM] if vorm == "los" else [f"--password={self.GEHEIM}"]
        return ["/pad/zad", "registry", "add", "prive", *waarde, "--yes"]

    @pytest.mark.parametrize("vorm", ["los", "gelijkteken"])
    def test_geen_bewering_zet_het_token_in_haar_melding(self, vorm: str) -> None:
        """Drie meldingen apart, want een maskering op een van de drie laat de andere twee
        staan; en beide vlagvormen, want de CLI accepteert ze allebei."""
        argv = self._argv(vorm)
        beweringen = [
            zad_cli.CliResultaat(exitcode=1, stdout="", stderr="", argv=argv).assert_ok,
            zad_cli.CliResultaat(exitcode=0, stdout="", stderr="", argv=argv).assert_faalt,
            zad_cli.CliResultaat(exitcode=0, stdout="geen json", stderr="", argv=argv).json,
        ]

        for bewering in beweringen:
            with pytest.raises(AssertionError) as fout:
                bewering()

            assert self.GEHEIM not in str(fout.value), f"{bewering.__name__} schrijft het token op"
            assert "registry add prive" in str(fout.value), f"{bewering.__name__} noemt de aanroep niet meer"

    @pytest.mark.parametrize("vorm", ["los", "gelijkteken"])
    def test_een_time_out_schrijft_het_token_niet_op(self, monkeypatch: pytest.MonkeyPatch, vorm: str) -> None:
        """De vierde plek, en de enige waar de melding niet van ``CliResultaat`` komt. De tekst
        van ``TimeoutExpired`` draagt de hele argv, dus die mag ook niet als gekoppelde
        uitzondering blijven hangen."""

        def _nep(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs["timeout"])

        monkeypatch.setattr(zad_cli.subprocess, "run", _nep)
        vlaggen = self._argv(vorm)[1:]

        with pytest.raises(AssertionError) as fout:
            zad_cli.ZadCli("/pad/zad", "https://proef").run(*vlaggen)

        assert self.GEHEIM not in str(fout.value)
        assert fout.value.__cause__ is None, "de oorspronkelijke uitzondering hangt eronder"
        assert fout.value.__suppress_context__, "de traceback drukt de gekoppelde uitzondering alsnog af"

    def test_een_gewoon_argument_blijft_leesbaar(self) -> None:
        """Anders is de maskering een melding zonder aanroep, en dan moet je de toets openen
        om te zien wat er misging."""
        assert zad_cli.leesbare_argv(["/pad/zad", "project", "status"]) == "/pad/zad project status"


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

    @staticmethod
    def _secret_data(**waarden: str) -> str:
        """De ``data`` van een secret zoals kubectl hem afdrukt: base64 per waarde.

        Gecodeerd hier en niet als tekenreeks in de toets: zo'n literal is voor de
        secretscan een generic-api-key, en een uitzondering erop kost meer dan de regel die
        hem berekent. Dat is ook hoe de rest van de toetsen het doet (zie
        ``test_compare_service_identity``).
        """
        data = {sleutel: base64.b64encode(waarde.encode()).decode() for sleutel, waarde in waarden.items()}
        return json.dumps({"data": data})

    def test_de_waarden_komen_ontcijferd_terug(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Een secret draagt base64. Komt dat ongemoeid terug, dan gaat er een onbruikbaar
        wachtwoord naar psql en is de melding een authenticatiefout ver van de oorzaak.

        Het wachtwoord draagt niet-ASCII, want een ``.decode()`` zonder codering is precies
        de plek waar dat omvalt.
        """
        self._kubectl(monkeypatch, 0, self._secret_data(password="gehz-met--únicöΞ", user="postgres"))

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


class TestOpruimlus:
    """De DROP-lus van ``test_sandbox_migratie_006``, waarvan ``_opruim_sql`` opschrijft wat
    een ander voorvoegsel daar aanricht.
    """

    def test_de_lus_staat_op_het_eigen_voorvoegsel(self) -> None:
        sql = migratie_006._opruim_sql(migratie_006._EIGEN_PREFIX)

        assert "starts_with(tablename, 'rc227_')" in sql
        assert "LIKE" not in sql, "in een LIKE-patroon is de `_` van het voorvoegsel een jokerteken"

    @pytest.mark.parametrize("prefix", ["", "rc", "%", "rc227"], ids=["leeg", "korter", "joker", "zonder-liggend"])
    def test_een_ander_voorvoegsel_komt_er_niet_door(self, prefix: str) -> None:
        """Ook een voorvoegsel dat KORTER is dan het eigen voorvoegsel pakt tabellen van
        iemand anders mee, dus "niet leeg" is hier niet genoeg."""
        with pytest.raises(AssertionError, match="voorvoegsel"):
            migratie_006._opruim_sql(prefix)


class TestDockerInlog:
    """De inlog waarmee het testimage in de registry komt.

    Het token is dat van de Forgejo-gebruiker van de sandbox, en dat is dezelfde gebruiker
    waarmee OPI in `zad-projects` schrijft.
    """

    @staticmethod
    def _vang(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
        """Vang elke docker-aanroep van de fixture in plaats van hem te doen."""
        aanroepen: list[dict[str, Any]] = []

        def _nep(*args: str, timeout: float = 0.0, invoer: str | None = None) -> subprocess.CompletedProcess[str]:
            aanroepen.append({"args": list(args), "invoer": invoer})
            return subprocess.CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")

        monkeypatch.setattr(registry_pull, "_docker", _nep)
        monkeypatch.setattr(registry_pull, "_anoniem_te_halen", lambda: False)
        return aanroepen

    def test_het_token_gaat_over_stdin_en_niet_als_vlag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        aanroepen = self._vang(monkeypatch)

        next(registry_pull.prive_image.__wrapped__())

        inlog = aanroepen[0]
        assert inlog["args"][0] == "login"
        assert "--password-stdin" in inlog["args"]
        assert "-p" not in inlog["args"], f"het token staat in de argv: {inlog['args']}"
        assert registry_pull._REGISTRY_PASSWORD not in " ".join(inlog["args"])
        assert inlog["invoer"] == registry_pull._REGISTRY_PASSWORD, "het token gaat niet over stdin mee"

    def test_er_wordt_afgemeld_ook_als_de_push_mislukt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        aanroepen = self._vang(monkeypatch)

        def _mislukt(*args: str, timeout: float = 0.0, invoer: str | None = None) -> subprocess.CompletedProcess[str]:
            aanroepen.append({"args": list(args), "invoer": invoer})
            code = 1 if args[0] == "push" else 0
            return subprocess.CompletedProcess(args=list(args), returncode=code, stdout="", stderr="niet gelukt")

        monkeypatch.setattr(registry_pull, "_docker", _mislukt)

        with pytest.raises(AssertionError, match="push"):
            next(registry_pull.prive_image.__wrapped__())

        assert [aanroep["args"][0] for aanroep in aanroepen][-1] == "logout", (
            f"er is niet afgemeld na een mislukte push: {[aanroep['args'] for aanroep in aanroepen]}"
        )


class TestSandboxmodulesBijElkaar:
    """De hook die de modulegrens terugzet, ``tests/e2e/conftest.py``.

    Wat hij voorkomt kost geld en geen falen: pytest groepeert op de parameters van
    fixtures met een scope boven function en breekt daarmee de modulegrens op, waarna een
    module-fixture opnieuw wordt opgezet. Elke extra opbouw is een volledig project op het
    gedeelde cluster, en de suite blijft er groen bij. Gemeten op de sandboxselectie: zonder
    deze hook 40 blokken over 30 modules, zeven modules gesplitst.
    """

    class _Item:
        """Het minimum dat de hook van een item aanraakt."""

        def __init__(self, module: str, naam: str, *, sandbox: bool = True) -> None:
            self.module = SimpleNamespace(__name__=module)
            self.naam = naam
            self._sandbox = sandbox

        def get_closest_marker(self, naam: str) -> object | None:
            return object() if naam == "sandbox" and self._sandbox else None

        def __repr__(self) -> str:
            return self.naam

    def test_de_toetsen_van_een_module_komen_achter_elkaar(self) -> None:
        items = [
            self._Item("a", "a1"),
            self._Item("b", "b1"),
            self._Item("a", "a2"),
            self._Item("b", "b2"),
            self._Item("a", "a3"),
        ]

        _houd_sandboxmodules_bij_elkaar(items)

        assert [item.naam for item in items] == ["a1", "a2", "a3", "b1", "b2"]

    def test_de_module_die_het_eerst_kwam_blijft_voorop(self) -> None:
        """Op eerste verschijning en niet op naam: anders herschikt de hook de RUN zelf.

        Zonder deze toets is ``sorted(per_module)`` een even goed antwoord, en dan bepaalt de
        alfabetische bestandsnaam de volgorde waarin een suite van een uur zijn projecten
        aanmaakt.
        """
        items = [self._Item("z", "z1"), self._Item("a", "a1"), self._Item("z", "z2")]

        _houd_sandboxmodules_bij_elkaar(items)

        assert [item.naam for item in items] == ["z1", "z2", "a1"]

    def test_een_toets_zonder_de_sandbox_marker_blijft_op_zijn_plek(self) -> None:
        """De hook mag alleen de posities van sandboxtoetsen onderling vullen.

        De lokale e2e-suite heeft niets met dit probleem te maken en de groepering van pytest
        is daar wel iets waard, dus die volgorde blijft zoals pytest hem oplevert.
        """
        items = [
            self._Item("a", "a1"),
            self._Item("lokaal", "l1", sandbox=False),
            self._Item("b", "b1"),
            self._Item("a", "a2"),
        ]

        _houd_sandboxmodules_bij_elkaar(items)

        assert [item.naam for item in items] == ["a1", "l1", "a2", "b1"]
        assert items[1].naam == "l1", "de toets zonder marker is van zijn plek gegaan"
