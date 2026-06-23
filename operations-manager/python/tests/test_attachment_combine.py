"""Test the submit-time generator that resolves staged attachments into AGE blocks."""

import shutil
import subprocess

import pytest
from opi.forms.editables.generators import AttachmentStagingResolveGenerator
from opi.services import upload_staging
from opi.utils.age import decrypt_age_block_to_bytes_sync

pytestmark = pytest.mark.skipif(
    shutil.which("age") is None or shutil.which("age-keygen") is None,
    reason="age/age-keygen binary not available",
)


@pytest.fixture
def age_keypair() -> tuple[str, str]:
    result = subprocess.run(["age-keygen"], capture_output=True, text=True, check=True)
    lines = result.stdout.splitlines() + result.stderr.splitlines()
    private_key = next(line for line in lines if line.startswith("AGE-SECRET-KEY"))
    public_key = next(line.split(": ", 1)[1].strip() for line in lines if "public key:" in line.lower())
    return public_key, private_key


def test_resolves_staged_attachment(age_keypair: tuple[str, str]) -> None:
    public_key, private_key = age_keypair
    payload = b"-----BEGIN CERTIFICATE-----\nABC\n-----END CERTIFICATE-----\n"
    token = upload_staging.stage_file(payload, "ca.pem")

    yaml_data = {
        "config": {"age-public-key": public_key},
        "services": [
            {"attachments": {"data": [{"id": "ca", "filename": "ca.pem", "content": f"staging:{token}"}]}},
        ],
    }

    AttachmentStagingResolveGenerator().generate(yaml_data)

    content = yaml_data["services"][0]["attachments"]["data"][0]["content"]
    assert str(content).startswith("-----BEGIN AGE ENCRYPTED FILE-----")
    assert decrypt_age_block_to_bytes_sync(str(content), private_key) == payload
    # Staging entry is cleaned up after resolution.
    assert upload_staging.read_staged(token) is None


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
