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
from opi.utils.sops import encrypt_to_sops_files_or_fail, generate_sops_key_pair
from opi.utils.yaml_util import load_yaml_from_string

_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import project_rotation as round_tool  # noqa: E402
from argo_rotation import (  # noqa: E402
    check_repository_secrets,
    pair_up,
    project_repositories,
    read_repository_secrets,
)
from key_rotation import FinalCheck, sops_files, sops_plaintext, sops_recipients  # noqa: E402

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

    pairing, converted, closed = await round_tool.run_argo_round(
        clone, projects, platform_private, platform_private, dry_run=False
    )
    round_tool.report_argo(pairing, converted, closed, clone, projects, dry_run=False)

    assert round_tool.argo_problems(pairing, closed) == []
    assert [s.path for s, _r in pairing.without_platform_password] == [secret]
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
    round has not run yet. Comparing against the project file as it stands would then report
    "nothing to do" for a round that is about to convert every secret in the clone -- and the dry
    run is the thing the operator reads before starting the irreversible half.
    """
    platform_private, platform_public = platform_keys
    projects, clone, _secret = await a_pair(
        tmp_path, platform_private, platform_public, in_the_project=OLD_TOKEN, in_the_secret=OLD_TOKEN
    )

    _pairing, blind, _closed = await round_tool.run_argo_round(
        clone, projects, platform_private, platform_private, dry_run=True
    )
    _pairing, previewed, _closed = await round_tool.run_argo_round(
        clone, projects, platform_private, platform_private, dry_run=True, expected=NEW_TOKEN
    )

    assert blind == [], "both sides agree today, so a preview without the token sees nothing"
    assert len(previewed) == 1


@needs_sops
@pytest.mark.asyncio
async def test_a_dry_run_writes_nothing(tmp_path: Path, platform_keys: tuple[str, str]) -> None:
    platform_private, platform_public = platform_keys
    projects, clone, secret = await a_pair(tmp_path, platform_private, platform_public)
    before = raw_hashes(clone)

    _pairing, converted, _closed = await round_tool.run_argo_round(
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

    _pairing, converted, _closed = await round_tool.run_argo_round(
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

    pairing, _converted, closed = await round_tool.run_argo_round(
        clone, projects, platform_private, platform_private, dry_run=True
    )
    problems = round_tool.argo_problems(pairing, closed)

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

    pairing, converted, closed = await round_tool.run_argo_round(
        clone, projects, platform_private, platform_private, dry_run=True
    )
    round_tool.report_argo(pairing, converted, closed, clone, projects, dry_run=True)

    assert round_tool.argo_problems(pairing, closed) == []
    assert [r.project for r in pairing.repositories_without_secret] == ["nooit-verwerkt"]
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

    pairing, converted, closed = await round_tool.run_argo_round(
        clone, projects, platform_private, platform_private, dry_run=False
    )

    assert round_tool.argo_problems(pairing, closed) == []
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
