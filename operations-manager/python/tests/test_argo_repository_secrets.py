"""Tests for scripts/argo_rotation.py: the ArgoCD repository secrets in the PAT round.

The key round already walks these files. This is the other question about them, and it is the
one that was missing: their password is a PLAINTEXT value inside the SOPS file, so a round that
re-encrypts the file leaves the withdrawn token exactly where it was, and the final check
reported CLEAN over it.

**The clone is written by OPI, not by this file.** Every secret here comes out of
``ArgoManager.prepare_repository_variables`` + the real ``manifests/argo-repository*.yaml.jinja``
+ ``encrypt_to_sops_files_or_fail`` -- the three steps ``argo_manager`` itself runs, in that
order. A fixture spelled out by hand would pin the shape this file believes in; this pins the
shape OPI writes, so a change there turns up as a red test instead of as drift in production.

Measured against the real clone of zad-argo-user-applications as well, once, by hand: 6
repository secrets, 11 project files, the round converted all 6 and the decrypted documents
came out identical outside ``stringData.password``. That run is in the PR; these tests are what
keeps it true.
"""

from __future__ import annotations

import base64
import hashlib
import shutil
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from opi.core.config import settings
from opi.generation.manifests import ManifestGenerator
from opi.manager.argo_manager import ArgoManager
from opi.utils.age import BASE64_AGE_PREFIX, encrypt_age_content
from opi.utils.naming import generate_argocd_repository_secret_name, get_output_filename_from_template
from opi.utils.sops import SOPSEncryptionError, encrypt_to_sops_files_or_fail, generate_sops_key_pair
from opi.utils.yaml_util import load_yaml_from_string

_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import argo_rotation  # noqa: E402
import project_rotation as round_tool  # noqa: E402
from argo_rotation import (  # noqa: E402
    ARGO_SECRET_TYPE_LABEL,
    RepositorySecret,
    check_repository_secrets,
    pair_up,
    project_repositories,
    read_repository_secrets,
    write_repository_secret,
)
from key_rotation import (  # noqa: E402
    ConversionFailed,
    FinalCheck,
    Fingerprint,
    decrypt_field,
    loose_values,
    sha256_of,
    sops_files,
    sops_plaintext,
    sops_recipients,
)

pytestmark = pytest.mark.skipif(shutil.which("age") is None, reason="requires the age binary")
needs_sops = pytest.mark.skipif(shutil.which("sops") is None, reason="requires the sops binary")

OLD_TOKEN = "ghp_" + "o" * 36
NEW_TOKEN = "ghp_" + "n" * 36
CLUSTER = "odcn-production"

PROJECT_TEMPLATE = """\
schema-version: 2
name: {name}
display-name: {name}
description: Project for the argo round test
users:
  - email: someone@rijksoverheid.nl
    role: admin
clusters:
  - {cluster}
repositories:
  - name: main-repo
    url: {url}
    username: git
    password: {password}
components:
  - name: component-1
    type: single
    ports:
      inbound: [8000]
      outbound: [80, 443]
deployments:
  - name: deployment-1
    cluster: {cluster}
    namespace: {name}
    repository: main-repo
    components:
      - reference: component-1
        image: ghcr.io/example/app:1
config:
  age-public-key: {project_public}
  age-private-key: |-
{project_private}
"""


async def _encrypted(value: str, public_key: str) -> str:
    block = await encrypt_age_content(value, public_key)
    return BASE64_AGE_PREFIX + base64.b64encode(block.encode()).decode()


def _indent(value: str, spaces: int = 4) -> str:
    return "\n".join(" " * spaces + line for line in value.splitlines())


async def write_project(
    directory: Path,
    name: str,
    platform_public: str,
    *,
    token: str = OLD_TOKEN,
    url: str = "https://github.com/example/app.git",
) -> Path:
    """One project file whose repository password sits on the platform key."""
    project_private, project_public = generate_sops_key_pair()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.yaml"
    path.write_text(
        PROJECT_TEMPLATE.format(
            name=name,
            cluster=CLUSTER,
            url=url,
            password=await _encrypted(token, platform_public),
            project_public=project_public,
            project_private=_indent(await _encrypted(project_private, platform_public)),
        )
    )
    return path


async def write_repository_secret_as_opi_would(
    clone: Path, project: str, repository: dict[str, Any], platform_private: str, platform_public: str
) -> Path:
    """One repository secret, written by the three steps ``argo_manager`` runs.

    ``prepare_repository_variables`` decides the fields and the URL, the real template turns
    them into the manifest and ``encrypt_to_sops_files_or_fail`` seals it. Nothing here spells
    the document out, which is the point: this is the form the round has to leave intact.
    """
    manager = ArgoManager(MagicMock())
    name = generate_argocd_repository_secret_name(project, repository["name"])
    with patch.object(settings, "SOPS_AGE_PRIVATE_KEY", platform_private):
        variables = await manager.prepare_repository_variables(
            name=name, namespace="rig-system", repository=repository, repo_type="git", project_name=project
        )
    template_name = (
        "argo-repository-https.yaml.jinja" if repository["url"].startswith("https://") else "argo-repository.yaml.jinja"
    )
    directory = clone / CLUSTER / project
    directory.mkdir(parents=True, exist_ok=True)
    ManifestGenerator().create_manifest_file(
        template_path=str(Path(settings.MANIFESTS_PATH) / template_name),
        values=variables,
        output_dir=str(directory),
        output_filename=get_output_filename_from_template(template_name, name),
        use_sops=True,
    )
    encrypt_to_sops_files_or_fail(str(directory), platform_public, "test", private_key=platform_private)
    return next(directory.glob("*.sops.yaml"))


@pytest.fixture
def platform_keys() -> tuple[str, str]:
    return generate_sops_key_pair()


def decrypted(path: Path, private_key: str) -> dict[str, Any]:
    plaintext = sops_plaintext(path, private_key)
    assert plaintext is not None, f"{path} does not open with this key"
    document = load_yaml_from_string(plaintext)
    assert isinstance(document, dict)
    return document


def raw_hashes(clone: Path) -> dict[str, str]:
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in sops_files(clone)}


async def a_pair(
    tmp_path: Path,
    platform_private: str,
    platform_public: str,
    *,
    in_the_project: str = NEW_TOKEN,
    in_the_secret: str = OLD_TOKEN,
    url: str = "https://github.com/example/app.git",
) -> tuple[Path, Path, Path]:
    """A projects clone and an argo clone that belong together. Returns (projects, argo, secret).

    The defaults are the state step 7 leaves behind halfway: the project file already holds the
    new token and the secret derived from it still holds the old one. That is precisely the gap
    the review found, so it is the starting position of most tests here.
    """
    projects = tmp_path / "zad-projects" / "projects"
    clone = tmp_path / "zad-argo"
    await write_project(projects, "een", platform_public, token=in_the_project, url=url)
    secret = await write_repository_secret_as_opi_would(
        clone,
        "een",
        {
            "name": "main-repo",
            "url": url,
            "username": "git",
            "password": await _encrypted(in_the_secret, platform_public),
        },
        platform_private,
        platform_public,
    )
    return projects, clone, secret


# ---------------------------------------------------------------------------
# the form: only the password moves
# ---------------------------------------------------------------------------


@needs_sops
@pytest.mark.asyncio
async def test_the_round_changes_the_password_and_nothing_else_in_the_document(
    tmp_path: Path, platform_keys: tuple[str, str]
) -> None:
    """The hard requirement: what comes out is what OPI would have written.

    Compared on the DECRYPTED document, field by field, so a changed annotation, a lost label or
    a re-quoted value is a red test. A secret that OPI would rewrite differently on its next pass
    is drift, and drift here stops the deployment of that project at the next sync.
    """
    platform_private, platform_public = platform_keys
    projects, clone, secret = await a_pair(tmp_path, platform_private, platform_public)
    before = decrypted(secret, platform_private)
    assert before["stringData"]["password"] == OLD_TOKEN

    await round_tool.run_argo_round(clone, projects, platform_private, platform_private, dry_run=False)

    after = decrypted(secret, platform_private)
    assert after["stringData"]["password"] == NEW_TOKEN
    before["stringData"]["password"] = NEW_TOKEN
    assert after == before, "the round wrote more than the password"


@needs_sops
@pytest.mark.asyncio
async def test_the_value_comes_from_the_project_file_and_not_from_the_new_token(
    tmp_path: Path, platform_keys: tuple[str, str]
) -> None:
    """These secrets are DERIVED, so the round copies rather than assumes.

    Whatever the project round decided about a repository is what lands here, and that is not
    always the token: a repository can carry its own credentials. Measured on the sandbox, where
    every project points at Forgejo with ``rig-admin`` instead of at GitHub.
    """
    platform_private, platform_public = platform_keys
    projects, clone, secret = await a_pair(
        tmp_path,
        platform_private,
        platform_public,
        in_the_project="not-a-github-token",
        in_the_secret="something-else",
        url="https://forgejo.example.nl/rig-admin/zad-deployments.git",
    )

    await round_tool.run_argo_round(clone, projects, platform_private, platform_private, dry_run=False)

    assert decrypted(secret, platform_private)["stringData"]["password"] == "not-a-github-token"


@needs_sops
@pytest.mark.asyncio
async def test_a_project_password_that_is_not_on_the_platform_key_is_reported_and_not_refused(
    tmp_path: Path, platform_keys: tuple[str, str], capsys: pytest.CaptureFixture
) -> None:
    """The shape the whole sandbox has: ``plain:`` credentials instead of ``base64+age:``.

    The project round skips such a field -- there is nothing on the platform key to convert --
    so there is nothing for this round to derive either. Treating its secret as one no project
    accounts for would stop the round on all eleven sandbox projects at once, and writing a
    guessed value into it would be worse. It is named, and the round goes on.
    """
    platform_private, platform_public = platform_keys
    projects, clone, secret = await a_pair(tmp_path, platform_private, platform_public)
    project_file = projects / "een.yaml"
    project_file.write_text(project_file.read_text().replace(BASE64_AGE_PREFIX, "plain:", 1))
    before = raw_hashes(clone)

    plan, converted = await round_tool.run_argo_round(
        clone, projects, platform_private, platform_private, dry_run=False
    )
    round_tool.report_argo(plan, converted, clone, projects, dry_run=False)

    assert round_tool.argo_problems(plan) == []
    assert [s.path for s, _r in plan.pairing.without_platform_password] == [secret]
    assert converted == []
    assert raw_hashes(clone) == before
    assert "holds no platform-key password" in capsys.readouterr().out


@needs_sops
@pytest.mark.asyncio
async def test_the_dry_run_previews_the_round_the_operator_is_about_to_start(
    tmp_path: Path, platform_keys: tuple[str, str]
) -> None:
    """The preview runs BEFORE the project files are written, and has to say so truthfully.

    In the ordinary case both sides still hold the old token when the dry run looks: the project
    round has not run yet. The decision is therefore taken on the SECRET's own password against
    the current token, not on the project file -- so the preview and the real run measure the
    same thing, whichever order they run in. Deriving from the project file needed a separate
    "pretend it already says the new token" argument for the preview, and that argument is what
    hid real drift: every secret differed from it, so every secret read as ordinary work.
    """
    platform_private, platform_public = platform_keys
    projects, clone, _secret = await a_pair(
        tmp_path, platform_private, platform_public, in_the_project=OLD_TOKEN, in_the_secret=OLD_TOKEN
    )

    _plan, before_the_project_round = await round_tool.run_argo_round(
        clone, projects, platform_private, platform_private, dry_run=True, current_pat=OLD_TOKEN, new_pat=NEW_TOKEN
    )
    # The state the project round leaves behind: the file already says the new token.
    project_file = projects / "een.yaml"
    project_file.write_text(
        project_file.read_text().replace(
            await _encrypted(OLD_TOKEN, platform_public), await _encrypted(NEW_TOKEN, platform_public)
        )
    )
    _plan, after_the_project_round = await round_tool.run_argo_round(
        clone, projects, platform_private, platform_private, dry_run=True, current_pat=OLD_TOKEN, new_pat=NEW_TOKEN
    )

    assert len(before_the_project_round) == 1
    assert after_the_project_round == before_the_project_round


@needs_sops
@pytest.mark.asyncio
async def test_a_secret_on_another_token_keeps_it_and_is_named_with_its_drift(
    tmp_path: Path, platform_keys: tuple[str, str], capsys: pytest.CaptureFixture
) -> None:
    """The live case this conditional exists for, on the argo side.

    Measured against the real clones: two of the 67 repository secrets hold a value the other 65
    do not, on the same GitHub URL, and for one of them the project file says something else
    again -- drift that was already there. The old round derived the value from the project file
    and would have written over both without a word, and the drift with it. Now the secret keeps
    its password, and both facts are printed: what was left alone, and what disagrees.
    """
    platform_private, platform_public = platform_keys
    older_token = "ghp_" + "x" * 36
    projects, clone, secret = await a_pair(
        tmp_path, platform_private, platform_public, in_the_project=OLD_TOKEN, in_the_secret=older_token
    )
    before = raw_hashes(clone)

    plan, converted = await round_tool.run_argo_round(
        clone, projects, platform_private, platform_private, dry_run=False, current_pat=OLD_TOKEN, new_pat=NEW_TOKEN
    )
    round_tool.report_argo(plan, converted, clone, projects, dry_run=False)
    printed = capsys.readouterr().out

    assert converted == []
    assert [s.path for s, _r, _reason in plan.kept] == [secret]
    assert decrypted(secret, platform_private)["stringData"]["password"] == older_token
    assert raw_hashes(clone) == before
    assert "its password is not the current PAT" in printed
    # The drift was there before this round and is reported as such, not flattened into silence.
    assert len(plan.drift) == 1
    assert "disagrees with its project file" in printed
    assert round_tool.argo_problems(plan) == [], "drift is a finding for a person, not a stop"


@needs_sops
@pytest.mark.asyncio
async def test_a_dry_run_writes_nothing(tmp_path: Path, platform_keys: tuple[str, str]) -> None:
    platform_private, platform_public = platform_keys
    projects, clone, secret = await a_pair(tmp_path, platform_private, platform_public)
    before = raw_hashes(clone)

    _plan, converted = await round_tool.run_argo_round(
        clone, projects, platform_private, platform_private, dry_run=True
    )

    assert len(converted) == 1, "the dry run still has to SAY what it would do"
    assert raw_hashes(clone) == before
    assert decrypted(secret, platform_private)["stringData"]["password"] == OLD_TOKEN


@needs_sops
@pytest.mark.asyncio
async def test_a_second_round_rewrites_nothing_at_all(tmp_path: Path, platform_keys: tuple[str, str]) -> None:
    """Idempotent down to the bytes, and that is stronger than "it converted nothing".

    SOPS is non-deterministic: re-encrypting the same plaintext produces a different nonce and a
    different MAC, so a round that wrote the unchanged value back would show up as a changed
    file in the argo clone on every run. That is noise in a repo ArgoCD watches.
    """
    platform_private, platform_public = platform_keys
    projects, clone, _secret = await a_pair(tmp_path, platform_private, platform_public)
    await round_tool.run_argo_round(clone, projects, platform_private, platform_private, dry_run=False)
    after_first = raw_hashes(clone)

    _plan, converted = await round_tool.run_argo_round(
        clone, projects, platform_private, platform_private, dry_run=False
    )

    assert converted == []
    assert raw_hashes(clone) == after_first


@needs_sops
@pytest.mark.asyncio
async def test_the_recipient_of_the_file_is_kept(tmp_path: Path, platform_keys: tuple[str, str]) -> None:
    """The PAT round does not move keys; that is the key round's job.

    Doing both here would hide one operation inside the other: a rotation that was never asked
    for would ride along with a token replacement, and the fingerprint of the key round would
    have nothing to say about it.
    """
    platform_private, platform_public = platform_keys
    projects, clone, secret = await a_pair(tmp_path, platform_private, platform_public)

    await round_tool.run_argo_round(clone, projects, platform_private, platform_private, dry_run=False)

    assert sops_recipients(secret) == [platform_public]


# ---------------------------------------------------------------------------
# an SSH key is not a PAT
# ---------------------------------------------------------------------------


@needs_sops
@pytest.mark.asyncio
async def test_a_secret_on_an_ssh_key_is_left_alone(tmp_path: Path, platform_keys: tuple[str, str]) -> None:
    """``argo_manager`` picks the SSH template for a repository that is not HTTPS.

    That template writes ``sshPrivateKey`` and no ``password`` at all -- "git SSH key/HTTPS-
    wachtwoord", as the call site puts it. The round has to read that off the decrypted document
    rather than off the file name, and it has to leave the file untouched down to the bytes.
    """
    platform_private, platform_public = platform_keys
    projects, clone, secret = await a_pair(
        tmp_path, platform_private, platform_public, url="git@github.com:example/app.git"
    )
    document = decrypted(secret, platform_private)
    assert "password" not in document["stringData"], "the SSH template is supposed to write no password"
    before = raw_hashes(clone)

    secrets, _unreadable = read_repository_secrets(clone, platform_private)
    pairing = pair_up(secrets, project_repositories(projects))
    await round_tool.run_argo_round(clone, projects, platform_private, platform_private, dry_run=False)

    assert [s.path for s in pairing.ssh_form] == [secret]
    assert pairing.pairs == []
    assert raw_hashes(clone) == before
    assert decrypted(secret, platform_private) == document


# ---------------------------------------------------------------------------
# the coupling, both directions
# ---------------------------------------------------------------------------


@needs_sops
@pytest.mark.asyncio
async def test_a_secret_no_project_accounts_for_stops_the_round(tmp_path: Path, platform_keys: tuple[str, str]) -> None:
    """The direction that IS drift, and it stops before a byte is written.

    Nothing maintains that password: the round cannot derive a value for it, and once the old
    token is withdrawn it hands ArgoCD a dead credential at the next sync. Converting the rest
    and leaving this one would be the half-converted state the whole tool exists to avoid.
    """
    platform_private, platform_public = platform_keys
    projects, clone, _secret = await a_pair(tmp_path, platform_private, platform_public)
    await write_repository_secret_as_opi_would(
        clone,
        "weggegooid",
        {
            "name": "main-repo",
            "url": "https://github.com/example/app.git",
            "username": "git",
            "password": await _encrypted(OLD_TOKEN, platform_public),
        },
        platform_private,
        platform_public,
    )
    before = raw_hashes(clone)

    plan, _converted = await round_tool.run_argo_round(
        clone, projects, platform_private, platform_private, dry_run=True
    )
    problems = round_tool.argo_problems(plan)

    assert len(problems) == 1
    assert "weggegooid-main-repo" in problems[0]
    assert raw_hashes(clone) == before


@needs_sops
@pytest.mark.asyncio
async def test_a_project_without_a_secret_is_named_but_does_not_stop_the_round(
    tmp_path: Path, platform_keys: tuple[str, str], capsys: pytest.CaptureFixture
) -> None:
    """The other direction, and it is NOT a stop -- measured, not reasoned about.

    On the real clone of zad-argo-user-applications 5 of the 11 project files have no directory
    of their own, 4 of them with a deployment on the cluster that clone holds. OPI writes these
    files when it PROCESSES a project, and those had not been processed since. A stop would
    refuse the round on a normal state, and the only repair for it is to reprocess every project
    -- which drags every other pending change into production and turns a targeted key change
    into a broad rollout.

    Named and counted all the same: a silent skip is exactly how the ArgoCD secrets fell out of
    the round in the first place.
    """
    platform_private, platform_public = platform_keys
    projects, clone, _secret = await a_pair(tmp_path, platform_private, platform_public)
    await write_project(projects, "nooit-verwerkt", platform_public, token=NEW_TOKEN)

    plan, converted = await round_tool.run_argo_round(clone, projects, platform_private, platform_private, dry_run=True)
    round_tool.report_argo(plan, converted, clone, projects, dry_run=True)

    assert round_tool.argo_problems(plan) == []
    assert [r.project for r in plan.pairing.repositories_without_secret] == ["nooit-verwerkt"]
    printed = capsys.readouterr().out
    assert "nooit-verwerkt/main-repo" in printed
    assert "nooit-verwerkt-main-repo" in printed, "the names it was looked up under, so the miss is checkable"


@needs_sops
@pytest.mark.asyncio
async def test_the_infrastructure_secret_of_the_same_repository_is_matched_too(
    tmp_path: Path, platform_keys: tuple[str, str]
) -> None:
    """One repository can own two secrets, and the second one is easy to miss.

    ``argo_manager`` writes a second repository secret for a project's infrastructure app, named
    after ``generate_infrastructure_application_name`` instead of after the project. On the real
    clone that was 1 of the 6. Matching on the project name alone would report it as a secret no
    project accounts for, and stop the round on something that is entirely normal.
    """
    platform_private, platform_public = platform_keys
    projects, clone, secret = await a_pair(tmp_path, platform_private, platform_public)
    infrastructure = await write_repository_secret_as_opi_would(
        clone,
        "een-infrastructure",
        {
            "name": "main-repo",
            "url": "https://github.com/example/app.git",
            "username": "git",
            "password": await _encrypted(OLD_TOKEN, platform_public),
        },
        platform_private,
        platform_public,
    )

    plan, converted = await round_tool.run_argo_round(
        clone, projects, platform_private, platform_private, dry_run=False
    )

    assert round_tool.argo_problems(plan) == []
    assert len(converted) == 2
    assert decrypted(secret, platform_private)["stringData"]["password"] == NEW_TOKEN
    assert decrypted(infrastructure, platform_private)["stringData"]["password"] == NEW_TOKEN


# ---------------------------------------------------------------------------
# the final check
# ---------------------------------------------------------------------------


@needs_sops
@pytest.mark.asyncio
async def test_the_final_check_sees_a_secret_left_on_the_old_token(
    tmp_path: Path, platform_keys: tuple[str, str]
) -> None:
    """The gap the review found, in the shape it had.

    The old KEY opens nothing here -- the file was re-encrypted by the key round -- and every
    existing half of the final check is happy. The password inside it is still the withdrawn
    token, and without this half the operator reads CLEAN and revokes it.
    """
    platform_private, platform_public = platform_keys
    projects, clone, _secret = await a_pair(tmp_path, platform_private, platform_public)
    old_private, _old_public = generate_sops_key_pair()

    check = FinalCheck()
    await check_repository_secrets(clone, projects, old_private, platform_private, check)

    assert not check.clean
    assert len(check.argo_drift) == 1
    assert "disagrees with its project file" in check.argo_drift[0]


@needs_sops
@pytest.mark.asyncio
async def test_the_final_check_is_clean_once_the_round_has_run(tmp_path: Path, platform_keys: tuple[str, str]) -> None:
    platform_private, platform_public = platform_keys
    projects, clone, _secret = await a_pair(tmp_path, platform_private, platform_public)
    old_private, _old_public = generate_sops_key_pair()
    await round_tool.run_argo_round(clone, projects, old_private, platform_private, dry_run=False)

    check = FinalCheck(token_checked=True)
    await check_repository_secrets(clone, projects, old_private, platform_private, check, NEW_TOKEN)

    assert check.clean, check.lines()


@needs_sops
@pytest.mark.asyncio
async def test_the_final_check_holds_the_argo_password_to_the_new_token(
    tmp_path: Path, platform_keys: tuple[str, str]
) -> None:
    """``--pat-file`` is the half that turns "the old token is gone" into a measurement.

    Here the project file and the secret AGREE, so the derivation check is happy -- both of them
    hold the old token, because the project round was never run. Only the comparison against the
    token that is supposed to be there catches that.
    """
    platform_private, platform_public = platform_keys
    projects, clone, _secret = await a_pair(
        tmp_path, platform_private, platform_public, in_the_project=OLD_TOKEN, in_the_secret=OLD_TOKEN
    )
    old_private, _old_public = generate_sops_key_pair()

    without = FinalCheck()
    await check_repository_secrets(clone, projects, old_private, platform_private, without)
    withtoken = FinalCheck(token_checked=True)
    await check_repository_secrets(clone, projects, old_private, platform_private, withtoken, NEW_TOKEN)

    assert without.argo_drift == [], "the two sides agree, so the derivation half has nothing to say"
    assert len(withtoken.holds_another_token) == 1


# ---------------------------------------------------------------------------
# a clone holds more than repository secrets
# ---------------------------------------------------------------------------


@needs_sops
@pytest.mark.asyncio
async def test_another_kind_of_sops_file_in_the_clone_is_left_out_of_the_round(
    tmp_path: Path, platform_keys: tuple[str, str]
) -> None:
    """An Application next to the secrets is not a repository secret, and saying so needs the key.

    Everything in these files is encrypted, ``kind`` and the labels included, so that question
    can only be answered after decryption. Answered off the file name instead, every other SOPS
    file in the clone would reach ``pair_up``, where a document without a repository name turns
    into "no project file accounts for this" -- a stop, on a file that is entirely normal.
    """
    platform_private, platform_public = platform_keys
    projects, clone, secret = await a_pair(tmp_path, platform_private, platform_public)
    directory = secret.parent
    (directory / "application.to-sops.yaml").write_text(
        "apiVersion: argoproj.io/v1alpha1\nkind: Application\nmetadata:\n  name: een\n  labels: {}\n"
    )
    encrypt_to_sops_files_or_fail(str(directory), platform_public, "test", private_key=platform_private)
    application = directory / "application.sops.yaml"
    assert application.is_file(), "the other manifest has to be a SOPS file, or this proves nothing"

    secrets, unreadable = read_repository_secrets(clone, platform_private)
    plan, converted = await round_tool.run_argo_round(
        clone, projects, platform_private, platform_private, dry_run=False
    )

    assert [s.path for s in secrets] == [secret]
    assert unreadable == []
    assert round_tool.argo_problems(plan) == []
    assert len(converted) == 1


@needs_sops
@pytest.mark.asyncio
async def test_a_secret_that_opens_with_neither_key_stops_the_round(
    tmp_path: Path, platform_keys: tuple[str, str]
) -> None:
    """Not an empty result but a finding, on both the round and the final check.

    A file this round cannot open is one it cannot hold against its project file either, so
    converting the rest and walking past it is the half-converted clone again -- and the final
    check would then report CLEAN over a file it never read.
    """
    platform_private, platform_public = platform_keys
    projects, clone, _secret = await a_pair(tmp_path, platform_private, platform_public)
    stranger_private, stranger_public = generate_sops_key_pair()
    lost = await write_repository_secret_as_opi_would(
        clone,
        "vreemde-sleutel",
        {
            "name": "main-repo",
            "url": "https://github.com/example/app.git",
            "username": "git",
            "password": await _encrypted(OLD_TOKEN, stranger_public),
        },
        stranger_private,
        stranger_public,
    )

    plan, _converted = await round_tool.run_argo_round(
        clone, projects, platform_private, platform_private, dry_run=True
    )
    check = FinalCheck()
    await check_repository_secrets(clone, projects, platform_private, platform_private, check)

    assert plan.pairing.unreadable == [lost]
    assert f"opens with neither key: {lost}" in round_tool.argo_problems(plan)
    assert not check.clean
    assert [line for line in check.argo_drift if "opens with neither key" in line] == [
        f"opens with neither key: {lost}"
    ]


# ---------------------------------------------------------------------------
# writing one back: the guards around the plaintext
# ---------------------------------------------------------------------------


def _a_secret_at(path: Path, recipients: list[str]) -> RepositorySecret:
    """A repository secret as it stands after decryption, for the guards in the write path."""
    return RepositorySecret(
        path=path,
        recipients=recipients,
        private_key="",
        document={
            "kind": "Secret",
            "metadata": {"name": "een-main-repo", "labels": {ARGO_SECRET_TYPE_LABEL: "repository"}},
            "stringData": {"name": "een-main-repo", "password": OLD_TOKEN},
        },
    )


def test_a_secret_on_more_than_one_recipient_is_refused_and_no_plaintext_is_written(tmp_path: Path) -> None:
    """Writing back to the first of several would drop the others without a word.

    Moving a file to another key is the key round's work. A PAT round that quietly did it too
    would hide one operation inside the other, and the key round's fingerprint would have
    nothing to say about it.
    """
    _private_a, public_a = generate_sops_key_pair()
    _private_b, public_b = generate_sops_key_pair()
    secret = _a_secret_at(tmp_path / "argo-repository-https-een.sops.yaml", [public_a, public_b])

    with pytest.raises(ConversionFailed) as refused:
        write_repository_secret(secret, NEW_TOKEN)

    assert "2 AGE recipients" in str(refused.value)
    assert list(tmp_path.iterdir()) == [], "the plaintext may not be written before the guard runs"


def test_a_file_that_is_not_a_sops_file_is_refused(tmp_path: Path) -> None:
    """``.sops.yaml`` in, ``.to-sops.yaml`` out: without that pairing the plaintext would land
    under a name nothing encrypts, and stay there."""
    _private, public = generate_sops_key_pair()
    secret = _a_secret_at(tmp_path / "argo-repository-https-een.yaml", [public])

    with pytest.raises(ConversionFailed) as refused:
        write_repository_secret(secret, NEW_TOKEN)

    assert "not a SOPS file name" in str(refused.value)
    assert list(tmp_path.iterdir()) == []


def test_a_failing_encryption_takes_the_plaintext_with_it(tmp_path: Path) -> None:
    """The one outcome this whole tool exists to prevent: a readable password in a git clone.

    The plaintext has to be on disk for SOPS to read it, so the window exists; what may not
    survive is the failure. ``sops`` not being on the PATH is enough to produce this.
    """
    _private, public = generate_sops_key_pair()
    secret = _a_secret_at(tmp_path / "argo-repository-https-een.sops.yaml", [public])

    with (
        patch.object(argo_rotation, "encrypt_to_sops_files", side_effect=SOPSEncryptionError("no sops here")),
        pytest.raises(SOPSEncryptionError),
    ):
        write_repository_secret(secret, NEW_TOKEN)

    assert list(tmp_path.iterdir()) == []


def test_sops_leaving_the_plaintext_behind_is_a_failure_and_the_file_still_goes(tmp_path: Path) -> None:
    """An encryption that reports success and removes nothing is the same leak without the error.

    So the outcome is measured rather than the exit code: the ``.to-sops.yaml`` is gone, and the
    round hears that the secret was not written.
    """
    _private, public = generate_sops_key_pair()
    secret = _a_secret_at(tmp_path / "argo-repository-https-een.sops.yaml", [public])

    with (
        patch.object(argo_rotation, "encrypt_to_sops_files"),
        pytest.raises(ConversionFailed) as refused,
    ):
        write_repository_secret(secret, NEW_TOKEN)

    assert "in plain text" in str(refused.value)
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# the entry point: three places in one round, or nothing at all
# ---------------------------------------------------------------------------


async def a_round_to_run(
    tmp_path: Path, old_private: str, old_public: str, new_private: str
) -> tuple[Path, Path, Path, Path, list[str]]:
    """Everything ``replace-git-pat.py`` touches, all three of them on the old token.

    Returns the projects directory, the argo clone, the secret in it, the loose-value file and
    the arguments. The record is pre-written the way a key round leaves it, because the
    correction of exactly the replaced entries is part of what the round has to do.
    """
    projects, clone, secret = await a_pair(
        tmp_path, old_private, old_public, in_the_project=OLD_TOKEN, in_the_secret=OLD_TOKEN
    )
    loose = tmp_path / "config.py"
    loose.write_text(f'PROJECT_REPO_PASSWORD = "{await _encrypted(OLD_TOKEN, old_public)}"\n')
    (tmp_path / "old_key.txt").write_text(f"{old_private}\n")
    (tmp_path / "key.txt").write_text(f"{new_private}\n")
    (tmp_path / "pat.txt").write_text(f"{NEW_TOKEN}\n")
    (tmp_path / "pat-current.txt").write_text(f"{OLD_TOKEN}\n")
    record = Fingerprint()
    record.set(f"{loose}#PROJECT_REPO_PASSWORD", sha256_of(OLD_TOKEN))
    record.save(tmp_path / "repo-fingerprint.json")
    arguments = [
        "--ja",
        "--no-commit",
        "--projects",
        str(projects),
        "--argo-applications",
        str(clone),
        "--pat-file",
        str(tmp_path / "pat.txt"),
        "--pat-current-file",
        str(tmp_path / "pat-current.txt"),
        "--old-key",
        str(tmp_path / "old_key.txt"),
        "--new-key",
        str(tmp_path / "key.txt"),
        "--fingerprint",
        str(tmp_path / "projects-fingerprint.json"),
        "--repo-fingerprint",
        str(tmp_path / "repo-fingerprint.json"),
    ]
    return projects, clone, secret, loose, arguments


@needs_sops
@pytest.mark.asyncio
async def test_the_entry_point_converts_all_three_places_in_one_round(
    tmp_path: Path, platform_keys: tuple[str, str]
) -> None:
    """Driven over the entry point rather than per function.

    Each of the three fails differently and none of them loudly: a project file hands the
    revoked token to its next git operation, ``PROJECT_REPO_PASSWORD`` hands it to the next NEW
    project, and the argo secret hands it to ArgoCD at the next sync. A round that walks one of
    them and calls it done is the shape this test exists to catch, so all three are read back
    -- and the record next to them, because that is what the following ``--verify`` compares.
    """
    old_private, old_public = platform_keys
    new_private, _new_public = generate_sops_key_pair()
    projects, _clone, secret, loose, arguments = await a_round_to_run(tmp_path, old_private, old_public, new_private)

    with patch.object(round_tool, "loose_paths", return_value=[loose]):
        code = await round_tool.main_replace_pat(arguments)

    assert code == 0
    project = load_yaml_from_string((projects / "een.yaml").read_text())
    assert isinstance(project, dict)
    assert await decrypt_field(project["repositories"][0]["password"], new_private) == NEW_TOKEN
    assert await decrypt_field(loose_values(loose)[0].value, new_private) == NEW_TOKEN
    assert decrypted(secret, old_private)["stringData"]["password"] == NEW_TOKEN
    recorded = Fingerprint.load(tmp_path / "repo-fingerprint.json").fields
    assert recorded == {f"{loose}#PROJECT_REPO_PASSWORD": sha256_of(NEW_TOKEN)}


@needs_sops
@pytest.mark.asyncio
async def test_a_finding_in_the_argo_clone_stops_the_round_before_a_single_file_is_written(
    tmp_path: Path, platform_keys: tuple[str, str], capsys: pytest.CaptureFixture
) -> None:
    """Half a round is worse than none, so the finding has to land in the PREVIEW.

    Nothing derives the password of a secret no project accounts for, so no later pass can
    repair it. Converting the project files first and stopping there would leave the two halves
    on different tokens -- with the project files already committed.
    """
    old_private, old_public = platform_keys
    new_private, _new_public = generate_sops_key_pair()
    projects, clone, _secret, loose, arguments = await a_round_to_run(tmp_path, old_private, old_public, new_private)
    await write_repository_secret_as_opi_would(
        clone,
        "weggegooid",
        {
            "name": "main-repo",
            "url": "https://github.com/example/app.git",
            "username": "git",
            "password": await _encrypted(OLD_TOKEN, old_public),
        },
        old_private,
        old_public,
    )
    before = ((projects / "een.yaml").read_text(), loose.read_text(), raw_hashes(clone))

    with patch.object(round_tool, "loose_paths", return_value=[loose]):
        code = await round_tool.main_replace_pat(arguments)

    assert code == 1
    assert "STOPPED" in capsys.readouterr().err
    assert ((projects / "een.yaml").read_text(), loose.read_text(), raw_hashes(clone)) == before


@needs_sops
@pytest.mark.asyncio
async def test_an_argo_clone_that_is_not_there_is_refused_before_anything_is_written(
    tmp_path: Path, platform_keys: tuple[str, str], capsys: pytest.CaptureFixture
) -> None:
    """The flag is required, so the way to get this wrong is a path that does not exist.

    A mistyped clone is an EMPTY clone if it is not checked: every secret then goes missing at
    once, which is the direction that does not stop the round, and ArgoCD keeps the old token.
    """
    old_private, old_public = platform_keys
    new_private, _new_public = generate_sops_key_pair()
    projects, _clone, _secret, loose, arguments = await a_round_to_run(tmp_path, old_private, old_public, new_private)
    arguments[arguments.index("--argo-applications") + 1] = str(tmp_path / "een-typefout")
    before = (projects / "een.yaml").read_text()

    with patch.object(round_tool, "loose_paths", return_value=[loose]):
        code = await round_tool.main_replace_pat(arguments)

    assert code == 2
    assert "not a directory" in capsys.readouterr().err
    assert (projects / "een.yaml").read_text() == before


@needs_sops
@pytest.mark.asyncio
async def test_a_repository_without_credentials_renders_a_secret_the_round_leaves_alone(
    tmp_path: Path, platform_keys: tuple[str, str]
) -> None:
    """Measured, because ``RepositorySecret.password`` makes a claim about this exact shape.

    ``prepare_repository_variables`` hands ``password: ""`` to the HTTPS template, which writes
    ``password:`` with nothing behind it -- and YAML reads that back as None, not as an empty
    string. So this secret is indistinguishable here from the SSH form and the round leaves it
    untouched, even though its project file does carry a password on the platform key. Pinning
    what the templates really produce rather than what the docstring expects them to: if OPI
    starts quoting that field, this test is where it turns up.
    """
    platform_private, platform_public = platform_keys
    projects = tmp_path / "zad-projects" / "projects"
    clone = tmp_path / "zad-argo"
    await write_project(projects, "een", platform_public, token=NEW_TOKEN)
    secret = await write_repository_secret_as_opi_would(
        clone,
        "een",
        {"name": "main-repo", "url": "https://github.com/example/app.git", "username": "git"},
        platform_private,
        platform_public,
    )
    assert decrypted(secret, platform_private)["stringData"]["password"] is None
    before = raw_hashes(clone)

    plan, converted = await round_tool.run_argo_round(
        clone, projects, platform_private, platform_private, dry_run=False
    )

    assert [s.credential_form for s in plan.pairing.ssh_form] == ["no credentials"]
    assert (plan.pairing.pairs, converted, plan.closed) == ([], [], [])
    assert raw_hashes(clone) == before


@needs_sops
@pytest.mark.asyncio
async def test_drift_that_was_already_there_survives_the_round_and_the_final_check_reports_it(
    tmp_path: Path, platform_keys: tuple[str, str]
) -> None:
    """The shape the review measured, end to end: the round no longer flattens existing drift.

    A secret on an older token whose project file says something else again. The old round
    derived the secret's value from the project file, so it wrote the drift away and the final
    check -- which asks the same question afterwards -- found nothing left to report. Now the
    secret keeps its value, so the disagreement is still there to be found, and the check reads
    the measurement against the project file rather than the worklist it would have written.
    """
    platform_private, platform_public = platform_keys
    older_token = "ghp_" + "x" * 36
    projects, clone, secret = await a_pair(
        tmp_path, platform_private, platform_public, in_the_project=OLD_TOKEN, in_the_secret=older_token
    )

    await round_tool.run_argo_round(
        clone, projects, platform_private, platform_private, dry_run=False, current_pat=OLD_TOKEN, new_pat=NEW_TOKEN
    )
    check = FinalCheck()
    await check_repository_secrets(clone, projects, platform_private, platform_private, check)

    assert decrypted(secret, platform_private)["stringData"]["password"] == older_token
    assert not check.clean
    assert [line for line in check.argo_drift if "disagrees with its project file" in line] != []


@needs_sops
@pytest.mark.asyncio
async def test_an_argo_secret_that_still_holds_the_current_pat_fails_the_final_check(
    tmp_path: Path, platform_keys: tuple[str, str]
) -> None:
    """The third of the three places, on the assertion that says the old token is dead.

    A secret the round skipped sits on the platform key perfectly well and hands ArgoCD the
    withdrawn credential at the next sync. Equality with the token that was replaced is the only
    thing that sees it.
    """
    platform_private, platform_public = platform_keys
    projects, clone, _secret = await a_pair(
        tmp_path, platform_private, platform_public, in_the_project=OLD_TOKEN, in_the_secret=OLD_TOKEN
    )

    clean = FinalCheck()
    await check_repository_secrets(clone, projects, platform_private, platform_private, clean)
    with_current = FinalCheck()
    await check_repository_secrets(
        clone, projects, platform_private, platform_private, with_current, current_pat=OLD_TOKEN
    )

    assert clean.clean, "both sides agree, so every other half is happy"
    assert not with_current.clean
    assert with_current.still_holds_current_pat != []
