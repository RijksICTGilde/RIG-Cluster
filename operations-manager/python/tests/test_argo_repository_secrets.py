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
import glob
import hashlib
import os
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
from opi.utils.yaml_util import dump_yaml_to_string, load_yaml_from_string

_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import argo_rotation  # noqa: E402
import project_rotation as round_tool  # noqa: E402
from argo_rotation import (  # noqa: E402
    ARGO_SECRET_TYPE_LABEL,
    TO_SOPS_SUFFIX,
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
#: A third token: neither the one being replaced nor the one replacing it.
OLDER_TOKEN = "ghp_" + "x" * 36
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
    older_token = OLDER_TOKEN
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
async def test_half_a_pat_round_is_refused_on_the_argo_side_too(tmp_path: Path, platform_keys: tuple[str, str]) -> None:
    """The same refusal the project round makes, on the other place the conditional lives.

    With only one of the two tokens the branch below falls through to the DERIVATION rule -- the
    key round's behaviour -- while the caller believes it is replacing conditionally. That is the
    blind write returning through the back door, and silently. The engine's twin refusal is
    measured in ``test_a_pat_round_without_the_current_pat_is_refused``; this one had nothing.
    """
    platform_private, platform_public = platform_keys
    projects, clone, _secret = await a_pair(tmp_path, platform_private, platform_public)
    before = raw_hashes(clone)

    for half in ({"new_pat": NEW_TOKEN}, {"current_pat": OLD_TOKEN}):
        with pytest.raises(ConversionFailed, match="both the current and the new PAT"):
            await argo_rotation.plan_argo_round(clone, projects, platform_private, **half)

    assert raw_hashes(clone) == before


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


def test_an_interrupt_during_the_encryption_takes_the_plaintext_with_it(tmp_path: Path) -> None:
    """The same leak, through the door a list of failure types does not cover.

    Ctrl-C while ``sops`` is running is a ``KeyboardInterrupt``: neither of the two failure types
    the test above names, so the cleanup is measured here on something that is not a failure at
    all. Why it has to run anyway: ``write_repository_secret``.
    """
    _private, public = generate_sops_key_pair()
    secret = _a_secret_at(tmp_path / "argo-repository-https-een.sops.yaml", [public])

    with (
        patch.object(argo_rotation, "encrypt_to_sops_files", side_effect=KeyboardInterrupt),
        pytest.raises(KeyboardInterrupt),
    ):
        write_repository_secret(secret, NEW_TOKEN)

    assert list(tmp_path.iterdir()) == []


def test_an_interrupt_halfway_through_the_write_takes_the_plaintext_with_it(tmp_path: Path) -> None:
    """The window BEFORE the encryption: the plaintext going on disk, not the sops call.

    The two tests above interrupt ``encrypt_to_sops_files``, so they say nothing about the step
    in front of it. The tear is staged on the dump: the half file lands under the
    ``.to-sops.yaml`` name and the write then stops, which is exactly what a torn write leaves
    behind.
    """
    _private, public = generate_sops_key_pair()
    secret = _a_secret_at(tmp_path / "argo-repository-https-een.sops.yaml", [public])
    source = tmp_path / "argo-repository-https-een.to-sops.yaml"

    def a_torn_write(document: Any) -> str:
        text = dump_yaml_to_string(document)
        source.write_text(text[: len(text) // 2], encoding="utf-8")
        raise KeyboardInterrupt

    with (
        patch.object(argo_rotation, "dump_yaml_to_string", side_effect=a_torn_write),
        pytest.raises(KeyboardInterrupt),
    ):
        write_repository_secret(secret, NEW_TOKEN)

    assert list(tmp_path.iterdir()) == []


def test_the_plaintext_sops_reads_is_readable_by_nobody_else(tmp_path: Path) -> None:
    """While it exists, the file with the token in it carries 0600 and not the umask's 0644.

    This runs on a shared dev server, so for the length of the sops call every local uid could
    read the password out of a 0644 file. The mode comes from ``mkstemp`` and survives the
    ``os.replace``; it is measured here from inside the encryption, the only moment the file is
    on disk.

    The umask is pinned for the measurement, otherwise this test says more about the machine it
    runs on than about the code: a plain ``open`` produces 0600 all by itself under ``umask
    0077``, so the mode would look right for a reason that has nothing to do with this code.
    Measured with the pin moved to ``umask 0077`` and the temporary written by a plain ``open``
    instead of ``mkstemp``: all 34 tests in this file green; with the pin at ``umask 0022``
    that same write is 1 red, and it is red here alone -- the name and the cleanup are
    untouched by it, so no other test in the file has anything to say about the mode.

    A ``write_text`` moved back inside the ``try`` is a coarser mutation and is 2 red: this
    test on the mode, and the neighbour below on there being no separate file at all at that
    moment, since such a write builds the plaintext straight onto the target's own name.
    """
    _private, public = generate_sops_key_pair()
    secret = _a_secret_at(tmp_path / "argo-repository-https-een.sops.yaml", [public])
    source = tmp_path / "argo-repository-https-een.to-sops.yaml"
    seen: list[int] = []

    def look_at_the_plaintext(*_arguments: Any) -> bool:
        seen.append(source.stat().st_mode & 0o777)
        source.unlink()
        return True

    previous_umask = os.umask(0o022)
    try:
        with patch.object(argo_rotation, "encrypt_to_sops_files", side_effect=look_at_the_plaintext):
            write_repository_secret(secret, NEW_TOKEN)
    finally:
        os.umask(previous_umask)

    assert seen == [0o600]


def test_while_the_plaintext_is_being_written_it_lies_next_to_the_target_and_sops_cannot_see_it(
    tmp_path: Path,
) -> None:
    """The half-written plaintext is a neighbour of the target under a name SOPS does not glob.

    Two things hang on where that temporary lives, and neither is visible in the outcome of a
    successful write. Next to the target is what makes the move a rename inside one filesystem,
    so the ``.to-sops.yaml`` name is either absent or complete and never half a password; a
    temporary in the system temp directory turns ``os.replace`` into a cross-device error on any
    machine where ``/tmp`` is its own filesystem, and drops the plaintext PAT outside the clone,
    where this round's "nothing left behind" check does not look. And the name has to stay out
    of the ``*.to-sops.yaml`` glob, because that glob is what ``encrypt_to_sops_files`` selects
    on -- it is spelled here the way ``opi/utils/sops.py`` spells it rather than with
    ``Path.glob``, which unlike ``glob.glob`` does match a leading dot.

    Both halves of the name are asserted on directly, because the glob assertion below cannot
    carry either of them on its own: ``glob.glob`` skips a leading dot, so it only sees a name
    that has lost the dot AND ends in ``.to-sops.yaml``, and either protection alone keeps it
    quiet. Measured over the 34 tests in this file: handing ``mkstemp`` the ``.to-sops.yaml``
    suffix while the dot stays is 1 red, on the suffix assertion; dropping only the leading dot
    is 0 red, which is the same answer the previous round gave -- the dot is belt over braces
    and the suffix is the brace. Dropping ``dir=source.parent`` is 1 red, on the
    "not in the target's directory" assertion. The glob assertion states the consequence the
    two names are protecting against and stands behind the stricter of them.
    """
    _private, public = generate_sops_key_pair()
    secret = _a_secret_at(tmp_path / "argo-repository-https-een.sops.yaml", [public])
    source = tmp_path / "argo-repository-https-een.to-sops.yaml"
    alongside: list[list[str]] = []
    visible_to_sops: list[list[str]] = []

    def look_around_while_writing(document: Any) -> str:
        alongside.append(sorted(entry.name for entry in tmp_path.iterdir()))
        visible_to_sops.append(sorted(glob.glob(os.path.join(str(tmp_path), "*.to-sops.yaml"))))
        return dump_yaml_to_string(document)

    with (
        patch.object(argo_rotation, "dump_yaml_to_string", side_effect=look_around_while_writing),
        patch.object(argo_rotation, "encrypt_to_sops_files", side_effect=lambda *_a: source.unlink() or True),
    ):
        write_repository_secret(secret, NEW_TOKEN)

    assert len(alongside) == 1, "the plaintext is built exactly once"
    assert len(alongside[0]) == 1, f"the temporary is not in the target's directory: {alongside[0]}"
    assert alongside[0][0] != source.name, "the half-written plaintext carries the target's own name"
    assert not alongside[0][0].endswith(TO_SOPS_SUFFIX), f"the temporary is named a SOPS source: {alongside[0][0]}"
    assert visible_to_sops == [[]], "SOPS globs the half-written plaintext"


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
    tmp_path: Path, old_private: str, old_public: str, new_private: str, *, in_all_three: str = OLD_TOKEN
) -> tuple[Path, Path, Path, Path, list[str]]:
    """Everything ``replace-git-pat.py`` touches, all three of them on the same token.

    Returns the projects directory, the argo clone, the secret in it, the loose-value file and
    the arguments. The record is pre-written the way a key round leaves it, because the
    correction of exactly the replaced entries is part of what the round has to do.

    ``in_all_three`` is what the three places carry. The default is the token being replaced --
    the ordinary round; a caller that passes something else gets the round that has nothing to
    replace anywhere.
    """
    projects, clone, secret = await a_pair(
        tmp_path, old_private, old_public, in_the_project=in_all_three, in_the_secret=in_all_three
    )
    loose = tmp_path / "config.py"
    loose.write_text(f'PROJECT_REPO_PASSWORD = "{await _encrypted(in_all_three, old_public)}"\n')
    (tmp_path / "old_key.txt").write_text(f"{old_private}\n")
    (tmp_path / "key.txt").write_text(f"{new_private}\n")
    (tmp_path / "pat.txt").write_text(f"{NEW_TOKEN}\n")
    (tmp_path / "pat-current.txt").write_text(f"{OLD_TOKEN}\n")
    record = Fingerprint()
    record.set(f"{loose}#PROJECT_REPO_PASSWORD", sha256_of(in_all_three))
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
async def test_the_three_numbers_count_every_place_and_are_printed_by_the_real_round(
    tmp_path: Path, platform_keys: tuple[str, str], capsys: pytest.CaptureFixture
) -> None:
    """The totals the operator reads before revoking: over all three places, and after writing.

    The dry run's copy of this line has its own test, but that one runs with an empty argo clone
    and no loose values -- so nothing there says the other two places reach the totals at all,
    and nothing says the line survives into the real round. Measured: dropping the argo and
    loose terms, or the call that comes after the round has written, leaves the whole rotation
    suite green.

    All three sit on a token that is neither the current nor the new one, so the round has
    nothing to replace anywhere and has to say exactly that, in three numbers and by path.
    """
    old_private, old_public = platform_keys
    new_private, _new_public = generate_sops_key_pair()
    projects, _clone, secret, loose, arguments = await a_round_to_run(
        tmp_path, old_private, old_public, new_private, in_all_three=OLDER_TOKEN
    )

    with patch.object(round_tool, "loose_paths", return_value=[loose]):
        code = await round_tool.main_replace_pat(arguments)
    printed = capsys.readouterr().out

    assert code == 0, printed
    assert "0 fields were replaced with the new PAT" in printed
    assert "3 fields were left as they are (their value is not the current PAT)" in printed
    assert "0 fields open with neither key" in printed
    # And named per place, so a total that adds up over the wrong set still reads wrong here.
    assert f"kept: {projects / 'een.yaml'}#repositories[0].password" in printed
    assert f"kept: {loose}#PROJECT_REPO_PASSWORD" in printed
    assert f"kept: {secret} " in printed


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
    older_token = OLDER_TOKEN
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


@needs_sops
@pytest.mark.asyncio
async def test_a_secret_whose_project_has_a_plain_password_is_held_to_the_token_too(
    tmp_path: Path, platform_keys: tuple[str, str]
) -> None:
    """The gap the review found: the token half ran over ``pairs`` and nothing else.

    The two lists are split on the PROJECT file, and the token question is about the SECRET, so
    a secret in ``without_platform_password`` has to answer for it too. Why: the docstring of
    ``check_repository_secrets``. Not a corner case: the doc names it as the shape of the whole
    sandbox.
    """
    platform_private, platform_public = platform_keys
    projects, clone, _secret = await a_pair(
        tmp_path, platform_private, platform_public, in_the_project=OLD_TOKEN, in_the_secret=OLD_TOKEN
    )
    project_file = projects / "een.yaml"
    project_file.write_text(project_file.read_text().replace(BASE64_AGE_PREFIX, "plain:", 1))

    plan = await argo_rotation.plan_argo_round(clone, projects, platform_private, platform_private)
    check = FinalCheck()
    await check_repository_secrets(
        clone, projects, platform_private, platform_private, check, pat=NEW_TOKEN, current_pat=OLD_TOKEN
    )

    assert plan.pairing.pairs == [], "the pairing puts this secret outside the list the check used to walk"
    assert plan.pairing.without_platform_password != []
    assert not check.clean
    assert check.still_holds_current_pat != []
    assert check.holds_another_token != []
    # The token half adds nothing to the count: the tree walk that opens these as SOPS files
    # counted them already, and counting them again would put the total out by one per secret.
    assert check.counted == 0
