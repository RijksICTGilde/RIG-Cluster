"""De beslislogica van de sandbox-helpers, zonder cluster.

``tests/e2e/helpers/cluster.py`` en ``tests/e2e/helpers/zad_cli.py`` zijn toetscode, maar ze
dragen de beslissingen waarop de sandboxmodules steunen: wanneer een psql-run geldig is,
wanneer een lezing een fout is, en of de CLI-toetsen draaien of overslaan. Die modules zelf
draaien alleen op een cluster, dus breekt daar iets in, dan valt dat pas op als iemand de
sandbox claimt. Deze toetsen draaien wel in een gewone ronde.
"""

from __future__ import annotations

import ast
import base64
import copy
import json
import logging
import os
import pathlib
import shutil
import subprocess
import sys
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, ClassVar

import httpx
import pytest
import yaml
from _pytest.outcomes import Skipped
from itsdangerous import TimestampSigner
from opi.utils.age import decrypt_age_content_sync, encrypt_age_content_sync
from tests.e2e import test_sandbox_migratie_006 as migratie_006
from tests.e2e import test_sandbox_registry_pull as registry_pull
from tests.e2e import test_sandbox_speelruimte as speelruimte
from tests.e2e import testserver
from tests.e2e.conftest import SANDBOX_TEST_USER, _houd_sandboxmodules_bij_elkaar
from tests.e2e.conftest import pytest_collection_modifyitems as ordeningshook
from tests.e2e.helpers import cluster, lifecycle, sandbox_api, weesprojecten, zad_cli

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

    def test_een_kale_naam_in_de_variabele_gaat_langs_het_pad(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Wie ``ZAD_CLI=zadctl-dev`` zet bedoelt een naam op het PATH, geen bestand hier.

        Zonder die opzoeking geeft ``cli_pad`` de kale naam terug, en een naam die niet op het
        PATH staat geeft dan een ``FileNotFoundError`` uit ``subprocess`` in plaats van de
        skipreden.
        """
        monkeypatch.setenv(zad_cli.CLI_ENV, "zadctl-dev")
        monkeypatch.setattr(zad_cli.shutil, "which", lambda naam: f"/opt/bin/{naam}")

        assert zad_cli.cli_pad() == "/opt/bin/zadctl-dev"

    def test_een_lege_variabele_is_geen_keuze(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``ZAD_CLI=`` (of een paar spaties) uit een shellscript mag het PATH niet dichtzetten."""
        monkeypatch.setenv(zad_cli.CLI_ENV, "   ")
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


class _OrdeningsItem:
    """Het minimum dat de twee ordeningscorrecties van een item aanraken."""

    def __init__(self, module: str, naam: str, *, sandbox: bool = True, serial: bool = False, regel: int = 0) -> None:
        self.module = SimpleNamespace(__name__=module)
        self.naam = naam
        self._markers = {"sandbox": sandbox, "serial": serial}
        self._regel = regel

    def get_closest_marker(self, naam: str) -> object | None:
        return object() if self._markers.get(naam) else None

    def reportinfo(self) -> tuple[str, int, str]:
        return "", self._regel, self.naam

    def __repr__(self) -> str:
        return self.naam


class TestSandboxmodulesBijElkaar:
    """De hook die de modulegrens terugzet, ``tests/e2e/conftest.py``.

    Wat hij voorkomt kost geld en geen falen: zonder hem wordt een module-fixture opnieuw
    opgezet, en dat is een volledig project op het gedeelde cluster terwijl de suite groen
    blijft. Het mechanisme staat in de docstring van de hook. Gemeten op de sandboxselectie:
    zonder deze hook 40 blokken over 30 modules, zeven modules gesplitst.
    """

    def test_de_toetsen_van_een_module_komen_achter_elkaar(self) -> None:
        items = [
            _OrdeningsItem("a", "a1"),
            _OrdeningsItem("b", "b1"),
            _OrdeningsItem("a", "a2"),
            _OrdeningsItem("b", "b2"),
            _OrdeningsItem("a", "a3"),
        ]

        _houd_sandboxmodules_bij_elkaar(items)

        assert [item.naam for item in items] == ["a1", "a2", "a3", "b1", "b2"]

    def test_de_module_die_het_eerst_kwam_blijft_voorop(self) -> None:
        """Op eerste verschijning en niet op naam: anders herschikt de hook de RUN zelf.

        Zonder deze toets is ``sorted(per_module)`` een even goed antwoord, en dan bepaalt de
        alfabetische bestandsnaam de volgorde waarin een suite van een uur zijn projecten
        aanmaakt.
        """
        items = [_OrdeningsItem("z", "z1"), _OrdeningsItem("a", "a1"), _OrdeningsItem("z", "z2")]

        _houd_sandboxmodules_bij_elkaar(items)

        assert [item.naam for item in items] == ["z1", "z2", "a1"]

    def test_een_toets_zonder_de_sandbox_marker_blijft_op_zijn_plek(self) -> None:
        """De hook mag alleen de posities van sandboxtoetsen onderling vullen.

        De lokale e2e-suite heeft niets met dit probleem te maken en de groepering van pytest
        is daar wel iets waard, dus die volgorde blijft zoals pytest hem oplevert.
        """
        items = [
            _OrdeningsItem("a", "a1"),
            _OrdeningsItem("lokaal", "l1", sandbox=False),
            _OrdeningsItem("b", "b1"),
            _OrdeningsItem("a", "a2"),
        ]

        _houd_sandboxmodules_bij_elkaar(items)

        assert [item.naam for item in items] == ["a1", "l1", "a2", "b1"]


class TestDeOrdeningshook:
    """``pytest_collection_modifyitems`` in ``tests/e2e/conftest.py``: de aanroep zelf.

    De twee correcties zijn los getoetst, de hook die ze aanroept was dat niet, en elk van de
    twee regels weghalen was nul rood. Gemeten op de sandboxselectie: zonder de
    groeperingsregel splitst de collectie weer op (zie ``TestSandboxmodulesBijElkaar``), en
    zonder de serial-regel verandert de volgorde zodra pytest-randomly aan staat, want dat is
    de schudder waar die correctie tegen staat.
    """

    def test_de_hook_doet_beide_correcties(self) -> None:
        """Een module die beide markers draagt, verspreid EN in de verkeerde regelvolgorde.

        Zonder de groepering blijft ``a2`` op positie 2 staan; zonder de serial-correctie komt
        hij voor ``a1``. Het antwoord hangt dus aan beide aanroepen.
        """
        items = [
            _OrdeningsItem("a", "a2", serial=True, regel=20),
            _OrdeningsItem("b", "b1"),
            _OrdeningsItem("a", "a1", serial=True, regel=10),
            _OrdeningsItem("b", "b2"),
        ]

        ordeningshook(items)

        assert [item.naam for item in items] == ["a1", "a2", "b1", "b2"]

    def test_de_hook_draait_na_de_groepering_van_pytest(self) -> None:
        """``trylast``, want pytest doet ``items[:] = reorder_items(items)`` in deze hook.

        Valt die optie weg, dan groepeert pytest de modules daarna alsnog uit elkaar en is de
        correctie zonder effect, terwijl alles hierboven groen blijft.
        """
        opties = ordeningshook.pytest_impl

        assert opties["trylast"] is True, f"de hook draait niet als laatste: {opties}"


class TestLimietUitYaml:
    """De lezer waarop de speelruimte-toetsen hun "niet veranderd" bouwen.

    ``test_sandbox_speelruimte.py`` leest de waarde voor en na een geweigerde poging en
    vergelijkt die twee. Vindt de lezer de vorm van het projectbestand niet, dan geeft hij
    beide keren ``None`` en is die vergelijking groen zonder iets gemeten te hebben. Het
    positieve gebruik in dezelfde module (``wait_for_condition`` op de opgeslagen waarde)
    pint hem wel, maar dat is een ANDERE toets en die draait alleen op een cluster.
    """

    @pytest.mark.parametrize(
        ("entry", "verwacht"),
        [
            ({"name": speelruimte._DIENST, "config": {speelruimte._VELD: 30}}, 30),
            ({speelruimte._DIENST: {"config": {speelruimte._VELD: 30}}}, 30),
            ({"name": speelruimte._DIENST, "config": {}}, None),
            ({"name": speelruimte._DIENST}, None),
            (speelruimte._DIENST, None),
        ],
        ids=["name-vorm", "sleutel-vorm", "config-zonder-veld", "zonder-config", "kale-dienst"],
    )
    def test_beide_vormen_van_een_dienstregel(self, entry: object, verwacht: object) -> None:
        """Een dienst met config staat in het projectbestand in twee vormen.

        ``{name: ..., config: ...}`` is wat de portaalkant schrijft (zie
        ``tests/test_connection_limit.py``), en ``{<dienst>: {config: ...}}`` staat in
        bestaande projectbestanden. Op een ervan lezen is een lezer die de helft niet vindt.
        """
        yaml = {"name": "proj", "services": ["publish-on-web", entry]}

        assert speelruimte._limiet_uit_yaml(yaml) == verwacht

    @pytest.mark.parametrize(
        "yaml",
        [{"services": ["publish-on-web"]}, {"services": []}, {"services": None}, {}],
        ids=["andere-dienst", "leeg", "none", "geen-sleutel"],
    )
    def test_zonder_de_dienst_is_het_none_en_geen_uitzondering(self, yaml: dict) -> None:
        assert speelruimte._limiet_uit_yaml(yaml) is None


class TestFixtureSleutelpaar:
    """Het AGE-sleutelpaar dat de testserver per run in een fixtureproject munt.

    De projectpagina ontsleutelt ``config/age-private-key`` met de PLATFORMsleutel voordat
    ze iets rendert, en die sleutel wordt per run gemunt (``testserver.test_age_keypair``).
    Een vooraf versleuteld blok in een fixture gaat daarna nooit meer open: dat gaf een 500
    op de projectpagina van elke fixture. ``_munt_projectsleutelpaar`` munt het paar daarom
    bij het laden.
    """

    @staticmethod
    def _fixtureproject(**config: object) -> dict:
        """Een fixture zoals hij op schijf staat: een paar dat nog gemunt moet worden."""
        return {
            "name": "sleutelpaar-proj",
            "config": {
                "age-public-key": "wordt-per-run-gemunt",
                "age-private-key": "wordt-per-run-gemunt",
                **config,
            },
        }

    @staticmethod
    def _platform_prive() -> str:
        return testserver.test_age_keypair()[0]

    def test_de_private_helft_gaat_open_met_de_platformsleutel_van_deze_run(self) -> None:
        """Dit is de lezing die de projectpagina doet voordat ze iets rendert."""
        data = self._fixtureproject()

        testserver._munt_projectsleutelpaar(data)

        geopend = decrypt_age_content_sync(data["config"]["age-private-key"], self._platform_prive())
        assert geopend is not None, "de private helft gaat niet open met de platformsleutel van deze run"
        assert geopend.startswith("AGE-SECRET-KEY-"), f"geen AGE-privesleutel maar {geopend[:24]!r}"

    def test_de_publieke_helft_is_een_ontvanger_en_niet_meer_de_placeholder(self) -> None:
        """De pagina toont deze waarde, en de wizard versleutelt ermee."""
        data = self._fixtureproject()

        testserver._munt_projectsleutelpaar(data)

        publiek = data["config"]["age-public-key"]
        assert publiek.startswith("age1"), f"geen AGE-ontvanger maar {publiek!r}"

    def test_de_twee_helften_zijn_een_paar(self) -> None:
        """Twee losse sleutels zien er allebei goed uit en werken samen niet.

        Wat het project met zijn eigen sleutel versleutelt (``api-key``, ``user-env-vars``)
        wordt met de andere helft teruggelezen, dus alleen als PAAR is dit bruikbaar.
        """
        data = self._fixtureproject()

        testserver._munt_projectsleutelpaar(data)

        prive = decrypt_age_content_sync(data["config"]["age-private-key"], self._platform_prive())
        assert prive is not None
        cijfertekst = encrypt_age_content_sync("een projectgeheim", data["config"]["age-public-key"])
        assert decrypt_age_content_sync(cijfertekst, prive) == "een projectgeheim"

    @pytest.mark.parametrize(
        "data",
        [
            {"name": "p"},
            {"name": "p", "config": None},
            {"name": "p", "config": "config-als-tekst"},
            {"name": "p", "config": {}},
            {"name": "p", "config": {"api-key": "klaartekst"}},
            {"name": "p", "config": {"age-private-key": ""}},
        ],
        ids=[
            "geen-config",
            "config-none",
            "config-geen-map",
            "lege-config",
            "config-zonder-sleutel",
            "lege-sleutel",
        ],
    )
    def test_een_fixture_zonder_private_helft_blijft_zoals_hij_stond(self, data: dict) -> None:
        """Alleen een fixture die al een private helft DRAAGT krijgt een nieuw paar.

        Twee kanten. Een project zonder die sleutel hoort er ook geen te krijgen: het
        sleutelblok op de projectpagina hangt aan ``config.age-private-key``, dus er een
        bijzetten laat een paneel renderen dat de fixture niet vraagt. En een ``config`` die
        geen map is mag geen AttributeError geven: de lader loopt over elk yaml-bestand in
        de map, en wat daar staat bepaalt hij niet.
        """
        voor = copy.deepcopy(data)

        testserver._munt_projectsleutelpaar(data)

        assert data == voor

    def test_elke_fixture_op_schijf_komt_met_een_leesbaar_paar_uit_de_lader(self) -> None:
        """De lader zelf, over de echte fixtures: dit is de stand die de server krijgt.

        De toetsen hierboven meten de functie; deze meet dat de lader hem AANROEPT, voor
        elke fixture die een sleutelpaar draagt.
        """
        projecten = testserver._load_fixture_projects()
        met_sleutel = {p["name"]: p["config"] for p in projecten if (p.get("config") or {}).get("age-private-key")}
        assert met_sleutel, f"geen fixtureproject met een age-private-key onder {testserver.FIXTURE_DIR}"

        for naam, config in met_sleutel.items():
            prive = decrypt_age_content_sync(config["age-private-key"], self._platform_prive())
            assert prive, f"{naam}: de private helft gaat niet open met de platformsleutel van deze run"
            cijfertekst = encrypt_age_content_sync(naam, config["age-public-key"])
            assert decrypt_age_content_sync(cijfertekst, prive) == naam, f"{naam}: de helften zijn geen paar"

    def test_geen_fixture_draagt_een_api_key_die_niemand_meer_kan_openen(self) -> None:
        """``config/api-key`` wordt met de PROJECTsleutel geopend, en die is per run nieuw.

        De projectpagina doet dat zonder vangnet (``router.py``, ``decrypt_password_smart``
        op ``config/api-key``), dus een AGE-blok daar geeft een 500 op elke projectpagina.
        Hermunten helpt niet zoals bij het sleutelpaar: de klaartekst eronder is dan weg.
        De regel staat in features/e2e-ui-testing.md, onder Adding Fixture Data.
        """
        bestanden = sorted(testserver.FIXTURE_DIR.glob("*.yaml"))
        assert bestanden, f"geen fixtures gevonden onder {testserver.FIXTURE_DIR}"

        versleuteld = []
        for pad in bestanden:
            data = yaml.safe_load(pad.read_text()) or {}
            api_key = str((data.get("config") or {}).get("api-key") or "")
            if "BEGIN AGE ENCRYPTED FILE" in api_key:
                versleuteld.append(pad.name)

        assert not versleuteld, f"config/api-key staat als AGE-blok in: {versleuteld}"


class _NepLocator:
    """Een locator die precies zoveel kan als de voortgangshelper van hem vraagt."""

    def __init__(self, attributen: dict[str, str] | None) -> None:
        self._attributen = attributen

    @property
    def first(self) -> _NepLocator:
        return self

    def count(self) -> int:
        return 0 if self._attributen is None else 1

    def wait_for(self, **_: Any) -> None:
        if self._attributen is None:
            raise AssertionError("element niet gevonden")

    def get_attribute(self, naam: str) -> str | None:
        return (self._attributen or {}).get(naam)


class _NepVoortgangspagina:
    """De voortgangspagina zoals de helper hem leest: een detaillink plus een alert.

    De detaillink draagt de projectnaam en staat er zodra de taak klaar is, geslaagd of
    niet; de alert draagt de uitkomst in zijn ``variant``.
    """

    def __init__(self, naam: str, *, variant: str = "success", href: str | None = None) -> None:
        self.naam = naam
        self.variant = variant
        self.href = href if href is not None else f"/projects/{naam}/details"
        self.url = "https://zad.sandbox.test/projects/progress/taak-1"

    def locator(self, selector: str) -> _NepLocator:
        if "/details" in selector:
            return _NepLocator({"href": self.href})
        if "alert" in selector:
            return _NepLocator({"variant": self.variant, "text": f"melding over {self.naam}"})
        raise AssertionError(f"onverwachte selector: {selector}")


class _NepZadProjects:
    """De listing van zad-projects: waar het projectbestand wel of niet in staat."""

    def __init__(self, *namen: str) -> None:
        self.namen = set(namen)

    def list_project_names(self) -> set[str]:
        return set(self.namen)

    def get_first_deployment_name(self, naam: str) -> str:
        return f"{naam}-productie"


class TestOpruimenNaEenHalveAanmaak:
    """Een aanmaak die omvalt NADAT het projectbestand geschreven is, ruimt het alsnog op.

    Dat bestand is de bron: de app-of-apps maakt de ArgoCD-applicaties er steeds opnieuw
    uit aan, dus een achterblijver is niet weg te krijgen door het cluster op te ruimen.
    De aanroepers ruimen op in de ``finally`` van hun fixture en hebben daar alleen de
    ``CreatedProject`` die de helper bij SUCCES teruggeeft, dus bij een mislukking moet de
    helper het zelf doen.
    """

    @staticmethod
    def _richt_in(
        monkeypatch: pytest.MonkeyPatch,
        *,
        naam: str = "speel-3ub",
        variant: str = "success",
        sleutel_leesbaar: bool = True,
        bestand_in_repo: bool = True,
        href: str | None = None,
    ) -> tuple[_NepVoortgangspagina, _NepZadProjects, list[tuple[str, str]]]:
        """Zet de wizardhelper neer zonder browser, cluster of API.

        Geeft (pagina, zad-projects, verwijderingen) terug. Een verwijdering die de echte
        route zou doen haalt het bestand hier uit de listing, zodat een toets op de vraag
        "staat het er nog?" hetzelfde meet als op de sandbox.
        """
        pagina = _NepVoortgangspagina(naam, variant=variant, href=href)
        repo = _NepZadProjects(*([naam] if bestand_in_repo else []))
        verwijderd: list[tuple[str, str]] = []

        def _verwijder(base_url: str, project: str, api_key: str, **_: Any) -> None:
            verwijderd.append((project, api_key))
            repo.namen.discard(project)

        def _sleutel(_page: Any, _base_url: str, project: str, **_: Any) -> str:
            if not sleutel_leesbaar:
                raise AssertionError(f"Could not read API key for '{project}'")
            return "sleutel-van-" + project

        monkeypatch.setattr(lifecycle, "walk_create_wizard_with_services", lambda *a, **k: None)
        monkeypatch.setattr(lifecycle, "walk_create_wizard", lambda *a, **k: None)
        monkeypatch.setattr(lifecycle, "read_api_key_with_retry", _sleutel)
        monkeypatch.setattr(cluster, "wait_for_project_apps_healthy", lambda *a, **k: True)
        monkeypatch.setattr(sandbox_api, "delete_project_via_api", _verwijder)
        return pagina, repo, verwijderd

    @staticmethod
    def _maak_aan(pagina: Any, repo: Any) -> Any:
        return lifecycle.create_project_with_services(
            pagina,
            "https://zad.sandbox.test",
            repo,
            "speelruimte",
            user_email="admin@sandbox.test",
            services=["publish-on-web"],
        )

    def test_een_mislukte_aanmaaktaak_laat_geen_projectbestand_achter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """De gemeten vorm: de taak schrijft het bestand en valt daarna om.

        De aanmaak hoort nog steeds te falen (de toets die hem aanriep moet rood worden),
        maar het bestand hoort niet in zad-projects achter te blijven.
        """
        pagina, repo, verwijderd = self._richt_in(monkeypatch, variant="error")

        with pytest.raises(AssertionError, match="niet geslaagd"):
            self._maak_aan(pagina, repo)

        assert "speel-3ub" not in repo.list_project_names(), "het projectbestand bleef in zad-projects staan"
        assert verwijderd == [("speel-3ub", "sleutel-van-speel-3ub")]

    def test_een_halfgeschreven_bestand_wordt_ook_opgeruimd(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Niet alleen de taakuitkomst: elke stap na het lezen van de naam kan omvallen.

        Hier valt het uitlezen van de eerste deployment om, zoals bij een bestand dat wel
        geschreven is maar nog geen deployments draagt.
        """
        pagina, repo, verwijderd = self._richt_in(monkeypatch)
        monkeypatch.setattr(
            repo, "get_first_deployment_name", lambda naam: _gooi(AssertionError(f"'{naam}' has no deployments"))
        )

        with pytest.raises(AssertionError, match="no deployments"):
            self._maak_aan(pagina, repo)

        assert "speel-3ub" not in repo.list_project_names()
        assert verwijderd == [("speel-3ub", "sleutel-van-speel-3ub")]

    def test_een_geslaagde_aanmaak_ruimt_niets_op(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """De andere kant, en de gevaarlijke: een geslaagd project mag niet weg.

        De toets erna werkt met dit project; wordt het hier opgeruimd, dan is de hele
        reparatie erger dan de fout.
        """
        pagina, repo, verwijderd = self._richt_in(monkeypatch)

        project = self._maak_aan(pagina, repo)

        assert project.name == "speel-3ub"
        assert project.api_key == "sleutel-van-speel-3ub"
        assert project.deployment_name == "speel-3ub-productie"
        assert verwijderd == [], "een geslaagde aanmaak werd opgeruimd"
        assert "speel-3ub" in repo.list_project_names()

    def test_de_wizard_zonder_diensten_ruimt_net_zo_op(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Beide aanmaakhelpers lopen langs dezelfde afronding, en dat moet zo blijven.

        ``create_project_via_wizard`` heeft zijn eigen wizardgang maar dezelfde staart;
        zonder deze toets is de opruiming daar ongepind.
        """
        pagina, repo, verwijderd = self._richt_in(monkeypatch, naam="waard-vqs", variant="error")

        with pytest.raises(AssertionError, match="niet geslaagd"):
            lifecycle.create_project_via_wizard(
                pagina,
                "https://zad.sandbox.test",
                repo,
                "waarden",
                user_email="admin@sandbox.test",
            )

        assert "waard-vqs" not in repo.list_project_names()
        assert verwijderd == [("waard-vqs", "sleutel-van-waard-vqs")]

    def test_geen_module_rondt_de_wizard_zelf_af(self) -> None:
        """De aanroep pinnen, niet alleen de helper.

        Vijf modules lopen hun eigen wizardgang en lazen daarna zelf de voortgangspagina
        uit. Daarmee hadden ze precies hetzelfde gat als de wizardhelpers: viel er iets om,
        dan had hun fixture geen naam om mee op te ruimen. Ze gaan nu alle vijf door
        ``naam_en_sleutel_na_de_wizard``, en dat hoort zo te blijven: wie de losse stappen
        weer zelf aanroept, bouwt het gat opnieuw.
        """
        stappen = ("naam_van_de_voortgangspagina", "assert_aanmaak_geslaagd")
        # Zonder dit meet de grep hieronder niets meer zodra iemand een stap hernoemt.
        weg = [stap for stap in stappen if not hasattr(lifecycle, stap)]
        assert not weg, f"deze toets zoekt naar stappen die niet meer bestaan: {weg}"

        modules = sorted((pathlib.Path(__file__).parent / "e2e").glob("test_*.py"))
        assert len(modules) > 50, f"de meting vond maar {len(modules)} e2e-modules"

        los = {module.name for module in modules for stap in stappen if f"{stap}(" in module.read_text()}

        assert not los, (
            f"deze modules roepen de losse stappen van de afronding aan: {sorted(los)}. "
            "Gebruik naam_en_sleutel_na_de_wizard of een van de create-helpers."
        )

    def test_een_aanmaak_zonder_projectbestand_valt_om_en_ruimt_op(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Dat de wizard iets aanmaakte zegt nog niet dat het in zad-projects staat.

        Die controle is wat deze suite bewaakt, en ze staat sinds deze ronde BINNEN de
        opruiming: valt ze om, dan is er een project waarvan niemand het bestand ziet maar
        dat wel bestaat, en dat hoort net zo goed weg.
        """
        pagina, repo, verwijderd = self._richt_in(monkeypatch, bestand_in_repo=False)

        with pytest.raises(AssertionError, match="staat niet in zad-projects"):
            self._maak_aan(pagina, repo)

        assert verwijderd == [("speel-3ub", "sleutel-van-speel-3ub")]

    def test_een_voortgangspagina_zonder_projectnaam_zegt_dat(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """De eerste stap van de keten: de detaillink is waar de naam vandaan komt.

        Draagt die link geen project, dan hoort de helper dat te zeggen in plaats van op een
        lege match te struikelen. Er gaat dan ook geen verwijdering de deur uit, en dat is
        het venster dat deze PR open laat: zonder naam weet de opruiming niet wat ze pakken
        moet, en is het gereedschap uit ``weesprojecten.py`` de weg.
        """
        # Wel een "/details"-link (anders vindt de selector hem niet), maar zonder project
        # ertussen: de vorm die de regex van de helper niet kan lezen.
        pagina, repo, verwijderd = self._richt_in(monkeypatch, href="/projects/details")

        with pytest.raises(AssertionError, match="wijst niet naar een project"):
            self._maak_aan(pagina, repo)

        assert verwijderd == [], "er is iets verwijderd terwijl de naam niet bekend was"

    def test_de_opruiming_volgt_de_ssl_instelling_van_de_sandbox(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Een cluster met een eigen certificaat mag niet juist in het foutpad onbereikbaar zijn.

        De opruiming die de modules zelf doen leest ``E2E_API_VERIFY_SSL``; deze doet dat
        via ``API_VERIFY_SSL``. Met een vaste waarde hier zou juist het foutpad op het
        certificaat van de sandbox kunnen stranden.
        """
        pagina, repo, _ = self._richt_in(monkeypatch, variant="error")
        opties: list[dict[str, Any]] = []
        monkeypatch.setattr(sandbox_api, "delete_project_via_api", lambda *_a, **k: opties.append(k))
        monkeypatch.setattr(lifecycle, "API_VERIFY_SSL", True)

        with pytest.raises(AssertionError, match="niet geslaagd"):
            self._maak_aan(pagina, repo)

        assert opties == [{"verify_ssl": True}]

    def test_een_onleesbare_sleutel_verbergt_de_echte_fout_niet(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Zonder API-sleutel kan de opruiming niet, en dan mag ze niets kapotmaken.

        De opruiming loopt in een ``finally``: gooit ze zelf, dan vervangt haar fout de
        fout die de toets moest lezen. Ze hoort te zeggen welk project blijft staan.
        """
        pagina, repo, verwijderd = self._richt_in(monkeypatch, variant="error", sleutel_leesbaar=False)

        with caplog.at_level(logging.ERROR), pytest.raises(AssertionError, match="niet geslaagd"):
            self._maak_aan(pagina, repo)

        assert verwijderd == []
        assert "speel-3ub" in repo.list_project_names()
        assert any("speel-3ub" in bericht for bericht in caplog.messages), (
            "er is niet gemeld welk project op de sandbox blijft staan"
        )


def _gooi(fout: Exception) -> Any:
    raise fout


class TestDeImportsUitLifecycle:
    """Wie uit ``lifecycle`` importeert noemt een naam die er is.

    Deze helper is opgesplitst en zijn functies zijn hernoemd, en zo'n hernoeming valt pas
    bij het DRAAIEN om. Geen enkele poort ziet dat: ``tests`` en ``scripts`` staan beide in
    de exclude van pyright, en ruff controleert niet of een geimporteerde naam bestaat. Een
    importerende module kan dus stil onstartbaar worden.
    """

    def test_elke_import_uit_lifecycle_bestaat(self) -> None:
        wortel = pathlib.Path(__file__).parent.parent
        ontbreekt: list[str] = []
        importerend: set[str] = set()
        for map_ in ("tests", "scripts"):
            for pad in sorted((wortel / map_).rglob("*.py")):
                boom = ast.parse(pad.read_text())
                for knoop in ast.walk(boom):
                    if not isinstance(knoop, ast.ImportFrom) or not (knoop.module or "").endswith("helpers.lifecycle"):
                        continue
                    importerend.add(str(pad.relative_to(wortel)))
                    ontbreekt += [
                        f"{pad.relative_to(wortel)}: {alias.name}"
                        for alias in knoop.names
                        if not hasattr(lifecycle, alias.name)
                    ]

        assert len(importerend) > 20, f"de meting vond maar {len(importerend)} modules die uit lifecycle importeren"
        assert not ontbreekt, f"deze imports bestaan niet meer in lifecycle: {sorted(ontbreekt)}"


class _NepMeetbankpagina(_NepVoortgangspagina):
    """De voortgangspagina plus het beetje navigatie dat de meetbank zelf doet."""

    def goto(self, _url: str, **_: Any) -> None:
        return None

    def wait_for_timeout(self, _ms: float) -> None:
        return None

    def screenshot(self, **_: Any) -> None:
        # Niet weglaten: zonder deze methode valt een meetbank die na een mislukte aanmaak
        # doorloopt om op "geen attribuut 'screenshot'", en dat leest als een onvolledige
        # nepklasse. Wie de klasse dan aanvult maakt de toets groen met de fout erin.
        raise AssertionError("de meetbank maakte een schermafdruk: ze liep door na een mislukte aanmaak")


class _NepPlaywright:
    """De keten ``sync_playwright() -> chromium.launch() -> new_context() -> new_page()``."""

    def __init__(self, pagina: _NepMeetbankpagina) -> None:
        self.pagina = pagina
        self.chromium = self

    def __enter__(self) -> _NepPlaywright:
        return self

    def __exit__(self, *_: object) -> bool:
        return False

    def launch(self, **_: Any) -> _NepPlaywright:
        return self

    def new_context(self, **_: Any) -> _NepPlaywright:
        return self

    def new_page(self) -> _NepMeetbankpagina:
        return self.pagina

    def close(self) -> None:
        return None


_PYTHON_WORTEL = pathlib.Path(__file__).parent.parent


def _meetbanken() -> list[str]:
    """De scripts die uit ``tests/`` importeren: precies die hebben de wortel nodig."""
    gevonden = [
        f"scripts/{pad.name}"
        for pad in sorted((_PYTHON_WORTEL / "scripts").glob("*.py"))
        if any(
            isinstance(knoop, ast.ImportFrom) and (knoop.module or "").startswith("tests.")
            for knoop in ast.walk(ast.parse(pad.read_text()))
        )
    ]
    # Een lege parametrize SLAAT OVER in plaats van te falen, dus de vondst heeft zijn eigen
    # grendel: raakt deze meting stuk, dan valt dat hier op en niet als groen vinkje.
    assert len(gevonden) >= 3, f"de meting vond maar {len(gevonden)} scripts die uit tests/ importeren"
    return gevonden


_MEETBANKEN = _meetbanken()


class TestDeMeetbankscripts:
    """De scripts in ``scripts/`` die uit ``tests/`` importeren, blijven startbaar.

    Het zijn geen poorten maar meetbanken, en ze draaien met de hand: een hernoeming in de
    helpers of een pad dat net niet klopt valt daar pas bij het DRAAIEN om, lang nadat de
    ronde die het veroorzaakte voorbij is. Geen enkele poort ziet het, want ``tests`` en
    ``scripts`` staan beide in de exclude van pyright en ruff controleert geen namen.
    """

    def test_een_mislukte_aanmaak_stopt_meteen_en_laat_het_project_staan(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``doorloop_rc108.py`` gebruikt de twee losse stappen, en wel beide.

        Het script belooft in zijn docstring dat het project BLIJFT STAAN als er iets
        misgaat, dus het mag niet door ``naam_en_sleutel_na_de_wizard`` lopen: die ruimt
        juist op. Maar het oordeel over de uitkomst moet er wel bij blijven, want alleen de
        naam lezen laat een mislukte aanmaak doorlopen naar
        ``wait_for_project_apps_healthy(timeout=300)``, en dan meldt de meetbank zich pas na
        vijf minuten en zonder de reden van de taak.
        """
        # Hier en niet boven: een onstartbare meetbank hoort EEN toets rood te maken. Als
        # import bovenaan wordt het een collectiefout, en die neemt de andere toetsen in dit
        # bestand mee, inclusief de driftgrendel die de ontbrekende naam juist benoemt.
        from scripts import doorloop_rc108 as doorloop

        pagina = _NepMeetbankpagina("speel-3ub", variant="error")
        gewacht: list[str] = []
        verwijderd: list[str] = []

        monkeypatch.setattr(doorloop, "sync_playwright", lambda: _NepPlaywright(pagina))
        monkeypatch.setattr(
            doorloop.httpx, "get", lambda *a, **k: SimpleNamespace(json=lambda: {"version": "abc1234-meting"})
        )
        monkeypatch.setattr(doorloop, "walk_create_wizard_with_services", lambda *a, **k: None)
        monkeypatch.setattr(cluster, "wait_for_project_apps_healthy", lambda naam, **k: gewacht.append(naam))
        monkeypatch.setattr(lifecycle, "read_api_key_with_retry", lambda *a, **k: "sleutel")
        monkeypatch.setattr(sandbox_api, "delete_project_via_api", lambda _u, naam, *a, **k: verwijderd.append(naam))
        monkeypatch.setattr(doorloop, "delete_project_via_api", lambda _u, naam, *a, **k: verwijderd.append(naam))
        monkeypatch.setattr("sys.argv", ["doorloop_rc108.py", "--commit", "abc1234", "--uit", str(tmp_path)])

        with pytest.raises(AssertionError, match="niet geslaagd"):
            doorloop.main()

        assert gewacht == [], "de meetbank liep na een mislukte aanmaak door naar de wacht op Healthy"
        assert verwijderd == [], "de meetbank ruimde op, terwijl ze belooft het project te laten staan"

    @pytest.mark.parametrize("script", _MEETBANKEN)
    def test_de_gedocumenteerde_aanroep_start(self, script: str) -> None:
        """De vorm uit de docstring van elk script, als LOS proces, want juist die viel om.

        De toets hierboven importeert de module en ziet daarmee niet dat python bij een
        aanroep met een BESTANDSNAAM de map van het script op ``sys.path`` zet en niet de
        projectwortel. Dat maakt een meetbank net zo onstartbaar als een import die niet
        meer bestaat, en pytest merkt het niet omdat hij zelf vanaf de wortel draait.
        """
        uitkomst = subprocess.run(
            [sys.executable, script, "--help"],
            cwd=_PYTHON_WORTEL,
            capture_output=True,
            text=True,
            timeout=120,
        )

        assert uitkomst.returncode == 0, f"{script} start niet: {uitkomst.stderr[-800:]}"


class _NepAntwoord:
    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


class _NepHttpClient:
    """Een httpx-client die de verzoeken opschrijft en de csrf-cookie zet zoals OPI dat doet."""

    laatste: _NepHttpClient | None = None
    #: Per toets te zetten met monkeypatch; een exemplaar leest ze hier.
    get_status = 200
    post_status = 200
    post_body = ""
    #: Lichaam per URL-stuk, voor de pollroute van het voortgangsfragment.
    lichamen: ClassVar[dict[str, str]] = {}
    #: Antwoorden op de pollroute, in volgorde; het laatste geldt ook voor latere polls.
    poll_antwoorden: ClassVar[list[tuple[int, str]]] = []
    #: Staat hier een listing, dan haalt een aangenomen POST het projectbestand eruit,
    #: zoals de verwijderroute van OPI dat doet.
    repo_na_post: _NepZadProjects | None = None

    def __init__(self, **opties: Any) -> None:
        self.opties = opties
        self.cookies = httpx.Cookies()
        self.verzoeken: list[tuple[str, str, dict[str, str]]] = []
        self._gepold = 0
        type(self).laatste = self

    def __enter__(self) -> _NepHttpClient:
        return self

    def __exit__(self, *_: Any) -> bool:
        return False

    def get(self, url: str, **_: Any) -> _NepAntwoord:
        self.verzoeken.append(("GET", url, {}))
        if self.get_status == 200:
            # De middleware munt de cookie op het antwoord wanneer er nog geen is.
            self.cookies.set("csrf_token", "token-uit-de-cookie")
        if "task-progress" in url and self.poll_antwoorden:
            status, lichaam = self.poll_antwoorden[min(self._gepold, len(self.poll_antwoorden) - 1)]
            self._gepold += 1
            return _NepAntwoord(status, lichaam)
        lichaam = next((tekst for stuk, tekst in self.lichamen.items() if stuk in url), "")
        return _NepAntwoord(self.get_status, lichaam)

    def post(self, url: str, *, headers: dict[str, str] | None = None, **_: Any) -> _NepAntwoord:
        self.verzoeken.append(("POST", url, dict(headers or {})))
        if self.post_status == 200 and self.repo_na_post is not None:
            self.repo_na_post.namen.discard(url.rsplit("/", 1)[-1])
        return _NepAntwoord(self.post_status, "geweigerd" if self.post_status != 200 else self.post_body)


class TestWeesprojectVerwijderen:
    """Het gereedschap voor achterblijvers: de webroute, zonder browser.

    De v1-route wil de eigen API-sleutel van het project en die staat alleen op de
    detailpagina, dus zonder browser blijft de webroute over. Die wil drie dingen tegelijk
    (sessiecookie, csrf-header uit de cookie, Origin op de host); wie er een vergeet krijgt
    een 403 die niet zegt welke.
    """

    @staticmethod
    def _client(monkeypatch: pytest.MonkeyPatch) -> type[_NepHttpClient]:
        monkeypatch.setattr(weesprojecten.httpx, "Client", _NepHttpClient)
        # ``laatste`` is een klasse-attribuut: zonder deze reset leest een toets die op
        # het UITBLIJVEN van een verzoek grendelt de client van de vorige toets.
        monkeypatch.setattr(_NepHttpClient, "laatste", None)
        return _NepHttpClient

    def test_het_verzoek_draagt_de_sessie_de_csrf_header_en_de_origin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Alle drie, en de csrf-header moet de waarde UIT de cookie zijn (double submit)."""
        self._client(monkeypatch)

        weesprojecten.start_verwijdering("https://zad.sandbox.test/", "test-ej1", secret_key="x" * 32)

        client = _NepHttpClient.laatste
        assert client is not None
        methode, url, headers = client.verzoeken[-1]
        assert (methode, url) == ("POST", "https://zad.sandbox.test/projects/delete/test-ej1")
        assert headers["x-csrf-token"] == client.cookies.get("csrf_token")
        assert headers["Origin"] == "https://zad.sandbox.test"
        sessie = client.cookies.get("session")
        assert sessie, "er ging geen sessiecookie mee"
        ondertekend = TimestampSigner("x" * 32).unsign(sessie)
        assert json.loads(base64.b64decode(ondertekend))["user"]["email"] == SANDBOX_TEST_USER["email"]

    def test_een_sessie_die_niet_wordt_aanvaard_zegt_dat_voor_de_post(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Een omleiding naar de login hoort hier te stoppen, niet in een 403 verderop."""
        self._client(monkeypatch)
        monkeypatch.setattr(_NepHttpClient, "get_status", 307)

        with pytest.raises(RuntimeError, match="E2E_SECRET_KEY"):
            weesprojecten.start_verwijdering("https://zad.sandbox.test", "test-ej1", secret_key="x" * 32)

        client = _NepHttpClient.laatste
        assert client is not None
        assert [m for m, _, _ in client.verzoeken] == ["GET"], "er is gepost zonder geldige sessie"

    def test_een_geweigerd_verzoek_draagt_het_antwoord_van_de_server(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Een 403 "CSRF token missing" moet de aanroeper bereiken, niet een kale fout."""
        self._client(monkeypatch)
        monkeypatch.setattr(_NepHttpClient, "post_status", 403)

        with pytest.raises(RuntimeError, match=r"403.*geweigerd"):
            weesprojecten.start_verwijdering("https://zad.sandbox.test", "test-ej1", secret_key="x" * 32)

    def test_een_weigering_van_opi_komt_eruit_als_zijn_eigen_melding(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """De taak volgen in plaats van de klok.

        Het voortgangsfragment ververst zichzelf alleen zolang de taak loopt, dus het
        ontbreken van die poll is de eindtoestand. Gemeten op de sandbox: OPI weigerde
        rest4-dqp na 18 seconden te verwijderen (een MinIO-gebruiker van het project
        bestond niet meer) terwijl de opruiming 600 seconden deed over "staat er nog".
        """
        self._client(monkeypatch)
        monkeypatch.setattr(_NepHttpClient, "post_body", '<div hx-get="/projects/rest4-dqp/task-progress/7">')
        monkeypatch.setattr(
            _NepHttpClient,
            "lichamen",
            {"task-progress": "<p>Failed: Project 'rest4-dqp' niet volledig verwijderd: policy weg</p>"},
        )
        repo = _NepZadProjects("rest4-dqp")
        monkeypatch.setattr(repo, "wait_for_project_gone", lambda naam, **_: False, raising=False)

        with caplog.at_level(logging.ERROR):
            weg = weesprojecten.verwijder_weesproject("https://zad.sandbox.test", "rest4-dqp", repo, timeout=30.0)

        assert weg is False
        assert "niet volledig verwijderd" in caplog.text, f"de reden van OPI ontbreekt: {caplog.text}"
        client = _NepHttpClient.laatste
        assert client is not None
        polls = [url for methode, url, _ in client.verzoeken if methode == "GET" and "task-progress" in url]
        assert len(polls) == 1, (
            f"het fragment zei al dat de taak klaar was, maar er is {len(polls)}x gepold: "
            "dan wacht de opruiming alsnog zijn time-out uit"
        )

    def test_een_project_dat_blijft_staan_geeft_een_exitcode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """De taak in de Taskfile moet rood worden, anders leest een mislukte opruiming als geslaagd."""
        monkeypatch.setattr(weesprojecten, "_forgejo_uit_de_omgeving", lambda: _NepZadProjects("rest4-dqp"))
        monkeypatch.setattr(weesprojecten, "verwijder_weesproject", lambda *a, **k: False)

        assert weesprojecten.main(["rest4-dqp"]) == 1

        monkeypatch.setattr(weesprojecten, "verwijder_weesproject", lambda *a, **k: True)
        assert weesprojecten.main(["rest4-dqp"]) == 0

    def test_de_taak_loopt_de_hele_rij_namen_af_en_noemt_wie_blijft_staan(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Meerdere namen mogen, dus een naam die struikelt mag de rij niet afbreken.

        Een weigering van de webroute komt eruit als een ``RuntimeError``. Die hoort als
        achterblijver gemeld te worden, niet als een traceback over de rest van de rij.
        """
        monkeypatch.setattr(weesprojecten, "_forgejo_uit_de_omgeving", _NepZadProjects)
        behandeld: list[str] = []

        def _verwijder(_base_url: str, naam: str, *_a: Any, **_k: Any) -> bool:
            behandeld.append(naam)
            if naam == "test-vim":
                raise RuntimeError("403 CSRF token missing")
            return naam != "waard-vqs"

        monkeypatch.setattr(weesprojecten, "verwijder_weesproject", _verwijder)

        with caplog.at_level(logging.ERROR):
            code = weesprojecten.main(["test-ej1", "test-vim", "waard-vqs"])

        assert code == 1
        assert behandeld == ["test-ej1", "test-vim", "waard-vqs"]
        assert "Blijven staan: test-vim, waard-vqs" in caplog.text

    def test_een_wees_is_pas_opgeruimd_als_zijn_bestand_weg_is(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Het BESTAND is de maat: zolang het er staat maakt de app-of-apps de apps opnieuw aan.

        Een aangenomen verzoek is dus geen opruiming: de route antwoordt met het
        voortgangsfragment van een taak die nog moet lopen.
        """
        self._client(monkeypatch)
        repo = _NepZadProjects("test-ej1")
        monkeypatch.setattr(repo, "wait_for_project_gone", lambda naam, **_: naam not in repo.namen, raising=False)

        assert weesprojecten.verwijder_weesproject("https://zad.sandbox.test", "test-ej1", repo) is False

    def test_een_wees_die_echt_verdwijnt_komt_als_geslaagd_terug(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """De geslaagde weg, met het bestand dat tijdens de taak uit de listing verdwijnt."""
        self._client(monkeypatch)
        repo = _NepZadProjects("test-ej1")
        monkeypatch.setattr(_NepHttpClient, "repo_na_post", repo)
        monkeypatch.setattr(repo, "wait_for_project_gone", lambda naam, **_: naam not in repo.namen, raising=False)

        assert weesprojecten.verwijder_weesproject("https://zad.sandbox.test", "test-ej1", repo) is True

        client = _NepHttpClient.laatste
        assert client is not None
        assert [url for methode, url, _ in client.verzoeken if methode == "POST"] == [
            "https://zad.sandbox.test/projects/delete/test-ej1"
        ]

    def test_een_naam_die_er_niet_staat_is_niets_om_op_te_ruimen(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Al opgeruimd, of een typefout: dan hoort er geen verwijdering de deur uit te gaan.

        Het gereedschap krijgt meerdere namen mee en wordt herhaald gedraaid, dus dit is
        de gewone toestand en geen rand.
        """
        self._client(monkeypatch)
        repo = _NepZadProjects("test-vim")

        assert weesprojecten.verwijder_weesproject("https://zad.sandbox.test", "test-ej1", repo) is True

        assert _NepHttpClient.laatste is None, "er is een verzoek gedaan voor een project dat er niet staat"

    def test_de_voortgang_wordt_gevolgd_zolang_de_taak_loopt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pollen tot het fragment zijn eigen verversing laat vallen, niet tot de klok afloopt."""
        self._client(monkeypatch)
        monkeypatch.setattr(
            _NepHttpClient,
            "poll_antwoorden",
            [
                (200, '<div hx-get="/projects/test-ej1/task-progress/7">bezig</div>'),
                (200, "<p>Project 'test-ej1' verwijderd</p>"),
            ],
        )

        uitkomst = weesprojecten.wacht_tot_de_taak_klaar_is(
            "https://zad.sandbox.test",
            "/projects/test-ej1/task-progress/7",
            secret_key="x" * 32,
            timeout=30.0,
            interval=0.0,
        )

        assert "verwijderd" in uitkomst
        client = _NepHttpClient.laatste
        assert client is not None
        assert len([url for _, url, _ in client.verzoeken if "task-progress" in url]) == 2

    def test_een_onleesbare_voortgang_stopt_met_de_statuscode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verdwijnt de taak uit OPI, dan is dat de uitkomst en geen reden om te blijven pollen."""
        self._client(monkeypatch)
        monkeypatch.setattr(_NepHttpClient, "poll_antwoorden", [(404, "")])

        uitkomst = weesprojecten.wacht_tot_de_taak_klaar_is(
            "https://zad.sandbox.test",
            "/projects/test-ej1/task-progress/7",
            secret_key="x" * 32,
            timeout=30.0,
            interval=0.0,
        )

        assert "HTTP 404" in uitkomst
        client = _NepHttpClient.laatste
        assert client is not None
        assert len([url for _, url, _ in client.verzoeken if "task-progress" in url]) == 1


@pytest.mark.skipif(shutil.which("task") is None, reason="vraagt de go-task binary")
class TestDeOpruimtaak:
    """``task sandbox:verwijder-weesproject`` zoals go-task hem rendert.

    Twee beloftes staan alleen in de Taskfile en zijn aan de pythonkant niet rood te
    krijgen: zonder NAAM draait er niets, en meerdere namen mogen. ``--dry`` rendert het
    commando zonder het te draaien, dus hier wordt geen sandbox aangeraakt.
    """

    taak: ClassVar[str] = "sandbox:verwijder-weesproject"

    @classmethod
    def _draai(cls, *argumenten: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["task", "--dry", "-v", cls.taak, *argumenten],
            cwd=_PYTHON_WORTEL.parents[1],
            capture_output=True,
            text=True,
            timeout=180,
            env={"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "")},
        )

    @classmethod
    def _commando(cls, *argumenten: str) -> str:
        """De regel die go-task zou uitvoeren; die schrijft hij naar stderr, niet naar stdout."""
        uitkomst = cls._draai(*argumenten)
        assert uitkomst.returncode == 0, uitkomst.stderr
        kop = f"task: [{cls.taak}]"
        regel = next(regel for regel in uitkomst.stderr.splitlines() if regel.startswith(kop))
        return regel[len(kop) :].strip()

    def test_zonder_naam_draait_er_niets_en_staat_de_aanroep_erbij(self) -> None:
        """Een opruimgereedschap zonder doel moet stoppen, en zeggen hoe het dan wel moet."""
        uitkomst = self._draai()

        assert uitkomst.returncode != 0, "de taak liep zonder projectnaam"
        assert f"task {self.taak} NAAM=" in uitkomst.stderr, (
            f"de melding zegt niet hoe de taak aangeroepen wordt: {uitkomst.stderr[-300:]}"
        )

    def test_meerdere_namen_komen_als_aparte_argumenten_aan(self) -> None:
        """De belofte uit de summary. Met ``{{.NAAM}}`` tussen quotes zou "a b" EEN project zijn."""
        commando = self._commando("NAAM=test-ej1 test-vim")

        assert commando.endswith("test-ej1 test-vim"), commando

    def test_de_taak_roept_de_module_aan_die_de_exitcode_bepaalt(self) -> None:
        """De lus heeft twee kanten, en dit is de andere.

        ``main`` geeft 1 bij een achterblijver, maar dat haalt de operator alleen als de
        taak ook deze module aanroept. De naam komt uit de module zelf, zodat een
        hernoeming hier rood wordt in plaats van stil langs een opgeschreven pad te gaan.
        """
        woorden = self._commando("NAAM=test-ej1").split()

        assert "-m" in woorden, woorden
        assert woorden[woorden.index("-m") + 1] == weesprojecten.__name__, woorden
