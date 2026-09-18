"""Het echte programma achter een naam op PATH, ook als die naam een shim is.

Een shim van asdf of mise draait alleen met de PATH en werkmap waarin de versiemanager zijn
versie vindt. Tests die een kale PATH of een tijdelijke werkmap gebruiken, krijgen daarom het
pad van het programma zelf.
"""

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

VERSIEBEHEERDERS = ("asdf", "mise")


def _proef(pad: str) -> str | None:
    """Wat `pad --version` buiten de repo met een kale PATH teruggeeft als het faalt, anders None."""
    with tempfile.TemporaryDirectory() as leeg:
        try:
            proc = subprocess.run(
                [pad, "--version"],
                cwd=leeg,
                env={"PATH": "/usr/bin:/bin", "HOME": leeg},
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as fout:
            return str(fout)
    if proc.returncode == 0:
        return None
    return f"exit {proc.returncode}: {(proc.stderr or proc.stdout).strip()}"


def _via_versiebeheerder(naam: str) -> str | None:
    for beheerder in VERSIEBEHEERDERS:
        if shutil.which(beheerder) is None:
            continue
        try:
            proc = subprocess.run(
                [beheerder, "which", naam], cwd=Path(__file__).parent, capture_output=True, text=True, timeout=10
            )
        except OSError, subprocess.TimeoutExpired:
            continue
        echt = proc.stdout.strip()
        if proc.returncode == 0 and echt and _proef(echt) is None:
            return echt
    return None


def echt_programma(naam: str) -> str:
    """Absoluut pad naar `naam`, of een skip die zegt of hij ontbreekt of niet aanroepbaar is."""
    gevonden = shutil.which(naam)
    if gevonden is None:
        pytest.skip(f"{naam} niet gevonden op PATH")
    fout = _proef(gevonden)
    if fout is None:
        return gevonden
    echt = _via_versiebeheerder(naam)
    if echt is None:
        pytest.skip(f"{naam} gevonden op {gevonden}, maar niet aanroepbaar buiten de repo met een kale PATH ({fout})")
    return echt
