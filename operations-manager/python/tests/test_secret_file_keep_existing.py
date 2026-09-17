"""De gedeelde secretschrijver neemt een bestaande waarde over als de dienst daarom vraagt."""

import os
import shutil

import pytest
import yaml
from opi.generation.manifests import ManifestGenerator
from opi.manager.project_manager import ProjectManager
from opi.services.catalog.base import SecretFileSpec
from opi.utils.sops import encrypt_to_sops_files
from tests.test_sops_skip_unchanged import PRIVATE_KEY, PUBLIC_KEY

pytestmark = pytest.mark.skipif(
    shutil.which("sops") is None or shutil.which("age") is None,
    reason="requires the sops and age binaries",
)

TEMPLATE = os.path.join(os.path.dirname(__file__), "..", "manifests", "generic-secret.yaml.to-sops.jinja")
NAME = "productie-fundament-oauth2-cookie"
WRONG_KEY = "REDACTED-AGE-PRIVATE-KEY-SEE-SECURITY-NOTICE"


def _write(directory: str, value: str, *, keep: bool = True, private_key: str | None = PRIVATE_KEY) -> str:
    """Schrijf het cookie-secret en geef de waarde uit de geschreven plaintext terug."""
    pm = ProjectManager.__new__(ProjectManager)
    pm._manifest_generator = ManifestGenerator()
    created_files: list[str] = []
    pm._write_secret_file(
        SecretFileSpec(secret_name=NAME, secret_pairs={"cookie-secret": value}, keep_existing_values=keep),
        deployment_name="productie",
        namespace="rig-demo",
        output_dir=directory,
        template_path=TEMPLATE,
        created_files=created_files,
        private_key=private_key,
    )
    assert created_files == [f"{NAME}-secret.to-sops.yaml"]
    with open(os.path.join(directory, created_files[0])) as f:
        return yaml.safe_load(f)["stringData"]["cookie-secret"]


def _sops_path(directory: str) -> str:
    return os.path.join(directory, f"{NAME}-secret.sops.yaml")


def _read(path: str) -> str:
    with open(path) as f:
        return f.read()


class TestKeepExistingValues:
    def test_zonder_vorige_ciphertext_komt_de_nieuwe_waarde(self, tmp_path):
        assert _write(str(tmp_path), "eerste") == "eerste"

    def test_de_vorige_waarde_gaat_voor(self, tmp_path):
        d = str(tmp_path)
        _write(d, "eerste")
        encrypt_to_sops_files(d, PUBLIC_KEY, PRIVATE_KEY)

        assert _write(d, "tweede") == "eerste"

    def test_tweede_run_laat_de_ciphertext_ongemoeid(self, tmp_path):
        d = str(tmp_path)
        _write(d, "eerste")
        encrypt_to_sops_files(d, PUBLIC_KEY, PRIVATE_KEY)
        first = _read(_sops_path(d))

        _write(d, "tweede")
        encrypt_to_sops_files(d, PUBLIC_KEY, PRIVATE_KEY)

        assert _read(_sops_path(d)) == first

    def test_zonder_de_vlag_wint_de_nieuwe_waarde(self, tmp_path):
        d = str(tmp_path)
        _write(d, "eerste")
        encrypt_to_sops_files(d, PUBLIC_KEY, PRIVATE_KEY)

        assert _write(d, "tweede", keep=False) == "tweede"

    def test_een_sleutel_die_de_vorige_versie_niet_heeft_krijgt_de_nieuwe_waarde(self, tmp_path):
        d = str(tmp_path)
        with open(os.path.join(d, f"{NAME}-secret.to-sops.yaml"), "w") as f:
            f.write("apiVersion: v1\nkind: Secret\nmetadata:\n  name: x\nstringData:\n  anders: oud\n")
        encrypt_to_sops_files(d, PUBLIC_KEY, PRIVATE_KEY)

        assert _write(d, "tweede") == "tweede"


class TestFaaltNaarDeNieuweWaarde:
    def test_onleesbare_ciphertext(self, tmp_path):
        d = str(tmp_path)
        with open(_sops_path(d), "w") as f:
            f.write("dit is: [geen sops")
        assert _write(d, "tweede") == "tweede"

    def test_verkeerde_sleutel(self, tmp_path):
        d = str(tmp_path)
        _write(d, "eerste")
        encrypt_to_sops_files(d, PUBLIC_KEY, PRIVATE_KEY)

        assert _write(d, "tweede", private_key=WRONG_KEY) == "tweede"

    def test_geen_sleutel(self, tmp_path):
        d = str(tmp_path)
        _write(d, "eerste")
        encrypt_to_sops_files(d, PUBLIC_KEY, PRIVATE_KEY)

        assert _write(d, "tweede", private_key=None) == "tweede"
