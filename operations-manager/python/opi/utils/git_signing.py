"""The GPG key OPI signs its commits in the GitOps repos with."""

import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass

from opi.core.config import settings
from opi.core.subprocess_env import child_env

logger = logging.getLogger(__name__)

#: Keyring directory in TMPDIR (volatile), not in settings.TEMP_DIR (a PVC on the sandbox).
GNUPG_HOME_NAME = "opi-gnupg"


class GitSigningKeyError(RuntimeError):
    """The configured signing key could not be read."""


@dataclass(frozen=True)
class GitSigningIdentity:
    """The fingerprint git signs with, and the GNUPGHOME holding that key."""

    fingerprint: str
    gnupg_home: str

    @property
    def key_id(self) -> str:
        """The long key ID: the shape ArgoCD demands in ``signatureKeys``, it refuses a fingerprint."""
        return self.fingerprint[-16:]


_identity: GitSigningIdentity | None = None


def get_git_signing_identity() -> GitSigningIdentity | None:
    """The loaded signing identity, or None as long as no key is configured."""
    return _identity


def load_git_signing_identity() -> GitSigningIdentity | None:
    """Read the configured key and remember it for the git connector.

    Without a configured or existing file OPI keeps working without signing. A file gpg
    cannot import is a configuration error and stops the startup.
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
        return subprocess.run(  # noqa: S603  # no shell; the variable parts are separate argv elements
            ["gpg", "--homedir", gnupg_home, "--batch", "--no-tty", *args],  # noqa: S607  # a tool from the image's PATH, not a fixed path
            capture_output=True,
            text=True,
            check=False,
            env=child_env(),
        )
    except FileNotFoundError as e:
        raise GitSigningKeyError("gpg staat niet in dit image, dus er kan niet ondertekend worden") from e
