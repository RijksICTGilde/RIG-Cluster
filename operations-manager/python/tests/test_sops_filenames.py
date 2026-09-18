"""De suffixen van de versleutelpijplijn horen bij een eigenaar.

De twee namen die een secret draagt stonden op zeventien plekken met de hand gebouwd of
met ``replace()`` omgezet. Deze toetsen pinnen de eigenaar.
"""

import os
import re

import pytest
from opi.connectors.git import GitConnector
from opi.generation.manifests import ManifestGenerator
from opi.manager.project_manager import _COMPONENT_MANIFEST_EXTENSIONS, _is_generated
from opi.services.catalog.image_registries.manifest_pass import _SKIP_SUFFIXES
from opi.utils.naming import generate_helm_values_filename
from opi.utils.sops import SOPS_SUFFIX, TO_SOPS_SUFFIX, sops_filenames


class TestSuffixen:
    def test_waarden_liggen_vast(self) -> None:
        """De namen staan in het GitOps-contract; een hernoeming breekt de CMP-plugin."""
        assert TO_SOPS_SUFFIX == ".to-sops.yaml"
        assert SOPS_SUFFIX == ".sops.yaml"


class TestSopsFilenames:
    def test_kale_naam_geeft_beide_namen(self) -> None:
        namen = sops_filenames("demo-secret")
        assert namen.plaintext == "demo-secret.to-sops.yaml"
        assert namen.encrypted == "demo-secret.sops.yaml"

    def test_is_een_tuple_van_plat_dan_versleuteld(self) -> None:
        assert tuple(sops_filenames("demo-secret")) == ("demo-secret.to-sops.yaml", "demo-secret.sops.yaml")

    def test_platte_naam_wordt_versleutelde_naam(self) -> None:
        assert sops_filenames("demo-secret.to-sops.yaml").encrypted == "demo-secret.sops.yaml"

    def test_versleutelde_naam_wordt_platte_naam(self) -> None:
        assert sops_filenames("demo-secret.sops.yaml").plaintext == "demo-secret.to-sops.yaml"

    def test_naam_die_de_suffix_al_draagt_verandert_niet(self) -> None:
        """Idempotent, want beide helften van een paar komen door dezelfde aanroep."""
        assert sops_filenames("demo-secret.to-sops.yaml").plaintext == "demo-secret.to-sops.yaml"
        assert sops_filenames("demo-secret.sops.yaml").encrypted == "demo-secret.sops.yaml"

    def test_pad_blijft_heel(self) -> None:
        """Aanroepers geven relatieve paden door, niet alleen basenamen."""
        assert sops_filenames("odcn/demo/values.to-sops.yaml").encrypted == "odcn/demo/values.sops.yaml"

    def test_punten_in_de_naam_blijven_staan(self) -> None:
        assert sops_filenames("registry.example.com-secret").encrypted == "registry.example.com-secret.sops.yaml"


class TestAanroepersGebruikenDeEigenaar:
    def test_manifestgenerator_schrijft_de_platte_naam(self, tmp_path) -> None:
        template = tmp_path / "secret.yaml.jinja"
        template.write_text("kind: Secret\n")
        pad = ManifestGenerator().create_manifest_file(
            template_path=str(template),
            values={},
            output_dir=str(tmp_path),
            output_filename="demo-secret",
            use_sops=True,
        )
        assert os.path.basename(pad) == sops_filenames("demo-secret").plaintext

    def test_decrypt_sops_noemt_de_versleutelde_naam(self, tmp_path) -> None:
        for naam in ("a.to-sops.yaml", "a.sops.yaml", "b.to-sops.yaml"):
            (tmp_path / naam).write_text("kind: Secret\n")
        ManifestGenerator().create_kustomization_files(
            output_dir=str(tmp_path),
            sops_files=["a.to-sops.yaml", "a.sops.yaml", "b.to-sops.yaml"],
        )
        inhoud = (tmp_path / "decrypt-sops.yaml").read_text()
        assert "a.to-sops.yaml" not in inhoud
        assert inhoud.count("a.sops.yaml") == 1
        assert "b.sops.yaml" in inhoud

    def test_helm_values_gebruiken_dezelfde_suffix(self) -> None:
        naam = generate_helm_values_filename("productie", "docs", encrypted=True)
        assert naam.endswith(SOPS_SUFFIX)
        assert sops_filenames(naam).plaintext == "productie-docs-helm-values.to-sops.yaml"

    def test_prune_herkent_beide_helften(self) -> None:
        assert SOPS_SUFFIX in _COMPONENT_MANIFEST_EXTENSIONS
        assert TO_SOPS_SUFFIX in _COMPONENT_MANIFEST_EXTENSIONS

    def test_vorige_ciphertext_van_een_geschreven_secret_blijft(self) -> None:
        """De prune mag de .sops.yaml van een deze run geschreven .to-sops.yaml niet opruimen."""
        assert _is_generated("demo-secret.sops.yaml", {"demo-secret.to-sops.yaml"})
        assert not _is_generated("oud-secret.sops.yaml", {"demo-secret.to-sops.yaml"})

    def test_manifestpas_slaat_beide_helften_over(self) -> None:
        assert _SKIP_SUFFIXES == (SOPS_SUFFIX, TO_SOPS_SUFFIX)

    def test_commitgrendel_weigert_de_platte_naam(self, tmp_path) -> None:
        connector = GitConnector.__new__(GitConnector)
        connector._GitConnector__working_dir = str(tmp_path)
        connector._get_server_context = lambda: "testserver"

        (tmp_path / sops_filenames("lek").plaintext).write_text("wachtwoord: geheim\n")
        with pytest.raises(RuntimeError, match=re.escape("lek.to-sops.yaml")):
            connector._abort_if_plaintext_secrets_present()

        os.remove(tmp_path / sops_filenames("lek").plaintext)
        (tmp_path / sops_filenames("lek").encrypted).write_text("wachtwoord: ENC[...]\n")
        connector._abort_if_plaintext_secrets_present()
