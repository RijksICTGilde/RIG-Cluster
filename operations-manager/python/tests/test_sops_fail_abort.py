"""
Regression tests for the SOPS-encryption fail-closed behavior.

Security context: on any SOPS encryption failure the old code early-returned
False (ignored by callers) and left plaintext *.to-sops.yaml files in the
working tree, which then got committed to the GitOps repo by `git add -A`.
The worst case is the ArgoCD repository secret holding the git SSH private
key, the trust root of the whole GitOps setup.

These tests assert:
1. encrypt_to_sops_files attempts every file (no early return) and raises
   SOPSEncryptionError instead of silently returning False.
2. encrypt_to_sops_files_or_fail turns that into a RuntimeError so secret-
   bearing call sites fail closed and never reach commit/push.
3. The git connector's pre-commit guard refuses to stage/commit when any
   plaintext *.to-sops.yaml file remains (defense in depth).

Run in isolation (the full suite fails at collection on an unrelated
fastapi/pydantic import break):

    uv run pytest tests/test_sops_fail_abort.py --noconftest
"""

import asyncio
import os
import subprocess
from collections.abc import Iterator

import pytest
from opi.connectors.git import GitConnector
from opi.utils.sops import (
    SOPSEncryptionError,
    encrypt_to_sops_files,
    encrypt_to_sops_files_or_fail,
)

PUBLIC_KEY = "age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqsdummyy"


def _write_to_sops(directory: str, name: str, secret: str) -> str:
    path = os.path.join(directory, f"{name}.to-sops.yaml")
    with open(path, "w") as f:
        f.write(secret)
    return path


def _fake_sops_always_fails(*args, **kwargs) -> subprocess.CompletedProcess:
    """Stand-in for subprocess.run that mimics a failing `sops --encrypt`."""
    return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="sops: boom")


# ---------------------------------------------------------------------------
# 1. encrypt_to_sops_files: no early return, raises, leaves no secret silently
# ---------------------------------------------------------------------------


def test_encrypt_raises_and_attempts_all_files_on_failure(tmp_path, monkeypatch) -> None:
    """
    GIVEN multiple .to-sops.yaml files and a failing sops binary
    WHEN encrypt_to_sops_files runs
    THEN it raises SOPSEncryptionError naming every failed file (proving it
         did not early-return on the first failure).
    """
    d = str(tmp_path)
    _write_to_sops(d, "argocd-repo-secret", "ssh-private-key: TOP-SECRET")
    _write_to_sops(d, "db-superuser", "password: also-secret")
    _write_to_sops(d, "keycloak-client", "client-secret: nope")

    monkeypatch.setattr(subprocess, "run", _fake_sops_always_fails)

    with pytest.raises(SOPSEncryptionError) as exc:
        encrypt_to_sops_files(d, PUBLIC_KEY)

    message = str(exc.value)
    # Every file must be reported, not just the first one.
    assert "argocd-repo-secret.to-sops.yaml" in message
    assert "db-superuser.to-sops.yaml" in message
    assert "keycloak-client.to-sops.yaml" in message
    assert "platte tekst" in message


def test_encrypt_does_not_delete_plaintext_on_failure(tmp_path, monkeypatch) -> None:
    """A failed encryption must never remove the plaintext source file."""
    d = str(tmp_path)
    plaintext = _write_to_sops(d, "argocd-repo-secret", "ssh-private-key: TOP-SECRET")

    monkeypatch.setattr(subprocess, "run", _fake_sops_always_fails)

    with pytest.raises(SOPSEncryptionError):
        encrypt_to_sops_files(d, PUBLIC_KEY)

    # The plaintext file is still there (so the pre-commit guard can catch it),
    # and no half-written .sops.yaml was produced.
    assert os.path.exists(plaintext)
    assert not os.path.exists(os.path.join(d, "argocd-repo-secret.sops.yaml"))


def test_encrypt_noop_when_no_files(tmp_path) -> None:
    """No .to-sops.yaml files is a legitimate success, not a failure."""
    assert encrypt_to_sops_files(str(tmp_path), PUBLIC_KEY) is True


def test_encrypt_success_path(tmp_path, monkeypatch) -> None:
    """On success the plaintext is removed and the .sops.yaml is written."""
    d = str(tmp_path)
    plaintext = _write_to_sops(d, "db-superuser", "password: secret")

    def fake_ok(*args, **kwargs) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="enc: payload\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_ok)

    assert encrypt_to_sops_files(d, PUBLIC_KEY) is True
    assert not os.path.exists(plaintext)
    encrypted = os.path.join(d, "db-superuser.sops.yaml")
    assert os.path.exists(encrypted)
    with open(encrypted) as f:
        assert f.read() == "enc: payload\n"


# ---------------------------------------------------------------------------
# 2. encrypt_to_sops_files_or_fail: secret-bearing call sites fail closed
# ---------------------------------------------------------------------------


def test_or_fail_raises_runtimeerror_on_encryption_failure(tmp_path, monkeypatch) -> None:
    d = str(tmp_path)
    _write_to_sops(d, "argocd-repo-secret", "ssh-private-key: TOP-SECRET")
    monkeypatch.setattr(subprocess, "run", _fake_sops_always_fails)

    with pytest.raises(RuntimeError) as exc:
        encrypt_to_sops_files_or_fail(d, PUBLIC_KEY, "ArgoCD repository-secret voor project 'demo'")

    message = str(exc.value)
    assert "platte tekst naar git" in message
    assert "ArgoCD repository-secret voor project 'demo'" in message


def test_or_fail_raises_when_plaintext_remains_even_if_encrypt_returns(tmp_path, monkeypatch) -> None:
    """
    Defense beyond the exception: if a future bug let encrypt_to_sops_files
    return without raising while a plaintext file is still on disk, the
    _or_fail wrapper must still refuse.
    """
    d = str(tmp_path)
    _write_to_sops(d, "leftover", "secret: still-here")

    monkeypatch.setattr("opi.utils.sops.encrypt_to_sops_files", lambda directory, key: True)

    with pytest.raises(RuntimeError) as exc:
        encrypt_to_sops_files_or_fail(d, PUBLIC_KEY, "secrets voor deployment 'demo'")

    assert "leftover.to-sops.yaml" in str(exc.value)


def test_or_fail_passes_on_clean_directory(tmp_path) -> None:
    """No files, nothing to encrypt: must not raise."""
    encrypt_to_sops_files_or_fail(str(tmp_path), PUBLIC_KEY, "lege directory")


# ---------------------------------------------------------------------------
# 3. git connector pre-commit guard (defense in depth)
# ---------------------------------------------------------------------------


@pytest.fixture
def git_repo(tmp_path) -> Iterator[tuple[GitConnector, str]]:
    work = tmp_path / "repo"
    work.mkdir()
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    connector = GitConnector(
        repo_url="git@example.invalid:org/zad-deployments.git",
        working_dir=str(work),
        branch="main",
    )
    return connector, str(work)


def test_precommit_guard_aborts_on_leftover_to_sops(git_repo) -> None:
    connector, work = git_repo
    leftover = os.path.join(work, "argocd-repo-secret.to-sops.yaml")
    with open(leftover, "w") as f:
        f.write("ssh-private-key: TOP-SECRET")

    with pytest.raises(RuntimeError) as exc:
        connector._abort_if_plaintext_secrets_present()

    message = str(exc.value)
    assert "argocd-repo-secret.to-sops.yaml" in message
    assert "platte tekst naar git" in message


def test_precommit_guard_finds_nested_to_sops(git_repo) -> None:
    connector, work = git_repo
    nested = os.path.join(work, "odcn", "demo")
    os.makedirs(nested)
    with open(os.path.join(nested, "db.to-sops.yaml"), "w") as f:
        f.write("password: secret")

    with pytest.raises(RuntimeError) as exc:
        connector._abort_if_plaintext_secrets_present()

    assert os.path.join("odcn", "demo", "db.to-sops.yaml") in str(exc.value)


def test_precommit_guard_passes_when_only_encrypted_present(git_repo) -> None:
    connector, work = git_repo
    with open(os.path.join(work, "db.sops.yaml"), "w") as f:
        f.write("enc: payload")
    # Must not raise.
    connector._abort_if_plaintext_secrets_present()


def test_commit_and_push_aborts_before_staging_plaintext(git_repo, monkeypatch) -> None:
    """
    End-to-end of the guard: commit_and_push must raise before it ever runs
    `git add -A`, so no plaintext secret can be staged.

    Driven via asyncio.run so the test does not depend on pytest-asyncio,
    which lets it run with --noconftest while the full suite is broken on an
    unrelated fastapi/pydantic import.
    """
    connector, work = git_repo
    with open(os.path.join(work, "argocd-repo-secret.to-sops.yaml"), "w") as f:
        f.write("ssh-private-key: TOP-SECRET")

    async def _noop() -> None:
        return None

    monkeypatch.setattr(connector, "ensure_repo_cloned", _noop)

    ran_git: list[list[str]] = []

    async def _spy_run_git_command(cmd, cwd=None):
        ran_git.append(cmd)
        return "", "", 0

    monkeypatch.setattr(connector, "_run_git_command", _spy_run_git_command)

    with pytest.raises(RuntimeError):
        asyncio.run(connector.commit_and_push("zou secrets lekken"))

    assert ran_git == [], "git was invoked even though a plaintext secret was present"
