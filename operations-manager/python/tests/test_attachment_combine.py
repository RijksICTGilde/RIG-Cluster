"""Test the submit-time generator that resolves staged attachments into AGE blocks."""

import shutil
import subprocess

import pytest
from opi.core.config import settings
from opi.forms.editables.generators import AttachmentStagingResolveGenerator
from opi.services import upload_staging
from opi.utils.age import decrypt_age_block_to_bytes_sync, wrap_project_key_sync

pytestmark = pytest.mark.skipif(
    shutil.which("age") is None or shutil.which("age-keygen") is None,
    reason="age/age-keygen binary not available",
)


@pytest.fixture
def age_keypair(monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    """A project key pair, and the platform settings that can unwrap it.

    The resolve generator reads the project PRIVATE key now (the staging store holds the
    upload encrypted), and that key is stored wrapped, so a project built on this pair also
    needs a platform key that opens the wrapping.
    """
    result = subprocess.run(["age-keygen"], capture_output=True, text=True, check=True)
    lines = result.stdout.splitlines() + result.stderr.splitlines()
    private_key = next(line for line in lines if line.startswith("AGE-SECRET-KEY"))
    public_key = next(line.split(": ", 1)[1].strip() for line in lines if "public key:" in line.lower())
    monkeypatch.setattr(settings, "SOPS_AGE_PRIVATE_KEY", private_key)
    monkeypatch.setattr(settings, "SOPS_AGE_PUBLIC_KEY", public_key)
    monkeypatch.setattr(settings, "PROJECT_KEK_PUBLIC_KEY", "")
    monkeypatch.setattr(settings, "PROJECT_KEK_PRIVATE_KEY", "")
    return public_key, private_key


def test_resolves_staged_attachment(age_keypair: tuple[str, str]) -> None:
    public_key, private_key = age_keypair
    payload = b"-----BEGIN CERTIFICATE-----\nABC\n-----END CERTIFICATE-----\n"
    token = upload_staging.stage_file(payload, "ca.pem", public_key)

    yaml_data = {
        "config": {"age-public-key": public_key, "age-private-key": wrap_project_key_sync(private_key)},
        "services": [
            {"attachments": {"data": [{"id": "ca", "filename": "ca.pem", "content": f"staging:{token}"}]}},
        ],
    }

    AttachmentStagingResolveGenerator().generate(yaml_data)

    content = yaml_data["services"][0]["attachments"]["data"][0]["content"]
    assert str(content).startswith("-----BEGIN AGE ENCRYPTED FILE-----")
    assert decrypt_age_block_to_bytes_sync(str(content), private_key) == payload
    # Staging entry is cleaned up after resolution.
    assert upload_staging.read_staged(token, private_key) is None


def test_non_staging_content_untouched(age_keypair: tuple[str, str]) -> None:
    public_key, _ = age_keypair
    yaml_data = {
        "config": {"age-public-key": public_key},
        "services": [
            {"attachments": {"data": [{"id": "ca", "filename": "ca.pem", "content": "base64+age:already"}]}},
        ],
    }
    AttachmentStagingResolveGenerator().generate(yaml_data)
    # Already-stored content is left as-is.
    assert yaml_data["services"][0]["attachments"]["data"][0]["content"] == "base64+age:already"
