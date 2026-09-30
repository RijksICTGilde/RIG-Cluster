"""De database-image gaat langs dezelfde clusterrewrite als elke andere image.

Waarom dit een eigen toets heeft: zonder de rewrite schrijft OPI het kale ghcr-pad in het
manifest, terwijl de admission van ODCN de pod naar rcr.rijksapps.nl herschrijft. Git en
cluster verschillen dan permanent, en de verwerking wacht op een Synced infrastructuur
voordat ze verdergaat. Een project met een database op de standaardimage kon daardoor niet
meer ververst worden; gemeten op mb-docs-helmfile op 28 september 2026.
"""

import re
from pathlib import Path
from typing import Any

from opi.services.catalog.image_registries.resolution import resolve_project_image

PROJECT_MANAGER = Path(__file__).resolve().parents[1] / "opi" / "manager" / "project_manager.py"

PROJECT: dict[str, Any] = {"name": "demo"}


class TestDeStandaardImageGaatLangsDeProxy:
    def test_het_kale_ghcr_pad_wordt_het_proxypad(self) -> None:
        """Dit is het geval dat de infrastructuur-app OutOfSync hield."""
        resolved = resolve_project_image("ghcr.io/cloudnative-pg/postgresql:17", PROJECT, "odcn-production")

        assert resolved.image == "rcr.rijksapps.nl/ghcr-rig/cloudnative-pg/postgresql:17"
        assert resolved.secret == "ghcr-rig-robot-pull-secret"

    def test_de_tag_blijft_staan(self) -> None:
        """Een rewrite die de tag verzet, verzet de databaseversie."""
        for tag in ("17", "16.4", "17.2-bookworm"):
            resolved = resolve_project_image(f"ghcr.io/cloudnative-pg/postgresql:{tag}", PROJECT, "odcn-production")
            assert resolved.image.endswith(f":{tag}"), resolved.image

    def test_een_image_dat_al_op_het_proxypad_staat_blijft(self) -> None:
        """Anders zou een tweede ronde het pad nog eens ervoor plakken."""
        al_om = "rcr.rijksapps.nl/ghcr-rig/cloudnative-pg/postgresql:17"
        assert resolve_project_image(al_om, PROJECT, "odcn-production").image == al_om

    def test_zonder_regels_verandert_er_niets(self) -> None:
        """Een cluster zonder proxy laat de image met rust, en geeft geen secret."""
        resolved = resolve_project_image("ghcr.io/cloudnative-pg/postgresql:17", PROJECT, "sandboxed-local")

        assert resolved.image == "ghcr.io/cloudnative-pg/postgresql:17"
        assert resolved.secret is None


class TestDeBedrading:
    """Dat de rewerkregel bestaat zegt niets; hij moet ook op dit pad liggen.

    De toetsen hierboven meten ``resolve_project_image`` zelf, en die was al goed toen de
    infrastructuur-app OutOfSync stond. Wat ontbrak was de aanroep. Deze guard leest de bron,
    want het alternatief is de hele manifestbouw opzetten voor een regel code.
    """

    def test_de_database_image_gaat_langs_de_rewrite_voor_hij_gerenderd_wordt(self) -> None:
        bron = PROJECT_MANAGER.read_text()

        aanroep = bron.find("resolve_project_image(")
        assert aanroep != -1, "de database-image gaat niet langs resolve_project_image"

        render = bron.find('render_template(\n                "postgresql-cluster.yaml.jinja"')
        assert render != -1, "de render van het cluster-sjabloon is verplaatst; werk deze guard bij"
        assert aanroep < render, "de rewrite staat NA de render en bereikt het manifest dus niet"

    def test_de_uitkomst_gaat_ook_de_secretmap_in(self) -> None:
        """Een herschreven image zonder pull-secret faalt op een registry die authenticatie vraagt."""
        bron = PROJECT_MANAGER.read_text()
        assert re.search(r"image_pull_secrets_map\[resolved_db\.image\]\s*=\s*resolved_db\.secret", bron), (
            "het secret van de opgeloste database-image landt niet in imagePullSecretsMap"
        )
