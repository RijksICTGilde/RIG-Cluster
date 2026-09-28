"""Toetsen voor scripts/age_keyring.py: bronnen-volgorde, recipient-match, en de
niet-gokken-regel voor metadata-loze waarden.

De waarheid van deze module zit in dingen die "hij draait op mijn machine" niet toetst:

* de bronvolgorde (env, env-file, keys.txt in home, security/ als fallback) en het
  gemelde duplicaat;
* een recipient zonder bijpassende private sleutel is een harde stop die zegt waar
  gezocht is;
* de verwachting voor een metadata-loze waarde komt uit een pad-regel met een
  referentiebestand, en als die verwachting faalt stopt het proces MET de melding welke
  sleutel wél opende -- nooit stil doorgaan met de verkeerde.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import age_keyring as keyring  # type: ignore[reportMissingImports]  # noqa: E402
from key_rotation import public_key_of, read_key  # type: ignore[reportMissingImports]  # noqa: E402

needs_age = pytest.mark.skipif(shutil.which("age-keygen") is None, reason="vraagt age-keygen")


def _make_key(target: Path) -> Path:
    """Een echte AGE-sleutel op een tijdelijke plek."""
    subprocess.run(["age-keygen", "-o", str(target)], capture_output=True, check=True)
    return target


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Geen invloed van de keyring van deze machine: env leeg en een lege home."""
    monkeypatch.delenv("SOPS_AGE_KEY", raising=False)
    monkeypatch.delenv("SOPS_AGE_KEY_FILE", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)


@needs_age
def test_de_bronvolgorde_is_env_dan_home_dan_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_env: None
) -> None:
    home_keys = tmp_path / "home/.config/sops/age"
    home_keys.mkdir(parents=True)
    _make_key(home_keys / "keys.txt")
    repo = tmp_path / "repo" / "security"
    repo.mkdir(parents=True)
    _make_key(repo / "key.txt")
    env_key = tmp_path / "env-key.txt"
    _make_key(env_key)
    monkeypatch.setenv("SOPS_AGE_KEY_FILE", str(env_key))

    sources = [entry.source for entry in keyring.load_keyring(security_dir=repo)]
    assert sources[0] == str(env_key), "env-file hoort als eerste"
    assert sources[1].endswith("keys.txt"), "daarna de home-plek"
    assert sources[2].endswith("security/key.txt"), "de repo is de fallback"


@needs_age
def test_dezelfde_sleutel_in_twee_bronnen_wordt_gemeld(
    tmp_path: Path, clean_env: None, capsys: pytest.CaptureFixture
) -> None:
    """Wie later één kopie roteert en de andere vergeet, wil dat nu weten."""
    key = _make_key(tmp_path / "shared.txt")
    security = tmp_path / "security"
    security.mkdir()
    import shutil as _shutil

    _shutil.copy(key, security / "key.txt")

    entries = keyring.load_keyring(security_dir=security)
    entries = entries  # bron: security
    # dezelfde sleutel ook via SOPS_AGE_KEY (inhoud)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("SOPS_AGE_KEY", read_key(key))
    try:
        entries = keyring.load_keyring(security_dir=security)
        assert len(entries) == 1, "dezelfde publieke helft mag maar één keer in de keyring"
        assert "dezelfde sleutel staat ook in" in capsys.readouterr().out
    finally:
        monkeypatch.undo()


@needs_age
def test_de_entry_vindt_zijn_bron_en_alle_sleutels_in_een_bestand(tmp_path: Path, clean_env: None) -> None:
    """Een keys.txt met TWEE sleutels levert beide, en de bron noemt het bestand."""
    first = _make_key(tmp_path / "a.txt")
    second = _make_key(tmp_path / "b.txt")
    security = tmp_path / "security"
    security.mkdir()
    (security / "combined.txt").write_text(read_key(first) + "\n" + read_key(second) + "\n", encoding="utf-8")

    entry = keyring.private_key_for(public_key_of(read_key(second)), security_dir=security)
    assert entry.private == read_key(second)
    assert entry.source.endswith("combined.txt")


@needs_age
def test_probe_stopt_als_de_verwachte_sleutel_niet_opent(tmp_path: Path, clean_env: None) -> None:
    """De verwachte sleutel faalt, een andere opent wel: melden welke, en stoppen.

    Zo kan nooit ongemerkt een sandbox-sleutel tegen productiemateriaal worden gehouden.
    """
    platform = _make_key(tmp_path / "platform.txt")
    sandbox = _make_key(tmp_path / "sandbox.txt")
    security = tmp_path / "security"
    security.mkdir()
    import shutil as _shutil

    _shutil.copy(platform, security / "key.txt")
    _shutil.copy(sandbox, security / "sandbox-key.txt")

    def fake_decrypt(_block: str, private: str) -> str | None:
        return "geheim" if private == read_key(sandbox) else None

    with pytest.raises(keyring.KeyringFailed, match=r"sandbox-key\.txt"):
        keyring.probe(
            "age-block",
            tmp_path / "waarde.txt",
            tmp_path,
            fake_decrypt,
            security_dir=security,
            expected_public=public_key_of(read_key(platform)),
        )


@needs_age
def test_probe_geeft_de_verwachte_sleutel_als_die_gewoon_werkt(tmp_path: Path, clean_env: None) -> None:
    key = _make_key(tmp_path / "platform.txt")
    security = tmp_path / "security"
    security.mkdir()
    import shutil as _shutil

    _shutil.copy(key, security / "key.txt")

    entry = keyring.probe(
        "age-block",
        tmp_path / "waarde.txt",
        tmp_path,
        lambda _block, private: "geheim" if private == read_key(key) else None,
        security_dir=security,
        expected_public=public_key_of(read_key(key)),
    )
    assert entry.private == read_key(key)


@needs_age
def test_probe_stopt_als_niets_opent(tmp_path: Path, clean_env: None) -> None:
    key = _make_key(tmp_path / "platform.txt")
    with pytest.raises(keyring.KeyringFailed, match="geen enkele sleutel"):
        keyring.probe(
            "age-block",
            tmp_path / "waarde.txt",
            tmp_path,
            lambda _block, _private: None,
            security_dir=None,
            expected_public=public_key_of(read_key(key)),
        )


def test_de_padregel_onderscheidt_sandbox_van_platform(tmp_path: Path) -> None:
    """De regel zelf: 'sandboxed' in de route kiest het sandbox-referentiebestand."""
    repo = tmp_path
    anchor = (
        repo
        / "infrastructure/bootstrap/infrastructure/secrets/config/overlays/sandboxed-local/keycloak-admin-secret.yaml.sops.yaml"
    )
    (tmp_path / "infrastructure/bootstrap/infrastructure/secrets/config/overlays/odcn").mkdir(parents=True)
    anchor.parent.mkdir(parents=True)
    anchor.write_text("sops:\n    age:\n        - recipient: age1sandboxvoorbeeld\n", encoding="utf-8")
    with pytest.raises(keyring.KeyringFailed, match="referentiebestand ontbreekt"):
        keyring.expected_recipient_for(repo / "config.py", repo)
    assert keyring.expected_recipient_for(anchor, repo) == "age1sandboxvoorbeeld"
