"""De AGE-keyring: waar de scripts hun private sleutels vandaan halen.

Het uitgangspunt staat in `secret_edit.py` en `key_rotation.py`: het versleutelde bestand
is de AUTORITEIT -- het noemt zelf zijn recipients en daarmee voor wie er versleuteld wordt.
Deze module is alleen de portemonnee: welke PRIVATE helft is waar beschikbaar. Zij beslist
nooit voor wie wordt versleuteld; zij levert de sleutel die bij een gevraagde recipient hoort.

Bronnen, in vaste volgorde, elk mag meerdere sleutels bevatten:

1. ``SOPS_AGE_KEY`` (inhoud) en ``SOPS_AGE_KEY_FILE`` (pad) -- sops-native, ook de CI-route;
2. ``~/.config/sops/age/keys.txt`` -- de sops-standaardplek, repo-onafhankelijk;
3. ``<repo>/security/*.txt`` -- de fallback, zodat bestaande gewoontes niet breken.

Elke keuze wordt hardop benoemd (welke recipient, welke bron). Staat dezelfde sleutel in
twee bronnen, dan meldt ``load_keyring`` dat: wie later één kopie roteert en de andere
vergeet, wil dat nu weten en niet dan.

Voor metadata-loze waarden -- losse ``base64+age:``-bedragen en projectvelden, waar geen
recipient in de tekst staat -- geldt geen gok maar een regel: ``expected_recipient_for``
mapt het pad op een verwachte sleutel via een referentie-SOPS-bestand per context. Klopt die
verwachting niet, dan probeert ``probe`` welke sleutels wél openen, zegt welke dat is, en
STOPT. Zo kan nooit ongemerkt een sandboxsleutel tegen productiemateriaal worden gehouden.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from key_rotation import public_key_of  # type: ignore[reportMissingImports]


class KeyringFailed(RuntimeError):
    """De keyring leverde de gevraagde sleutel niet, of weerlegde een verwachting."""


@dataclass(frozen=True)
class KeyEntry:
    """Eén private AGE-sleutel, met zijn publieke helft en waar hij vandaan kwam."""

    private: str
    public: str
    source: str  # "SOPS_AGE_KEY", een pad naar een keys.txt, of een bestand in security/


_MARKER = "AGE-SECRET-KEY-"


def _entries_from_lines(lines: list[str], source: str) -> list[KeyEntry]:
    """Elke sleutelregel in de tekst, op de marker gezocht en nooit op regelnummer."""
    found: list[KeyEntry] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(_MARKER):
            found.append(KeyEntry(private=stripped, public=public_key_of(stripped), source=source))
    return found


def load_keyring(security_dir: Path | None = None) -> list[KeyEntry]:
    """Alle beschikbare sleutels in bronvolgorde, ontdubbeld op de publieke helft.

    A.d.h.v. `os.environ` op het moment van aanroep. ``security_dir`` is een parameter en
    geen vaste repo-plek, zodat de keyring ook buiten een checkout werkt; de eerste bron
    die een sleutel levert wint, en een duplicaat wordt gemeld in plaats van genegeerd.
    """
    staged: list[KeyEntry] = []
    env_content = os.environ.get("SOPS_AGE_KEY", "").strip()
    if env_content:
        staged.extend(_entries_from_lines(env_content.splitlines(), "SOPS_AGE_KEY"))
    env_file = os.environ.get("SOPS_AGE_KEY_FILE", "").strip()
    if env_file:
        path = Path(env_file).expanduser()
        if path.is_file():
            staged.extend(_entries_from_lines(path.read_text(encoding="utf-8").splitlines(), str(path)))
    home = Path.home() / ".config" / "sops" / "age" / "keys.txt"
    if home.is_file():
        staged.extend(_entries_from_lines(home.read_text(encoding="utf-8").splitlines(), str(home)))
    if security_dir is not None and Path(security_dir).is_dir():
        for path in sorted(Path(security_dir).glob("*.txt")):
            staged.extend(_entries_from_lines(path.read_text(encoding="utf-8").splitlines(), str(path)))

    seen: dict[str, KeyEntry] = {}
    for entry in staged:
        if entry.public in seen:
            print(f"(keyring: dezelfde sleutel staat ook in {seen[entry.public].source} en {entry.source})")
            continue
        seen[entry.public] = entry
    return list(seen.values())


def private_key_for(recipient: str, security_dir: Path | None = None) -> KeyEntry:
    """De private helft bij een recipient uit een SOPS-metadata, met bronvermelding."""
    keyring = load_keyring(security_dir)
    for entry in keyring:
        if entry.public == recipient:
            return entry
    where = ", ".join(sorted({entry.source for entry in keyring})) or "leeg (alle bronnen misten sleutels)"
    raise KeyringFailed(
        f"geen sleutel voor recipient {recipient[:24]}…; de keyring bevat {len(keyring)} sleutel(s) uit: {where}"
    )


# --- metadata-loze waarden: eerst de regel, dan pas proberen -------------------------


def _first_recipient(path: Path) -> str:
    import re

    text = path.read_text(encoding="utf-8")
    match = re.search(r"^\s*-?\s*recipient:\s*(age1[a-z0-9]+)\s*$", text, re.MULTILINE)
    if not match:
        raise KeyringFailed(f"referentiebestand {path} heeft geen recipient in zijn SOPS-metadata")
    return match.group(1)


def expected_recipient_for(path: Path, repo: Path) -> str:
    """De recipient die een metadata-loze waarde op díe plek hoort te hebben.

    De regel is pad-gedreven en bewust kort: 'sandboxed' in de route betekent de
    sandbox-recipient, al het andere van dit platform de productie-recipient. De recipient
    zelf komt niet uit een veronderstelling maar uit een referentiebestand dat voor die
    context op schijf staat -- een SOPS-bestand dat zijn eigen autoriteit al bij zich draagt.
    """
    anchors = {
        "sandbox": repo
        / "infrastructure/bootstrap/infrastructure/secrets/config/overlays/sandboxed-local/keycloak-admin-secret.yaml.sops.yaml",
        "platform": repo
        / "infrastructure/bootstrap/infrastructure/secrets/config/overlays/odcn/keycloak-admin-secret.yaml.sops.yaml",
    }
    context = "sandbox" if "sandboxed" in str(path) else "platform"
    anchor = anchors[context]
    if not anchor.is_file():
        raise KeyringFailed(f"kan de verwachting niet vastleggen: referentiebestand ontbreekt: {anchor}")
    return _first_recipient(anchor)


def probe(
    ciphertext_block: str,
    path: Path,
    repo: Path,
    decrypt,  # (block, private_key) -> str | None; de bestaande decrypt uit key_rotation
    security_dir: Path | None = None,
    *,
    expected_public: str | None = None,
) -> KeyEntry:
    """Welke sleutel opent deze metadata-loze waarde? Eerst de regel, dan meten, dan stoppen.

    Volgorde is de afspraak: de verwachte sleutel krijgt de eerste kans. ``expected_public``
    kan hem benoemen (tests, of een aanroeper die de verwachting al vast had); zonder die
    vlag komt hij uit ``expected_recipient_for`` (pad → context → referentiebestand).
    Werkt de verwachte niet, dan meet ``probe`` welke sleutel uit de keyring wél opent --
    en stopt mét die wetenschap, zodat bijvoorbeeld nooit stil met de sandboxsleutel verder
    wordt gegaan terwijl de plek productie zegt. Opent geen enkele sleutel, dan is de waarde
    onleesbaar met deze keyring en stopt het proces eveneens.
    """
    keyring = load_keyring(security_dir)
    expected_public = expected_public or expected_recipient_for(path, repo)
    expected = next((entry for entry in keyring if entry.public == expected_public), None)
    if expected is not None and decrypt(ciphertext_block, expected.private) is not None:
        return expected

    opened_by = [entry for entry in keyring if entry is not expected and decrypt(ciphertext_block, entry.private) is not None]
    if opened_by:
        names = ", ".join(f"{entry.source} (publiek {entry.public[:20]}…)" for entry in opened_by)
        raise KeyringFailed(
            f"{path}: de verwachte sleutel opent deze waarde niet; hij opent WEL met: {names}. "
            "Gestopt: controleer eerst waarom dit veld op een andere sleutel staat dan zijn plek voorschrijft."
        )
    raise KeyringFailed(f"{path}: geen enkele sleutel in de keyring opent deze waarde")
