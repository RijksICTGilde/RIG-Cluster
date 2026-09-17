"""Tests voor tests/programma.py, met een nagebootste versiemanager en shim."""

from typing import TYPE_CHECKING

import pytest
from tests.programma import echt_programma

if TYPE_CHECKING:
    from pathlib import Path

# De twee gezichten uit RC-204: de shim vindt zijn motor niet op een kale PATH, of vindt
# buiten de repo geen .tool-versions.
SHIM_ZONDER_MOTOR = 'exec asdf exec proefprog "$@"\n'
SHIM_ZONDER_VERSIE = """d=$PWD
while [ "$d" != / ]; do
  [ -f "$d/.tool-versions" ] && exec {echt} "$@"
  d=$(dirname "$d")
done
echo "No version is set" >&2
exit 126
"""


def _script(pad: Path, body: str) -> Path:
    pad.parent.mkdir(parents=True, exist_ok=True)
    pad.write_text(f"#!/bin/sh\n{body}")
    pad.chmod(0o755)
    return pad


def _vind(naam: str) -> str:
    """Een skip telt hier als rood: deze tests verwachten een pad."""
    try:
        return echt_programma(naam)
    except pytest.skip.Exception as skip:
        pytest.fail(f"overgeslagen: {skip}")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Werkmap met .tool-versions, en een PATH met shims voor de versiemanager."""
    (tmp_path / ".tool-versions").write_text("proefprog 1.0\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", f"{tmp_path / 'shims'}:{tmp_path / 'beheerder'}:/usr/bin:/bin")
    return tmp_path


@pytest.fixture
def echt(repo: Path) -> Path:
    return _script(repo / "installs" / "proefprog", "echo 'proefprog 1.0'\n")


def _beheerder(repo: Path, antwoord: Path) -> None:
    _script(
        repo / "beheerder" / "asdf", f'[ "$1" = which ] && echo "{antwoord}" || {{ shift 2; exec {antwoord} "$@"; }}\n'
    )


def test_a_directly_installed_program_is_returned_as_found(repo: Path) -> None:
    prog = _script(repo / "shims" / "proefprog", "echo 'proefprog 1.0'\n")

    assert _vind("proefprog") == str(prog)


@pytest.mark.parametrize("shim", [SHIM_ZONDER_MOTOR, SHIM_ZONDER_VERSIE], ids=["kale-path", "buiten-repo"])
def test_a_shim_gives_way_to_the_program_of_the_version_manager(repo: Path, echt: Path, shim: str) -> None:
    _script(repo / "shims" / "proefprog", shim.format(echt=echt))
    _beheerder(repo, echt)

    assert _vind("proefprog") == str(echt)


def test_a_missing_program_skips_as_not_found(repo: Path) -> None:
    with pytest.raises(pytest.skip.Exception, match=r"^proefprog niet gevonden op PATH$"):
        echt_programma("proefprog")


def test_a_shim_without_version_manager_skips_with_path_and_reason(repo: Path, echt: Path) -> None:
    shim = _script(repo / "shims" / "proefprog", SHIM_ZONDER_MOTOR)

    with pytest.raises(pytest.skip.Exception) as skip:
        echt_programma("proefprog")

    assert f"gevonden op {shim}" in str(skip.value)
    assert "exit 127" in str(skip.value)


def test_a_version_manager_path_that_does_not_run_is_not_returned(repo: Path) -> None:
    _script(repo / "shims" / "proefprog", SHIM_ZONDER_MOTOR)
    _beheerder(repo, _script(repo / "installs" / "proefprog", "exit 3\n"))

    with pytest.raises(pytest.skip.Exception, match="niet aanroepbaar"):
        echt_programma("proefprog")
