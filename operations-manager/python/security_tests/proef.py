"""De ene ingang: zet de sandbox op, draai alle proeven, schrijf het rapport, breek af.

Exitcode niet-nul zodra er een bevinding is die niet in de baseline staat, en ook zodra een
laag niets heeft kunnen meten: NIET GEMETEN leest nooit als groen. De codes staan in
``operations-manager/security-proof/README.md``.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from security_tests import sandboxlog, zap_scan
from security_tests.doel import OPILOG, RAPPORTMAP, huidig_doel
from security_tests.rapport import Crash, render, telling_uit_junit, uit_junit
from security_tests.sandbox import proefsandbox

#: Waar het junit-rapport van de fuzzers landt, en het rapport voor een lezer.
JUNIT = RAPPORTMAP / "fuzzers.xml"
RAPPORT = RAPPORTMAP / "aanvalsproef.md"

#: De exitcodes van de gate. Een laag die niets meette krijgt zijn eigen code, zodat NIET
#: GEMETEN in CI niet als groen en niet als een bevinding leest.
GROEN = 0
BEVINDING = 1
NIET_GEMETEN = 3

PYTHONMAP = Path(__file__).resolve().parent


def draai_fuzzers() -> int:
    """Draai de pytest-proeven tegen de draaiende sandbox."""
    RAPPORTMAP.mkdir(parents=True, exist_ok=True)
    uitkomst = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(PYTHONMAP),
            "-q",
            "--junitxml",
            str(JUNIT),
            "-p",
            "no:cacheprovider",
        ],
        cwd=PYTHONMAP.parent,
        check=False,
    )
    return uitkomst.returncode


def main() -> int:
    with proefsandbox():
        doel = huidig_doel()
        print(f"proefsandbox staat op {doel.url}", flush=True)
        fuzzercode = draai_fuzzers()
        zap = zap_scan.draai(doel)

    logboek = sandboxlog.lees(OPILOG)
    bevindingen = uit_junit(JUNIT, sandboxlog.vensters(logboek))
    telling = telling_uit_junit(JUNIT)
    asgi = sandboxlog.asgi_fouten(logboek)
    onverklaard = [fout for fout in asgi if not fout.verwacht]
    # Een pytest die niet op een uitslag eindigde is zelf omgevallen; de laatste logregels
    # van OPI zeggen waar het proces op strandde.
    crash = Crash(fuzzercode, sandboxlog.laatste_regels(OPILOG)) if fuzzercode not in (0, 1) else None
    RAPPORT.write_text(render(bevindingen, zap, telling, asgi, crash))
    print(f"rapport: {RAPPORT}")
    for bevinding in bevindingen:
        print(f"BEVINDING {bevinding.proef}::{bevinding.naam}")
    for fout in onverklaard:
        print(f"BEVINDING asgi: {fout.route} ({', '.join(fout.oorzaken) or 'geen traceback'}) in {fout.proef}")
    for melding in zap.meldingen:
        print(f"BEVINDING zap: {melding}")
    # De bevindingen alleen uit het junit-rapport lezen is niet genoeg: een run waarin alles
    # overslaat of waarin pytest zelf omvalt levert geen failures op en zou groen eindigen.
    niet_gemeten = []
    if not zap.gemeten:
        niet_gemeten.append(f"de ZAP-basislaag ({zap.reden})")
    if telling.gemeten == 0:
        niet_gemeten.append(f"geen enkele gerichte proef heeft iets beproefd ({telling.overgeslagen} overgeslagen)")
    if fuzzercode not in (0, 1):
        niet_gemeten.append(f"pytest eindigde op {fuzzercode}, niet op een uitslag")
    for melding in niet_gemeten:
        print(f"NIET GEMETEN: {melding}")

    # Een bevinding gaat voor: die is het sterkste signaal, ook als er daarnaast een laag
    # niets meette. Beide staan in de uitvoer en in het rapport.
    if bevindingen or zap.meldingen or onverklaard:
        return BEVINDING
    return NIET_GEMETEN if niet_gemeten else GROEN


if __name__ == "__main__":
    raise SystemExit(main())
