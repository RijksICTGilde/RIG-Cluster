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

    def json(self) -> object:
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
        except subprocess.TimeoutExpired as fout:
            raise AssertionError(f"{' '.join(argv)} liep langer dan {self.timeout}s") from fout

        return CliResultaat(
            exitcode=proces.returncode,
            stdout=proces.stdout,
            stderr=proces.stderr,
            argv=argv,
        )
