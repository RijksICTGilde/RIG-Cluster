"""De manifestpas: elke image in een podspec langs dezelfde regels."""

from __future__ import annotations

from typing import Any

import pytest
import yaml
from opi.services.catalog.image_registries.manifest_pass import (
    apply_rules,
    apply_rules_to_directory,
    apply_rules_to_document,
    pod_spec_of,
)
from opi.services.catalog.image_registries.resolution import cluster_rules
from opi.services.catalog.image_registries.rules import RegistryRule

ODCN = "odcn-production"
SANDBOX = "sandboxed-local"

GHCR = RegistryRule(match="ghcr.io", to="rcr.rijksapps.nl/ghcr-rig", secret="ghcr-rig-robot-pull-secret")
QUAY = RegistryRule(match="quay.io", to="rcr.rijksapps.nl/quay-rig", secret="quay-rig-robot-pull-secret")


def _deployment(*images: str) -> dict[str, Any]:
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "web"},
        "spec": {"template": {"spec": {"containers": [{"name": f"c{i}", "image": im} for i, im in enumerate(images)]}}},
    }


class TestWaarDePodspecZit:
    @pytest.mark.parametrize(
        ("manifest", "gevonden"),
        [
            ({"kind": "Pod", "spec": {"containers": []}}, True),
            ({"kind": "Deployment", "spec": {"template": {"spec": {"containers": []}}}}, True),
            ({"kind": "StatefulSet", "spec": {"template": {"spec": {"containers": []}}}}, True),
            ({"kind": "CronJob", "spec": {"jobTemplate": {"spec": {"template": {"spec": {}}}}}}, True),
            ({"kind": "Service", "spec": {"ports": []}}, False),
            ({"kind": "Secret"}, False),
        ],
    )
    def test_per_kind(self, manifest: dict[str, Any], gevonden: bool) -> None:
        assert (pod_spec_of(manifest) is not None) is gevonden


class TestDePasZelf:
    def test_herschrijft_en_hangt_het_secret_eronder(self) -> None:
        manifest = apply_rules(_deployment("ghcr.io/org/web:1"), [GHCR])
        spec = manifest["spec"]["template"]["spec"]
        assert spec["containers"][0]["image"] == "rcr.rijksapps.nl/ghcr-rig/org/web:1"
        assert spec["imagePullSecrets"] == [{"name": "ghcr-rig-robot-pull-secret"}]

    def test_een_sidecar_uit_zijn_sjabloon_wordt_ook_geraakt(self) -> None:
        """De auth-wall proxy staat als vaste waarde in sidecar-authorization-wall.yaml.jinja
        en komt nooit langs de componentlus."""
        manifest = apply_rules(
            _deployment("ghcr.io/org/web:1", "quay.io/oauth2-proxy/oauth2-proxy:v7.7.1"), [GHCR, QUAY]
        )
        spec = manifest["spec"]["template"]["spec"]
        assert spec["containers"][1]["image"] == "rcr.rijksapps.nl/quay-rig/oauth2-proxy/oauth2-proxy:v7.7.1"
        assert {s["name"] for s in spec["imagePullSecrets"]} == {
            "ghcr-rig-robot-pull-secret",
            "quay-rig-robot-pull-secret",
        }

    def test_init_containers_tellen_mee(self) -> None:
        manifest = _deployment("ghcr.io/org/web:1")
        manifest["spec"]["template"]["spec"]["initContainers"] = [{"name": "init", "image": "quay.io/x/y:1"}]
        spec = apply_rules(manifest, [GHCR, QUAY])["spec"]["template"]["spec"]
        assert spec["initContainers"][0]["image"] == "rcr.rijksapps.nl/quay-rig/x/y:1"

    def test_een_bestaand_pull_secret_blijft_staan(self) -> None:
        manifest = _deployment("ghcr.io/org/web:1")
        manifest["spec"]["template"]["spec"]["imagePullSecrets"] = [{"name": "eigen-secret"}]
        spec = apply_rules(manifest, [GHCR])["spec"]["template"]["spec"]
        assert [s["name"] for s in spec["imagePullSecrets"]] == ["eigen-secret", "ghcr-rig-robot-pull-secret"]

    def test_hetzelfde_secret_komt_er_niet_twee_keer_in(self) -> None:
        spec = apply_rules(_deployment("ghcr.io/a:1", "ghcr.io/b:1"), [GHCR])["spec"]["template"]["spec"]
        assert spec["imagePullSecrets"] == [{"name": "ghcr-rig-robot-pull-secret"}]

    def test_de_pas_is_idempotent(self) -> None:
        """Wat de componentlus al oploste staat al op zijn bestemming; een tweede keer
        draaien mag daar niets meer aan veranderen."""
        eenmaal = apply_rules(_deployment("ghcr.io/org/web:1"), [GHCR])
        tweemaal = apply_rules(apply_rules(_deployment("ghcr.io/org/web:1"), [GHCR]), [GHCR])
        assert eenmaal == tweemaal

    def test_zonder_regels_gebeurt_er_niets(self) -> None:
        origineel = _deployment("nginx:alpine")
        assert apply_rules(_deployment("nginx:alpine"), []) == origineel

    def test_een_manifest_zonder_podspec_blijft_ongemoeid(self) -> None:
        service = {"kind": "Service", "spec": {"ports": [{"port": 80}]}}
        assert apply_rules(dict(service), [GHCR]) == service


class TestOverEenMap:
    def test_alleen_gewone_manifesten(self, tmp_path: Any) -> None:
        (tmp_path / "deployment.yaml").write_text(yaml.dump(_deployment("ghcr.io/org/web:1")))
        (tmp_path / "secret.to-sops.yaml").write_text(yaml.dump({"kind": "Secret", "stringData": {"a": "b"}}))
        (tmp_path / "kustomization.yaml").write_text(yaml.dump({"resources": []}))
        origineel_secret = (tmp_path / "secret.to-sops.yaml").read_text()

        apply_rules_to_directory(str(tmp_path), [GHCR])

        geschreven = yaml.safe_load((tmp_path / "deployment.yaml").read_text())
        assert geschreven["spec"]["template"]["spec"]["containers"][0]["image"].startswith("rcr.rijksapps.nl")
        assert (tmp_path / "secret.to-sops.yaml").read_text() == origineel_secret

    @pytest.mark.parametrize("suffix", [".to-sops.yaml", ".sops.yaml"])
    def test_een_sops_bestand_blijft_ongemoeid(self, tmp_path: Any, suffix: str) -> None:
        """Herschrijven breekt de MAC van de versleutelde helft, dus KSOPS leest hem niet meer.
        Vandaar inhoud die de pas WEL zou aanpakken."""
        pad = tmp_path / f"web{suffix}"
        pad.write_text(yaml.dump(_deployment("ghcr.io/org/web:1")))
        origineel = pad.read_text()

        apply_rules_to_directory(str(tmp_path), [GHCR])

        assert pad.read_text() == origineel

    def test_een_bestand_zonder_wijziging_wordt_niet_herschreven(self, tmp_path: Any) -> None:
        """Anders herversleutelt SOPS zich suf op precies de churn die eerder is opgeruimd."""
        path = tmp_path / "service.yaml"
        path.write_text("kind: Service\nspec: {}\n")
        voor = path.stat().st_mtime_ns
        apply_rules_to_directory(str(tmp_path), [GHCR])
        assert path.stat().st_mtime_ns == voor

    def test_een_meerregelig_command_blijft_een_literal_block(self, tmp_path: Any) -> None:
        """De pas schrijft via de canonieke schrijver, dus een blok blijft een blok.

        Met een eigen PyYAML-dump werd elk aangeraakt manifest volledig opnieuw
        geserialiseerd: een heredoc kwam terug als een regel vol ``\\n``-escapes en het
        commentaar uit het sjabloon verdween. ``opi/utils/yaml_util.py`` is de enige
        schrijver, en die kiest de literal block style zodat elk schrijfpad hem erft.
        """
        path = tmp_path / "deployment.yaml"
        path.write_text(
            "# De sidecar draait op een vaste image uit zijn sjabloon.\n"
            "apiVersion: apps/v1\n"
            "kind: Deployment\n"
            "spec:\n"
            "  template:\n"
            "    spec:\n"
            "      containers:\n"
            "        - name: app\n"
            "          image: ghcr.io/org/web:1\n"
            "          command:\n"
            "            - /bin/sh\n"
            "            - -c\n"
            "            - |\n"
            "              cat <<EOF > /etc/haproxy.cfg\n"
            "              frontend in\n"
            "              EOF\n"
        )

        apply_rules_to_directory(str(tmp_path), [GHCR])

        geschreven = path.read_text()
        assert "image: rcr.rijksapps.nl/ghcr-rig/org/web:1" in geschreven
        assert "            - |\n" in geschreven, geschreven
        assert "\\n" not in geschreven, geschreven
        assert geschreven.startswith("# De sidecar draait op een vaste image uit zijn sjabloon.\n")
        spec = yaml.safe_load(geschreven)["spec"]["template"]["spec"]
        assert spec["containers"][0]["command"][2] == "cat <<EOF > /etc/haproxy.cfg\nfrontend in\nEOF\n"
        assert spec["imagePullSecrets"] == [{"name": "ghcr-rig-robot-pull-secret"}]

    def test_een_multidocument_bestand_telt_helemaal_mee(self, tmp_path: Any) -> None:
        """Eerder viel zo'n bestand stil weg met alleen een waarschuwing."""
        path = tmp_path / "bundle.yaml"
        path.write_text(
            yaml.dump(_deployment("ghcr.io/org/web:1"))
            + "---\n"
            + yaml.dump({"kind": "Pod", "spec": {"containers": [{"name": "side", "image": "quay.io/x/y:1"}]}})
        )

        apply_rules_to_directory(str(tmp_path), [GHCR, QUAY])

        documenten = list(yaml.safe_load_all(path.read_text()))
        assert len(documenten) == 2
        assert documenten[0]["spec"]["template"]["spec"]["containers"][0]["image"] == (
            "rcr.rijksapps.nl/ghcr-rig/org/web:1"
        )
        assert documenten[1]["spec"]["containers"][0]["image"] == "rcr.rijksapps.nl/quay-rig/x/y:1"


class TestOverEenLosDocument:
    def test_een_kale_pod_krijgt_zijn_secret(self) -> None:
        document = yaml.safe_dump(
            {"kind": "Pod", "spec": {"containers": [{"name": "backup", "image": "ghcr.io/minbzk/rig-backup:latest"}]}}
        )
        resultaat = yaml.safe_load(apply_rules_to_document(document, [GHCR]))
        assert resultaat["spec"]["containers"][0]["image"] == "rcr.rijksapps.nl/ghcr-rig/minbzk/rig-backup:latest"
        assert resultaat["spec"]["imagePullSecrets"] == [{"name": "ghcr-rig-robot-pull-secret"}]

    def test_zonder_regels_komt_de_tekst_ongewijzigd_terug(self) -> None:
        document = "kind: Pod\nspec:\n  containers: []\n"
        assert apply_rules_to_document(document, []) == document

    def test_zonder_iets_op_te_lossen_komt_de_tekst_letterlijk_terug(self) -> None:
        """Niets te doen betekent ook niet opnieuw serialiseren."""
        document = "# een sjabloon met commentaar\nkind: Service\nspec: {}\n"
        assert apply_rules_to_document(document, [GHCR]) == document

    def test_alle_documenten_van_een_bundel_tellen_mee(self) -> None:
        document = (
            yaml.safe_dump({"kind": "Pod", "spec": {"containers": [{"name": "a", "image": "ghcr.io/org/a:1"}]}})
            + "---\n"
            + yaml.safe_dump({"kind": "Pod", "spec": {"containers": [{"name": "b", "image": "quay.io/org/b:1"}]}})
        )
        documenten = list(yaml.safe_load_all(apply_rules_to_document(document, [GHCR, QUAY])))
        assert [d["spec"]["containers"][0]["image"] for d in documenten] == [
            "rcr.rijksapps.nl/ghcr-rig/org/a:1",
            "rcr.rijksapps.nl/quay-rig/org/b:1",
        ]


class TestDeClustertabelIsDeBron:
    """Wat de extensie in extensions/odcn-registry-rewrite.yaml droeg, staat nu in de
    clusterconfig van de dienst: een eigenaar voor 'welke registry, welk secret'."""

    def test_odcn_draagt_de_gedeelde_proxies(self) -> None:
        matches = {rule.match for rule in cluster_rules(ODCN)}
        assert matches == {
            "ghcr.io",
            "docker.io",
            "registry.gitlab.com",
            "gcr.io",
            "quay.io",
            "registry.k8s.io",
            "code.overheid.nl",
        }

    def test_elke_regel_noemt_zijn_secret(self) -> None:
        assert all(rule.secret and rule.to.startswith("rcr.rijksapps.nl/") for rule in cluster_rules(ODCN))

    def test_een_cluster_zonder_tabel_doet_niets(self) -> None:
        assert cluster_rules(SANDBOX) == []


class TestGelijkAanWatDeExtensieDeed:
    """De meting die het plan vraagt: is het resultaat voor en na hetzelfde?

    De extensie is weg, dus "vergelijken met de extensie" kan niet meer als code. Wat wel
    kan is zijn UITKOMSTEN vastpinnen: dit zijn de gevallen die ``tests/test_extensions.py``
    mat, met de tabel die ``extensions/odcn-registry-rewrite.yaml`` droeg. Zolang deze
    gelijkheden staan is er aan de gegenereerde manifesten niets veranderd behalve wat er
    bewust bij is gekomen (de normalisatie van korte namen, en het secret bij een image die
    al op zijn bestemming staat).
    """

    @staticmethod
    def _pod(image: str, pull_secrets: list[dict[str, str]] | None = None) -> dict[str, Any]:
        spec: dict[str, Any] = {"containers": [{"name": "app", "image": image}]}
        if pull_secrets is not None:
            spec["imagePullSecrets"] = pull_secrets
        return {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "t"}, "spec": spec}

    @pytest.mark.parametrize(
        ("image", "verwacht", "secret"),
        [
            ("ghcr.io/org/app:latest", "rcr.rijksapps.nl/ghcr-rig/org/app:latest", "ghcr-rig-robot-pull-secret"),
            (
                "ghcr.io/minbzk/base-images/rig-backup:latest",
                "rcr.rijksapps.nl/ghcr-rig/minbzk/base-images/rig-backup:latest",
                "ghcr-rig-robot-pull-secret",
            ),
            (
                "quay.io/oauth2-proxy/oauth2-proxy:v7.7.1",
                "rcr.rijksapps.nl/quay-rig/oauth2-proxy/oauth2-proxy:v7.7.1",
                "quay-rig-robot-pull-secret",
            ),
            (
                "registry.k8s.io/pause:3.9",
                "rcr.rijksapps.nl/k8s-rig/pause:3.9",
                "k8s-rig-robot-pull-secret",
            ),
            (
                "code.overheid.nl/team/app:1",
                "rcr.rijksapps.nl/code-overheid-rig/team/app:1",
                "code-overheid-rig-robot-pull-secret",
            ),
        ],
    )
    def test_dezelfde_uitkomst_als_de_oude_tabel(self, image: str, verwacht: str, secret: str) -> None:
        spec = apply_rules(self._pod(image), cluster_rules(ODCN))["spec"]
        assert spec["containers"][0]["image"] == verwacht
        assert spec["imagePullSecrets"] == [{"name": secret}]

    def test_een_image_buiten_de_tabel_blijft_onaangeroerd(self) -> None:
        manifest = apply_rules(self._pod("registry.intern.nl/org/app:v1"), cluster_rules(ODCN))
        assert manifest["spec"]["containers"][0]["image"] == "registry.intern.nl/org/app:v1"
        assert "imagePullSecrets" not in manifest["spec"]

    def test_een_bestaand_secret_wordt_niet_verdubbeld(self) -> None:
        spec = apply_rules(
            self._pod("ghcr.io/org/app:1", [{"name": "ghcr-rig-robot-pull-secret"}]), cluster_rules(ODCN)
        )["spec"]
        assert spec["imagePullSecrets"] == [{"name": "ghcr-rig-robot-pull-secret"}]

    def test_wat_er_WEL_bij_is_gekomen(self) -> None:
        """De twee bewuste verschillen, zodat ze niet als regressie kunnen worden gelezen.

        De oude tabel matchte op ``docker.io`` en liet ``nginx:alpine`` dus lopen, terwijl
        de admission-webhook van ODCN hem wel zo behandelt. En een image die al op zijn
        bestemming staat kreeg geen secret, wat de dp-bn7-storing was.
        """
        korte_naam = apply_rules(self._pod("nginx:alpine"), cluster_rules(ODCN))["spec"]
        assert korte_naam["containers"][0]["image"] == "rcr.rijksapps.nl/dockerhub-rig/library/nginx:alpine"

        al_op_bestemming = apply_rules(self._pod("rcr.rijksapps.nl/ghcr-rig/org/app:1"), cluster_rules(ODCN))["spec"]
        assert al_op_bestemming["containers"][0]["image"] == "rcr.rijksapps.nl/ghcr-rig/org/app:1"
        assert al_op_bestemming["imagePullSecrets"] == [{"name": "ghcr-rig-robot-pull-secret"}]
