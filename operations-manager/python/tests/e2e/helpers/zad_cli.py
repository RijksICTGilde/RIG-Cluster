"""De zad-cli aanroepen tegen de sandbox, als tweede afnemer van dezelfde API.

De UI en de CLI praten met dezelfde endpoints maar maken andere aannames over de
antwoordvorm: de UI leest een HTML-pagina of een HTMX-fragment, de CLI leest de JSON
en de foutenvelop eronder. Een wijziging die de UI niet raakt maar de CLI wel viel
daarom tot nu toe nergens om, want geen enkele toets liep dat pad af.

De CLI woont in een eigen repository (`zad-cli`, commando `zad`) en wordt niet door
deze repo meegeleverd. Staat hij niet op het PATH, dan slaan de toetsen die hem
gebruiken over, net zoals de sandboxtoetsen overslaan zonder ``E2E_BASE_URL``. Dat is
een keuze: een CLI die ontbreekt is geen falende toets, en een pad dat niet gemeten is
moet niet als groen wegschrijven.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass

import pytest

#: Namen waaronder de CLI geinstalleerd kan zijn. `zad` is de entry point uit
#: zad-cli's pyproject; `zadctl` staat in het plan van RC-227 en wordt meegenomen
#: zodat een omgeving met die naam ook meet in plaats van over te slaan.
CLI_NAMEN = ("zad", "zadctl")

#: Overschrijft het zoeken, voor een CLI die niet op het PATH staat.
CLI_ENV = "ZAD_CLI"


@dataclass(frozen=True)
class CliResultaat:
    """Wat een CLI-aanroep opleverde: de drie dingen waar een toets iets over zegt."""

    exitcode: int
    stdout: str
    stderr: str
    argv: list[str]

    @property
    def uitvoer(self) -> str:
        """stdout en stderr samen, want de CLI kiest zelf waar een melding heen gaat."""
        return f"{self.stdout}\n{self.stderr}"

    def json(self) -> object:
        """De stdout als JSON, met de hele aanroep in de fout als dat niet lukt."""
        try:
            return json.loads(self.stdout)
        except json.JSONDecodeError as fout:
            raise AssertionError(
                f"{' '.join(self.argv)} gaf geen JSON terug (exit {self.exitcode}): {fout}\n"
                f"stdout: {self.stdout!r}\nstderr: {self.stderr!r}"
            ) from fout

    def assert_ok(self) -> CliResultaat:
        assert self.exitcode == 0, f"{' '.join(self.argv)} gaf exit {self.exitcode}\n{self.uitvoer}"
        return self

    def assert_faalt(self) -> CliResultaat:
        assert self.exitcode != 0, f"{' '.join(self.argv)} slaagde onverwacht\n{self.uitvoer}"
        return self


def cli_pad() -> str | None:
    """Het pad naar de CLI, of None als hij er niet is."""
    uit_env = os.environ.get(CLI_ENV, "").strip()
    if uit_env:
        return uit_env if os.path.isabs(uit_env) else shutil.which(uit_env)
    for naam in CLI_NAMEN:
        gevonden = shutil.which(naam)
        if gevonden:
            return gevonden
    return None


def skip_zonder_cli() -> str:
    """Het CLI-pad, of een skip met de reden erbij."""
    pad = cli_pad()
    if not pad:
        pytest.skip(
            f"zad-cli niet gevonden ({' of '.join(CLI_NAMEN)} op het PATH, of {CLI_ENV}). "
            "De CLI zit in een eigen repository en wordt hier niet meegeleverd."
        )
    return pad


class ZadCli:
    """Aanroeper voor een vaste sandbox en, optioneel, een vast project.

    De instellingen gaan via de omgeving en niet via vlaggen: dat is de weg die een
    gebruiker in een script ook neemt (``ZAD_API_URL``, ``ZAD_API_KEY``,
    ``ZAD_PROJECT_ID``), en hij dekt daarmee ook de resolutie in de CLI zelf.
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

    def voor_project(self, project: str, api_key: str) -> ZadCli:
        """Dezelfde CLI, gericht op een ander project."""
        return ZadCli(
            self.pad, self.api_url.removesuffix("/api"), api_key=api_key, project=project, timeout=self.timeout
        )

    def run(self, *args: str, verwacht_json: bool = False, extra_env: dict[str, str] | None = None) -> CliResultaat:
        """Roep de CLI aan en geef exitcode, stdout en stderr terug zonder te oordelen.

        Oordelen doet de toets: een niet-nul exitcode is voor de helft van de toetsen
        hieronder juist wat gemeten wordt.
        """
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
        except subprocess.TimeoutExpired as fout:
            raise AssertionError(f"{' '.join(argv)} liep langer dan {self.timeout}s") from fout

        return CliResultaat(
            exitcode=proces.returncode,
            stdout=proces.stdout,
            stderr=proces.stderr,
            argv=argv,
        )
