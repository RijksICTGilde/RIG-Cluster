"""De gedeelde secretschrijver neemt een bestaande waarde over als de dienst daarom vraagt."""

import os
import shutil
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from opi.generation.manifests import ManifestGenerator
from opi.manager.project_manager import ProjectManager, _existing_secret_pairs
from opi.services.catalog.base import ManifestContribution, SecretFileSpec
from opi.utils.sops import encrypt_to_sops_files
from tests.test_sops_skip_unchanged import PRIVATE_KEY, PUBLIC_KEY

pytestmark = pytest.mark.skipif(
    shutil.which("sops") is None or shutil.which("age") is None,
    reason="requires the sops and age binaries",
)

TEMPLATE = os.path.join(os.path.dirname(__file__), "..", "manifests", "generic-secret.yaml.to-sops.jinja")
NAME = "productie-fundament-oauth2-cookie"
PROJECT: dict = {
    "name": "demo",
    "components": [{"name": "fundament", "type": "single", "ports": {"inbound": [8080]}, "services": []}],
    "deployments": [
        {
            "name": "productie",
            "cluster": "sandboxed-local",
            "namespace": "demo",
            "components": [{"reference": "fundament", "image": "nginx:1"}],
        }
    ],
}
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

    def test_een_sleutel_die_de_dienst_niet_meer_levert_komt_niet_terug(self, tmp_path):
        d = str(tmp_path)
        with open(os.path.join(d, f"{NAME}-secret.to-sops.yaml"), "w") as f:
            f.write(
                "apiVersion: v1\nkind: Secret\nmetadata:\n  name: x\nstringData:\n  cookie-secret: oud\n  vervallen: x\n"
            )
        encrypt_to_sops_files(d, PUBLIC_KEY, PRIVATE_KEY)

        _write(d, "tweede")

        with open(os.path.join(d, f"{NAME}-secret.to-sops.yaml")) as f:
            assert yaml.safe_load(f)["stringData"] == {"cookie-secret": "oud"}

    def test_een_gewijzigd_aliassjabloon_wordt_opnieuw_opgelost(self, tmp_path):
        d = str(tmp_path)

        def write(password: str, template: str) -> dict[str, str]:
            pm = ProjectManager.__new__(ProjectManager)
            pm._manifest_generator = ManifestGenerator()
            pm._deployment_aliases = {"productie": {"secret": {"redis": {"REDIS_URL": template}}}}
            pm._write_secret_file(
                SecretFileSpec(
                    secret_name=NAME,
                    secret_pairs={"REDIS_PASSWORD": password},
                    secret_type="redis",
                    resolve_aliases=True,
                    keep_existing_values=True,
                ),
                deployment_name="productie",
                namespace="rig-demo",
                output_dir=d,
                template_path=TEMPLATE,
                created_files=[],
                private_key=PRIVATE_KEY,
            )
            with open(os.path.join(d, f"{NAME}-secret.to-sops.yaml")) as f:
                return yaml.safe_load(f)["stringData"]

        write("geheim", "redis://:$REDIS_PASSWORD@oudhost")
        encrypt_to_sops_files(d, PUBLIC_KEY, PRIVATE_KEY)

        assert write("vers", "redis://:$REDIS_PASSWORD@nieuwhost") == {
            "REDIS_PASSWORD": "geheim",
            "REDIS_URL": "redis://:geheim@nieuwhost",
        }


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

    @pytest.mark.parametrize(
        "decrypted",
        ["dit is: [geen yaml", "- een\n- lijst\n", "kind: Secret\n", "stringData:\n  - een lijst\n"],
    )
    def test_onbruikbaar_ontsleuteld_document(self, tmp_path, decrypted):
        path = tmp_path / "x.sops.yaml"
        path.write_text("ciphertext")
        with patch("opi.manager.project_manager.decrypt_sops_with_key", return_value=decrypted):
            assert _existing_secret_pairs(str(path), PRIVATE_KEY) == {}

    def test_alleen_tekstwaarden_worden_overgenomen(self, tmp_path):
        path = tmp_path / "x.sops.yaml"
        path.write_text("ciphertext")
        decrypted = "stringData:\n  cookie-secret: oud\n  poort: 8080\n  leeg: null\n"
        with patch("opi.manager.project_manager.decrypt_sops_with_key", return_value=decrypted):
            assert _existing_secret_pairs(str(path), PRIVATE_KEY) == {"cookie-secret": "oud"}


class TestDeployment:
    """``create_application_manifests`` geeft de projectsleutel door aan de schrijver."""

    async def _run(self, working_dir: str, value: str) -> str:
        with patch("opi.manager.project_manager.KubectlConnector"):
            pm = ProjectManager()
        pm.get_contents = AsyncMock(return_value=PROJECT)
        pm.get_name = AsyncMock(return_value="demo")
        pm._project_file_handler.extract_component_user_env_vars = AsyncMock(return_value={})
        pm._project_file_handler.extract_deployment_component_user_env_vars = AsyncMock(return_value={})
        git = MagicMock()
        git.get_working_dir = AsyncMock(return_value=working_dir)
        spec = SecretFileSpec(secret_name=NAME, secret_pairs={"cookie-secret": value}, keep_existing_values=True)
        with (
            patch("opi.manager.project_manager.get_decoded_project_private_key", AsyncMock(return_value=PRIVATE_KEY)),
            patch(
                "opi.manager.project_manager.collect_manifest_contributions",
                return_value=[ManifestContribution(secret_files=[spec])],
            ),
        ):
            created = await pm.create_application_manifests(PROJECT["deployments"][0], git, "uit")
        assert f"{NAME}-secret.to-sops.yaml" in created
        with open(os.path.join(working_dir, "uit", f"{NAME}-secret.to-sops.yaml")) as f:
            return yaml.safe_load(f)["stringData"]["cookie-secret"]

    async def test_tweede_run_houdt_de_waarde_van_de_eerste(self, tmp_path):
        d = str(tmp_path)
        assert await self._run(d, "eerste") == "eerste"
        encrypt_to_sops_files(os.path.join(d, "uit"), PUBLIC_KEY, PRIVATE_KEY)

        assert await self._run(d, "tweede") == "eerste"
