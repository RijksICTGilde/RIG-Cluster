"""The ArgoCD repository secrets in zad-argo-user-applications: the PAT round's third place.

The key round already walks these files. They are SOPS files on the platform recipient, so
``rotate-sops-key.py --argo-applications`` rotates them along with everything else. The PAT
round asks a different question about the same files: their ``password`` is not ciphertext on
the platform key but a PLAINTEXT value inside it, put there by
``argo_manager.prepare_repository_variables`` out of ``repositories[].password`` of the project
file. Rotating the key leaves that value exactly as it was, so after a PAT round every one of
these secrets still hands ArgoCD the revoked token -- and the final check says CLEAN, because
the old KEY really does open nothing any more.

**Derived, not maintained.** OPI regenerates these secrets out of the project file whenever it
processes that project. This round therefore writes what OPI itself would write, and nothing
else:

* the value comes from the project file, decrypted -- never straight from the new PAT.
  Whatever the project round decided about a repository is what lands here, including the
  decision to leave it alone: a password that is absent, ``plain:`` or in any other form is not
  on the platform key, the project round skips it, and then there is nothing to derive and the
  secret is left as it is. That is not a corner case -- it is the shape of the whole sandbox,
  where every project carries ``plain:`` credentials for Forgejo;
* only ``stringData.password`` changes. The document is round-tripped through the same YAML
  writer the rest of the tool uses, so annotations, labels, quoting and key order come out as
  they went in;
* it is written back through ``encrypt_to_sops_files``, the exact function ``argo_manager``
  encrypts with, onto the recipient the file already carries. This round does not move keys;
  that is the key round's job, and doing both here would hide one inside the other.

**The SSH form is not ours.** ``argo_manager`` picks ``argo-repository.yaml.jinja`` for a
repository that is not HTTPS, and that template writes ``sshPrivateKey`` and no ``password`` at
all -- the comment next to the call says "git SSH key/HTTPS-wachtwoord" for that reason. A
secret without a ``password`` field is left alone, and that is measured on the decrypted
document rather than assumed from the file name.

**The coupling, and why only one direction stops the round.** A secret is named after the
project and the repository it was derived from, so the two sides can be matched by name instead
of by guesswork. The two directions are not symmetric, and that is measured on a real clone of
zad-argo-user-applications rather than reasoned about:

* a secret whose name matches no project repository is a finding that STOPS the round. Nothing
  maintains that password, this round cannot derive a value for it, and it would sit there with
  the revoked token after the old one is withdrawn;
* a project repository with no secret is NOT a stop. On the real clone 5 of the 11 projects have
  no directory of their own, 4 of them with a deployment on the very cluster that clone holds,
  because OPI writes these files when it PROCESSES a project and those had not been processed
  since. A stop there would refuse the round on a normal state -- and the one repair for it is
  to reprocess every project, which is exactly what a key rotation must not set off. They are
  named, counted and reported, because a silent skip is how the fifth place fell away earlier.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from key_rotation import (  # type: ignore[reportMissingImports]
    PROJECT_FIELD_REPO_PASSWORD,
    ConversionFailed,
    FinalCheck,
    check_token,
    decrypt_field,
    form_of,
    load_yaml_from_path,
    project_files,
    sops_files,
    sops_plaintext,
    sops_recipients,
)
from opi.utils.naming import (
    generate_argocd_repository_secret_name,
    generate_infrastructure_application_name,
)
from opi.utils.sops import SOPSEncryptionError, encrypt_to_sops_files
from opi.utils.yaml_util import dump_yaml_to_string, load_yaml_from_string

# A failed decryption is the EXPECTED outcome here: a clone whose key round has run opens with
# the new key and one where it has not with the old one, and telling those apart is done by
# trying. Both modules log such an attempt at ERROR, which would bury this round's verdict.
for _noisy in ("opi.utils.age", "opi.utils.sops"):
    logging.getLogger(_noisy).setLevel(logging.CRITICAL)

#: The label ArgoCD itself selects repository secrets on, and the one both templates carry.
#: Taken from the manifests ``argo_manager`` renders, so a secret of another kind in the same
#: clone (an AppProject, an Application) is not mistaken for one of these.
ARGO_SECRET_TYPE_LABEL = "argocd.argoproj.io/secret-type"  # noqa: S105 - a label name, not a secret
ARGO_SECRET_TYPE_REPOSITORY = "repository"  # noqa: S105 - a label value, not a secret

#: The field the HTTPS template writes and the SSH template does not. An SSH key is not a PAT,
#: and that whole distinction hangs on this one name, so it is spelled once.
PASSWORD_FIELD = "password"  # noqa: S105 - a field name, not a password
SSH_KEY_FIELD = "sshPrivateKey"

#: ``.to-sops.yaml`` in, ``.sops.yaml`` out: the pair ``encrypt_to_sops_files`` works on.
SOPS_SUFFIX = ".sops.yaml"
TO_SOPS_SUFFIX = ".to-sops.yaml"


@dataclass
class RepositorySecret:
    """One ArgoCD repository secret in the clone, as it stands after decryption."""

    path: Path
    recipients: list[str]
    private_key: str
    document: dict[str, Any]

    @property
    def string_data(self) -> dict[str, Any]:
        value = self.document.get("stringData")
        return value if isinstance(value, dict) else {}

    @property
    def name(self) -> str:
        return str(self.string_data.get("name") or self.document.get("metadata", {}).get("name") or "")

    @property
    def password(self) -> str | None:
        """The HTTPS password, or None when this secret is the SSH form.

        None and "" are different answers here: the template writes ``password: ""`` for a
        repository without credentials, and that one IS ours to keep in step.
        """
        value = self.string_data.get(PASSWORD_FIELD)
        return None if value is None else str(value)

    @property
    def is_ssh_form(self) -> bool:
        """Whether a PAT round has anything to do here, read off the decrypted document.

        Not off the file name: ``argo-repository-https.yaml`` and ``argo-repository.yaml`` do
        say which template wrote the file, but that is a name and this is a credential.
        """
        return self.password is None

    @property
    def credential_form(self) -> str:
        """What this secret authenticates with, in the words the report uses."""
        if self.password is not None:
            return "https password"
        return "ssh key" if SSH_KEY_FIELD in self.string_data else "no credentials"


@dataclass
class ProjectRepository:
    """One ``repositories[]`` entry of one project file, with what the secret is named after."""

    path: Path
    project: str
    repository: str
    field_name: str
    #: None when this repository carries no password on the PLATFORM key -- absent, ``plain:``
    #: or any other form. The project round does not convert those either, so there is nothing
    #: to derive a value from and the secret is reported rather than written or refused.
    ciphertext: str | None

    @property
    def secret_names(self) -> tuple[str, ...]:
        """The names OPI can give this repository's secret: the deployment one and the
        infrastructure one.

        Both come out of ``opi.utils.naming``. Writing the format out here ("{project}-{repo}")
        would be a second spelling of a rule that sanitises for Kubernetes, and the two would
        drift the first time that sanitiser changes.
        """
        return (
            generate_argocd_repository_secret_name(self.project, self.repository),
            generate_argocd_repository_secret_name(
                generate_infrastructure_application_name(self.project), self.repository
            ),
        )


@dataclass
class Pairing:
    """Which secret belongs to which project repository, and what is left over on both sides."""

    pairs: list[tuple[RepositorySecret, ProjectRepository]] = field(default_factory=list)
    ssh_form: list[RepositorySecret] = field(default_factory=list)
    without_platform_password: list[tuple[RepositorySecret, ProjectRepository]] = field(default_factory=list)
    secrets_without_project: list[RepositorySecret] = field(default_factory=list)
    repositories_without_secret: list[ProjectRepository] = field(default_factory=list)
    unreadable: list[Path] = field(default_factory=list)


def is_repository_secret(document: object) -> bool:
    """A decrypted document that is one of the two repository-secret manifests.

    Everything in these files is encrypted, ``kind`` and the labels included, so this can only
    be answered after decryption -- which is also why the file name is not the test.
    """
    if not isinstance(document, dict) or document.get("kind") != "Secret":
        return False
    labels = (document.get("metadata") or {}).get("labels") or {}
    return isinstance(labels, dict) and labels.get(ARGO_SECRET_TYPE_LABEL) == ARGO_SECRET_TYPE_REPOSITORY


def read_repository_secrets(clone: Path, *private_keys: str) -> tuple[list[RepositorySecret], list[Path]]:
    """Every ArgoCD repository secret in the clone, opened with the first key that fits.

    Both keys go in for the same reason the project round takes both: a clone whose key round
    has run reads with the new one, a clone where it has not with the old one, and the round
    must not mistake the second for a broken file.

    Second return value: the SOPS files that opened with neither key. Those are a finding, not
    an empty result.
    """
    found: list[RepositorySecret] = []
    unreadable: list[Path] = []
    for path in sops_files(clone):
        recipients = sops_recipients(path)
        plaintext = None
        opened_with = ""
        for key in private_keys:
            plaintext = sops_plaintext(path, key)
            if plaintext is not None:
                opened_with = key
                break
        if plaintext is None:
            unreadable.append(path)
            continue
        document = load_yaml_from_string(plaintext)
        if not is_repository_secret(document):
            continue
        found.append(
            RepositorySecret(
                path=path,
                recipients=recipients,
                private_key=opened_with,
                document=document,  # type: ignore[arg-type]  -- is_repository_secret proved the type
            )
        )
    return found, unreadable


def project_repositories(directory: Path) -> list[ProjectRepository]:
    """Every ``repositories[]`` entry across the project clone, password or not.

    Not only the ones the round can convert. A repository whose password is absent or in another
    form still HAS a secret in the argo clone, and leaving it out of this list would make that
    secret look like one no project file accounts for -- a stop, on something entirely normal.
    The sandbox is exactly that shape: every project there carries ``plain:`` credentials for
    Forgejo.
    """
    found: list[ProjectRepository] = []
    for path in project_files(directory):
        data = load_yaml_from_path(str(path))
        if not isinstance(data, dict):
            continue
        for index, entry in enumerate(data.get("repositories") or []):
            if not isinstance(entry, dict):
                continue
            password = entry.get("password")
            on_platform_key = isinstance(password, str) and form_of(password) is not None
            found.append(
                ProjectRepository(
                    path=path,
                    project=str(data.get("name") or path.stem),
                    repository=str(entry.get("name") or ""),
                    field_name=PROJECT_FIELD_REPO_PASSWORD.format(index=index),
                    ciphertext=password if on_platform_key else None,
                )
            )
    return found


def pair_up(secrets: list[RepositorySecret], repositories: list[ProjectRepository]) -> Pairing:
    """Match each secret to the project repository it was derived from, by name.

    A repository can own two secrets (the deployment one and the infrastructure one), so the
    index maps name -> repository and every secret is looked up in it, rather than the other
    way around.
    """
    pairing = Pairing()
    by_name: dict[str, ProjectRepository] = {}
    for repository in repositories:
        for name in repository.secret_names:
            by_name[name] = repository

    matched: set[int] = set()
    for secret in secrets:
        if secret.is_ssh_form:
            pairing.ssh_form.append(secret)
            continue
        repository = by_name.get(secret.name)
        if repository is None:
            pairing.secrets_without_project.append(secret)
            continue
        matched.add(id(repository))
        if repository.ciphertext is None:
            pairing.without_platform_password.append((secret, repository))
            continue
        pairing.pairs.append((secret, repository))

    pairing.repositories_without_secret = [
        repository for repository in repositories if repository.ciphertext is not None and id(repository) not in matched
    ]
    return pairing


async def plan_argo_round(
    clone: Path, projects: Path, *private_keys: str, expected: str | None = None
) -> tuple[Pairing, list[tuple[RepositorySecret, ProjectRepository, str]], list[str]]:
    """What the round would do: the pairing, the secrets whose password differs, and the failures.

    ``expected`` is what the DRY RUN needs and the real run must not have. The PAT round writes
    the project files first and then these secrets, so a preview that reads the project files as
    they stand right now compares against the OLD token and reports "nothing to do" for a round
    that is about to convert every one of them. With ``expected`` the preview compares against
    the value the project round is about to write instead. The run itself leaves it off and
    derives from the file, so a project that the round skipped does not drag its secret along.

    Third value: the fields that opened with neither key. Those cannot produce a value to write,
    and writing the rest anyway would leave the clone half converted.
    """
    secrets, unreadable = read_repository_secrets(clone, *private_keys)
    pairing = pair_up(secrets, project_repositories(projects))
    pairing.unreadable = unreadable

    todo: list[tuple[RepositorySecret, ProjectRepository, str]] = []
    closed: list[str] = []
    for secret, repository in pairing.pairs:
        plaintext = expected
        if plaintext is None:
            for key in private_keys:
                plaintext = await decrypt_field(repository.ciphertext or "", key)
                if plaintext is not None:
                    break
        if plaintext is None:
            closed.append(f"{repository.path}#{repository.field_name}")
            continue
        if secret.password != plaintext:
            todo.append((secret, repository, plaintext))
    return pairing, todo, closed


def write_repository_secret(secret: RepositorySecret, password: str) -> None:
    """Put one password into the secret and encrypt it back the way OPI does.

    The plaintext goes to the ``.to-sops.yaml`` next to the file and ``encrypt_to_sops_files``
    turns it into the ``.sops.yaml`` again -- the same function, in the same directory, with the
    same pairing of names ``argo_manager`` uses, so there is nothing here that OPI's next write
    could disagree with. Its own private key comes along, which is what makes an unchanged
    secret keep its existing ciphertext instead of being rewritten with a fresh nonce.

    The recipient is the one the file already carries, and there has to be exactly one: picking
    the first of several would silently drop the others. Moving a file to another key belongs to
    the key round; a PAT round that quietly did that as well would hide one operation inside the
    other.
    """
    if not secret.path.name.endswith(SOPS_SUFFIX):
        raise ConversionFailed(f"not a SOPS file name, so its plaintext counterpart is unknown: {secret.path}")
    if len(secret.recipients) != 1:
        raise ConversionFailed(
            f"{secret.path} has {len(secret.recipients)} AGE recipients; this round writes back to exactly one"
        )
    secret.string_data[PASSWORD_FIELD] = password
    source = secret.path.with_name(secret.path.name[: -len(SOPS_SUFFIX)] + TO_SOPS_SUFFIX)
    source.write_text(dump_yaml_to_string(secret.document), encoding="utf-8")
    try:
        encrypt_to_sops_files(str(secret.path.parent), secret.recipients[0], secret.private_key)
    except (SOPSEncryptionError, OSError):
        # A plaintext password left behind in a git clone is the one outcome this whole tool
        # exists to prevent, so it goes before the error is passed on.
        source.unlink(missing_ok=True)
        raise
    if source.exists():
        source.unlink()
        raise ConversionFailed(f"SOPS left {source} in plain text; the secret was not written")


async def check_repository_secrets(
    clone: Path, projects: Path, old_private: str, new_private: str, check: FinalCheck, pat: str | None = None
) -> None:
    """The final check's argo half: every repository secret still says what its project file says.

    This is the check that was missing. ``--assert-old-key-dead`` walks these files and proves
    the old KEY does not open them, which stays true after a PAT round that skipped them -- they
    were re-encrypted by the key round and their password was never touched. So it reported CLEAN
    while ArgoCD held a withdrawn token and every sync of every project would have failed.

    The comparison is against the project file rather than against the token, for the same reason
    the round writes from the project file: these secrets are derived, and a repository that does
    not use the shared token has its own password there quite legitimately. With ``pat`` the
    password is ALSO held to the new token when it is one, which is what makes "the old token is
    in no argo secret" a measurement instead of an inference.
    """
    pairing, todo, closed = await plan_argo_round(clone, projects, old_private, new_private)
    for secret, repository, _password in todo:
        check.argo_drift.append(
            f"ArgoCD repository secret disagrees with its project file: {secret.path}"
            f" ({secret.name}, derived from {repository.path}#{repository.field_name})"
        )
    for secret in pairing.secrets_without_project:
        check.argo_drift.append(f"no project file accounts for this ArgoCD repository secret: {secret.path}")
    for path in pairing.unreadable:
        check.argo_drift.append(f"opens with neither key: {path}")
    for name in closed:
        check.argo_drift.append(f"opens with neither key: {name}")
    if pat is None:
        return
    for secret, _repository in pairing.pairs:
        password = secret.password
        if password is not None:
            check_token(f"{secret.path} ({secret.name})", password, pat, check)
