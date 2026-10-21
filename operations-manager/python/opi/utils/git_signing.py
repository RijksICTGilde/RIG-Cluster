"""De GPG-sleutel waarmee OPI zijn commits in de GitOps-repo's ondertekent."""

import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass

from opi.core.config import settings
from opi.utils.subprocess_env import child_env

logger = logging.getLogger(__name__)

#: Keyring-map in TMPDIR (vluchtig), niet in settings.TEMP_DIR (PVC op sandbox).
GNUPG_HOME_NAME = "opi-gnupg"


class GitSigningKeyError(RuntimeError):
    """De geconfigureerde signing key kon niet worden ingelezen."""


@dataclass(frozen=True)
class GitSigningIdentity:
    """De fingerprint waarmee git ondertekent, en de GNUPGHOME waarin die sleutel staat."""

    fingerprint: str
    gnupg_home: str

    @property
    def key_id(self) -> str:
        """De long key ID: de vorm die ArgoCD in ``signatureKeys`` eist, een fingerprint weigert het."""
        return self.fingerprint[-16:]


_identity: GitSigningIdentity | None = None


def get_git_signing_identity() -> GitSigningIdentity | None:
    """De ingelezen signing identity, of None zolang er geen sleutel is geconfigureerd."""
    return _identity


def load_git_signing_identity() -> GitSigningIdentity | None:
    """Lees de geconfigureerde sleutel in en onthoud hem voor de git-connector.

    Zonder geconfigureerd of bestaand bestand blijft OPI werken zonder te ondertekenen.
    Een bestand dat gpg niet kan importeren is een configuratiefout en stopt de opstart.
    """
    global _identity

    key_file = settings.GIT_SIGNING_KEY_FILE
    if not key_file or not os.path.isfile(key_file):
        logger.warning(
            "Geen GPG-sleutel voor commits (GIT_SIGNING_KEY_FILE=%r): OPI commit zonder handtekening", key_file
        )
        _identity = None
        return None

    gnupg_home = os.path.join(tempfile.gettempdir(), GNUPG_HOME_NAME)
    shutil.rmtree(gnupg_home, ignore_errors=True)
    os.makedirs(gnupg_home, mode=0o700)

    _import_key(key_file, gnupg_home)
    fingerprint = _secret_key_fingerprint(key_file, gnupg_home)

    _identity = GitSigningIdentity(fingerprint=fingerprint, gnupg_home=gnupg_home)
    logger.info("OPI ondertekent zijn commits met GPG-sleutel %s", fingerprint)
    return _identity


def _import_key(key_file: str, gnupg_home: str) -> None:
    result = _run_gpg(["--import", key_file], gnupg_home)
    if result.returncode != 0:
        raise GitSigningKeyError(f"Kon de GPG-sleutel uit {key_file} niet importeren: {result.stderr.strip()}")


def _secret_key_fingerprint(key_file: str, gnupg_home: str) -> str:
    """De fingerprint van de geïmporteerde privésleutel, uit de machineleesbare uitvoer van gpg."""
    result = _run_gpg(["--list-secret-keys", "--with-colons"], gnupg_home)
    if result.returncode != 0:
        raise GitSigningKeyError(f"Kon de sleutels in {gnupg_home} niet uitlezen: {result.stderr.strip()}")

    in_secret_key = False
    for line in result.stdout.splitlines():
        fields = line.split(":")
        if fields[0] == "sec":
            in_secret_key = True
        elif fields[0] == "fpr" and in_secret_key:
            return fields[9]

    raise GitSigningKeyError(f"{key_file} bevat geen privésleutel waarmee ondertekend kan worden")


def _run_gpg(args: list[str], gnupg_home: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["gpg", "--homedir", gnupg_home, "--batch", "--no-tty", *args],
            capture_output=True,
            text=True,
            check=False,
            env=child_env(),
        )
    except FileNotFoundError as fout:
        raise GitSigningKeyError("gpg staat niet in dit image, dus er kan niet ondertekend worden") from fout
