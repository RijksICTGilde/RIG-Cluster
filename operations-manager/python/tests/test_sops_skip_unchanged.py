"""Real-round-trip tests for SOPS skip-if-unchanged re-encryption.

SOPS is non-deterministic (fresh nonces + MAC + lastmodified per run), so
re-encrypting unchanged secrets rewrote every *.sops.yaml on every deployment
and churned the GitOps repo. encrypt_to_sops_files now keeps the existing
ciphertext when the decrypted plaintext is unchanged, given the matching
private key.

These use the real `sops` + `age` binaries with a throwaway keypair, so they
prove the actual byte-level behaviour rather than mocking it. Skipped when the
binaries are absent.
"""

import os
import shutil
import subprocess

import pytest
from opi.utils.sops import encrypt_to_sops_files

pytestmark = pytest.mark.skipif(
    shutil.which("sops") is None or shutil.which("age") is None,
    reason="requires the sops and age binaries",
)

# Throwaway keypair generated for tests only.
PUBLIC_KEY = "age1xm9xhge3l57wppkjlvf2vfy3pnzlke29l7mp03dv4ryqenf32uqq9x5df2"
PRIVATE_KEY = "AGE-SECRET-KEY-124L5ZZCEZ84QUSVLU8KS2QE6HW7S6NZUCQL0G7PJ7P68NM6TR4XSRUHHC9"

SECRET_A = "apiVersion: v1\nkind: Secret\nmetadata:\n  name: demo\nstringData:\n  password: hunter2\n"
SECRET_B = "apiVersion: v1\nkind: Secret\nmetadata:\n  name: demo\nstringData:\n  password: changed!\n"


def _write_to_sops(directory: str, name: str, content: str) -> str:
    path = os.path.join(directory, f"{name}.to-sops.yaml")
    with open(path, "w") as f:
        f.write(content)
    return path


def _encrypted_path(directory: str, name: str) -> str:
    return os.path.join(directory, f"{name}.sops.yaml")


def _read(path: str) -> str:
    with open(path) as f:
        return f.read()


class TestSkipUnchanged:
    def test_unchanged_secret_keeps_byte_identical_ciphertext(self, tmp_path):
        d = str(tmp_path)
        # First run: encrypt.
        _write_to_sops(d, "demo", SECRET_A)
        encrypt_to_sops_files(d, PUBLIC_KEY, PRIVATE_KEY)
        first = _read(_encrypted_path(d, "demo"))

        # Second run with identical plaintext: ciphertext must be untouched.
        _write_to_sops(d, "demo", SECRET_A)
        encrypt_to_sops_files(d, PUBLIC_KEY, PRIVATE_KEY)
        second = _read(_encrypted_path(d, "demo"))

        assert second == first  # not re-encrypted -> no git churn
        assert not os.path.exists(os.path.join(d, "demo.to-sops.yaml"))  # plaintext dropped

    def test_changed_secret_is_reencrypted(self, tmp_path):
        d = str(tmp_path)
        _write_to_sops(d, "demo", SECRET_A)
        encrypt_to_sops_files(d, PUBLIC_KEY, PRIVATE_KEY)
        first = _read(_encrypted_path(d, "demo"))

        # Different plaintext -> must re-encrypt and reflect the new value.
        _write_to_sops(d, "demo", SECRET_B)
        encrypt_to_sops_files(d, PUBLIC_KEY, PRIVATE_KEY)
        second = _read(_encrypted_path(d, "demo"))

        assert second != first
        out = subprocess.run(
            ["sops", "--decrypt", _encrypted_path(d, "demo")],
            capture_output=True,
            text=True,
            env={**os.environ, "SOPS_AGE_KEY": PRIVATE_KEY},
            check=False,
        )
        assert "changed!" in out.stdout

    def test_unchanged_only_in_semantics_not_formatting(self, tmp_path):
        """Key-order/formatting differences in the plaintext are not a change."""
        d = str(tmp_path)
        _write_to_sops(d, "demo", "stringData:\n  a: '1'\n  b: '2'\n")
        encrypt_to_sops_files(d, PUBLIC_KEY, PRIVATE_KEY)
        first = _read(_encrypted_path(d, "demo"))

        # Same mapping, different key order -> parsed YAML equal -> skip.
        _write_to_sops(d, "demo", "stringData:\n  b: '2'\n  a: '1'\n")
        encrypt_to_sops_files(d, PUBLIC_KEY, PRIVATE_KEY)
        assert _read(_encrypted_path(d, "demo")) == first

    def test_no_private_key_always_reencrypts(self, tmp_path):
        d = str(tmp_path)
        _write_to_sops(d, "demo", SECRET_A)
        encrypt_to_sops_files(d, PUBLIC_KEY)  # no key
        first = _read(_encrypted_path(d, "demo"))

        _write_to_sops(d, "demo", SECRET_A)
        encrypt_to_sops_files(d, PUBLIC_KEY)  # no key -> re-encrypt despite identical content
        assert _read(_encrypted_path(d, "demo")) != first

    def test_wrong_private_key_reencrypts(self, tmp_path):
        d = str(tmp_path)
        _write_to_sops(d, "demo", SECRET_A)
        encrypt_to_sops_files(d, PUBLIC_KEY, PRIVATE_KEY)
        first = _read(_encrypted_path(d, "demo"))

        # A non-matching key can't decrypt the existing file -> must re-encrypt.
        wrong = "AGE-SECRET-KEY-1QQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQSXMDXC7"
        _write_to_sops(d, "demo", SECRET_A)
        encrypt_to_sops_files(d, PUBLIC_KEY, wrong)
        assert _read(_encrypted_path(d, "demo")) != first

    def test_first_time_no_existing_file_encrypts(self, tmp_path):
        d = str(tmp_path)
        _write_to_sops(d, "demo", SECRET_A)
        encrypt_to_sops_files(d, PUBLIC_KEY, PRIVATE_KEY)
        assert os.path.exists(_encrypted_path(d, "demo"))
        assert not os.path.exists(os.path.join(d, "demo.to-sops.yaml"))
