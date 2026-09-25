"""De zad-cli aanroepen tegen de sandbox, als tweede afnemer van dezelfde API.

De UI en de CLI praten met dezelfde endpoints maar maken andere aannames over de
antwoordvorm: de UI leest een HTML-pagina of een HTMX-fragment, de CLI leest de JSON
en de foutenvelop eronder. Een wijziging die de UI niet raakt maar de CLI wel viel
daarom tot nu toe nergens om, want geen enkele toets liep dat pad af.

Een pad dat niet gemeten is mag niet als groen wegschrijven: ontbreekt de CLI, dan slaan
de toetsen die hem gebruiken over in plaats van te slagen.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass

import pytest

#: `zad` is de entry point uit zad-cli's pyproject; `zadctl` staat in het plan van RC-227
#: en wordt meegenomen zodat een omgeving met die naam ook meet in plaats van over te slaan.
CLI_NAMEN = ("zad", "zadctl")

#: Overschrijft het zoeken, voor een CLI die niet op het PATH staat.
CLI_ENV = "ZAD_CLI"

#: Vlaggen waarvan de WAARDE niet in een melding hoort. ``registry add --password <token>`` is
#: de aanroep die dat hier doet, en elke bewering hieronder zet de argv in haar melding: die
#: komt in het pytest-verslag en in de uitvoer van de CI-stap terecht.
GEHEIME_VLAGGEN = ("--password", "--token", "--api-key", "--secret")

MASKER = "***"


def leesbare_argv(argv: list[str]) -> str:
    """De argv als tekst, met de waarde achter een geheime vlag gemaskeerd.

    Ook de ``--vlag=waarde``-vorm: dan staat de waarde in hetzelfde argument, en een
    maskering die alleen naar het volgende argument kijkt laat hem staan.
    """
    delen: list[str] = []
    volgende_is_geheim = False
    for deel in argv:
        if volgende_is_geheim:
            delen.append(MASKER)
            volgende_is_geheim = False
            continue
        met_gelijkteken = next((vlag for vlag in GEHEIME_VLAGGEN if deel.startswith(f"{vlag}=")), None)
        delen.append(f"{met_gelijkteken}={MASKER}" if met_gelijkteken else deel)
        volgende_is_geheim = deel in GEHEIME_VLAGGEN
    return " ".join(delen)


@dataclass(frozen=True)
class CliResultaat:
    exitcode: int
    stdout: str
    stderr: str
    argv: list[str]

    @property
    def uitvoer(self) -> str:
        """stdout en stderr samen, want de CLI kiest zelf waar een melding heen gaat."""
        return f"{self.stdout}\n{self.stderr}"

    @property
    def aanroep(self) -> str:
        """De aanroep zoals hij in een melding hoort: zonder de waarde van een geheime vlag."""
        return leesbare_argv(self.argv)

    def json(self) -> object:
        try:
            return json.loads(self.stdout)
        except json.JSONDecodeError as fout:
            raise AssertionError(
                f"{self.aanroep} gaf geen JSON terug (exit {self.exitcode}): {fout}\n"
                f"stdout: {self.stdout!r}\nstderr: {self.stderr!r}"
            ) from fout

    def assert_ok(self) -> CliResultaat:
        assert self.exitcode == 0, f"{self.aanroep} gaf exit {self.exitcode}\n{self.uitvoer}"
        return self

    def assert_faalt(self) -> CliResultaat:
        assert self.exitcode != 0, f"{self.aanroep} slaagde onverwacht\n{self.uitvoer}"
        return self


def cli_pad() -> str | None:
    uit_env = os.environ.get(CLI_ENV, "").strip()
    if uit_env:
        return uit_env if os.path.isabs(uit_env) else shutil.which(uit_env)
    for naam in CLI_NAMEN:
        gevonden = shutil.which(naam)
        if gevonden:
            return gevonden
    return None


#: De skipreden, ook bruikbaar als ``pytest.mark.skipif``-tekst.
GEEN_CLI = (
    f"zad-cli niet gevonden ({' of '.join(CLI_NAMEN)} op het PATH, of {CLI_ENV}). "
    "De CLI zit in een eigen repository en wordt hier niet meegeleverd."
)


def skip_zonder_cli() -> str:
    pad = cli_pad()
    if not pad:
        pytest.skip(GEEN_CLI)
    return pad


class ZadCli:
    """Aanroeper voor een vaste sandbox, met de instellingen via de omgeving en niet via
    vlaggen: dat is de weg die een gebruiker in een script ook neemt, en hij dekt daarmee
    ook de resolutie in de CLI zelf.
    """

    def __init__(
        self,
        pad: str,
        base_url: str,
        *,
        api_key: str = "",
        project: str = "",
        timeout: float = 300.0,
    ) -> None:
        self.pad = pad
        self.api_url = f"{base_url.rstrip('/')}/api"
        self.api_key = api_key
        self.project = project
        self.timeout = timeout

    def run(self, *args: str, verwacht_json: bool = False, extra_env: dict[str, str] | None = None) -> CliResultaat:
        argv = [self.pad, *args]
        if verwacht_json:
            argv += ["-o", "json"]
        env = {
            **os.environ,
            "ZAD_API_URL": self.api_url,
            "NO_COLOR": "1",
            # Anders wraps rich de uitvoer op de breedte van de terminal en breekt een
            # assertie op een melding af op een willekeurige plek.
            "COLUMNS": "200",
            "TERM": "dumb",
        }
        if self.api_key:
            env["ZAD_API_KEY"] = self.api_key
        if self.project:
            env["ZAD_PROJECT_ID"] = self.project
        env.update(extra_env or {})

        try:
            proces = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env=env,
                check=False,
            )
        except subprocess.TimeoutExpired:
            # `from None`: de tekst van ``TimeoutExpired`` draagt de HELE argv, en een
            # gekoppelde uitzondering wordt in de traceback gewoon afgedrukt.
            raise AssertionError(f"{leesbare_argv(argv)} liep langer dan {self.timeout}s") from None

        return CliResultaat(
            exitcode=proces.returncode,
            stdout=proces.stdout,
            stderr=proces.stderr,
            argv=argv,
        )
