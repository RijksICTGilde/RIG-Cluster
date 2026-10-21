"""De pins van de SOPS-sidecar en de back-upimage, en de taken die ze zetten.

Deze toetsen lezen de Taskfile en de manifesten, geen Python. Dat is met opzet: een pin die
naar `latest` of naar een naam wijst die niemand publiceert gaat stil fout, en meestal pas
bij de eerste podstart na een wijziging.

De guard op de manifesten zelf staat in tests/test_image_layer_guard.py
(``TestManifestImages``). Achtergrond: features/image-versioning-calver.md.
"""

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
TASKFILE = REPO_ROOT / "Taskfile.yaml"
DOCKER_IMAGES = REPO_ROOT / ".github/workflows/docker-images.yml"
CALVER_TAG = "scripts/calver-tag.sh"

#: De gekozen naam per image: die van de Taskfile, want alleen daar zijn deze twee images
#: ooit gepubliceerd. De verworpen naam is die van docker-images.yml.
GEKOZEN = {
    "rig-cmp-argo-kustomize-sops": "ghcr.io/minbzk/base-images/rig-cmp-argo-kustomize-sops",
    "rig-backup": "ghcr.io/minbzk/base-images/rig-backup",
}
VERWORPEN = {
    "rig-cmp-argo-kustomize-sops": "ghcr.io/rijksictgilde/zad/rig-cmp-argo-kustomize-sops",
    "rig-backup": "ghcr.io/rijksictgilde/zad/rig-backup",
}

#: De taken die het werk doen. De digeststap staat één keer, in _pin-image-digest.
PUBLICEER = {
    "rig-cmp-argo-kustomize-sops": "publish-cmp-kustomize-sops",
    "rig-backup": "publish-backup-image",
}
DIGESTSTAP = "_pin-image-digest"
PINTAKEN = ("pin-cmp-image", "pin-operations-manager-image")

#: De bestanden waar de sidecar in git staat: de component die de sandbox gebruikt, de
#: image-patch die op productie telt, en de ArgoCD-CR zelf.
CMP_BESTANDEN = (
    "bootstrap/rig-system/kustomize/components/sops-plugin/kustomization.yaml",
    "bootstrap/rig-system/kustomize/overlays/odcn-production/kustomization.yaml",
    "bootstrap/rig-system/kustomize/overlays/odcn-production/argocd-deployment.yaml",
)

PROEFDIGEST = "sha256:" + "a" * 64
PROEFTAG = "2026.10.18.1432-7ccad61d"


def _taken() -> dict[str, Any]:
    return yaml.safe_load(TASKFILE.read_text())["tasks"]


def _taak(naam: str) -> dict[str, Any]:
    taken = _taken()
    assert naam in taken, f"{naam} staat niet in de Taskfile"
    return taken[naam]


def _aangeroepen(taak: str, subtaak: str) -> dict[str, Any] | None:
    for cmd in _taak(taak)["cmds"]:
        if isinstance(cmd, dict) and cmd.get("task") == subtaak:
            return cmd
    return None


def _shell(*taken: str) -> str:
    """De regels die deze taken zelf in een shell draaien."""
    return "\n".join(cmd for taak in taken for cmd in _taak(taak)["cmds"] if isinstance(cmd, str))


def _pinbestanden(pintaak: str) -> list[str]:
    """De bestanden die een pintaak aan de digeststap meegeeft, met zijn vars ingevuld."""
    meegegeven = _aangeroepen(pintaak, DIGESTSTAP)["vars"]["PIN_FILES"]
    for naam, waarde in (_taak(pintaak).get("vars") or {}).items():
        meegegeven = meegegeven.replace(f"{{{{.{naam}}}}}", str(waarde))
    return meegegeven.split()


def _bronpaden(taak: str) -> list[str]:
    return _taak(taak)["vars"]["IMAGE_VERSION"]["sh"].split()[1:]


def _sedexpressie(image_name: str, image_tag: str, digest: str) -> str:
    """De sed uit _pin-image-digest, met zijn variabelen ingevuld."""
    regel = next(r for r in _shell(DIGESTSTAP).splitlines() if "sed -i.bak" in r)
    expressie = re.search(r'-E "(.+)" "\$BESTAND"', regel.strip()).group(1)
    return (
        expressie.replace("{{.IMAGE_NAME}}", image_name)
        .replace("{{.IMAGE_TAG}}", image_tag)
        .replace("${DIGEST}", digest)
        .replace("\\$", "$")
    )


class TestDePublicatietakenGevenEenCalVerTag:
    """Dezelfde vorm als publish-operations-manager en publish-keycloak-image."""

    @pytest.mark.parametrize("image", sorted(PUBLICEER))
    def test_de_tag_wordt_met_het_gedeelde_script_uitgerekend(self, image: str) -> None:
        """Een eigen variant zou stil afwijken van de tag die de andere images dragen."""
        berekening = _taak(PUBLICEER[image])["vars"]["IMAGE_VERSION"]["sh"].split()

        assert berekening[0] == CALVER_TAG
        assert berekening[1:], "zonder bronpaden meet het script niets"

    @pytest.mark.parametrize("image", sorted(PUBLICEER))
    def test_de_bronpaden_zijn_de_paden_van_dit_image(self, image: str) -> None:
        """Mist een pad, dan leest een vuile bron als schoon; staat er een te veel bij, dan
        is elke tag -dirty om een wijziging die niet in de image zit."""
        bouw = _aangeroepen(PUBLICEER[image], "docker-build-and-push")["vars"]
        dockerfile = f"{bouw['BUILD_CONTEXT'].rstrip('/')}/{bouw['DOCKERFILE_PATH']}".removeprefix("./")

        paden = _bronpaden(PUBLICEER[image])
        assert paden, image
        for pad in paden:
            assert (REPO_ROOT / pad).is_dir(), pad
        assert any(dockerfile.startswith(f"{pad}/") for pad in paden), (dockerfile, paden)

    @pytest.mark.parametrize("image", sorted(PUBLICEER))
    def test_een_dirty_tag_wordt_geweigerd(self, image: str) -> None:
        """Weigeren en niet waarschuwen: deze tag wordt in git gepind, en een pin naar
        ongecommitte bron is niet te herleiden en niet opnieuw te maken."""
        controles = _taak(PUBLICEER[image])["preconditions"]
        weigering = [c for c in controles if "-dirty" in c["sh"]]
        assert weigering, controles

        for controle in weigering:
            draai = subprocess.run(
                ["sh", "-c", controle["sh"].replace("{{.IMAGE_VERSION}}", f"{PROEFTAG}-dirty")],
                cwd=REPO_ROOT,
            )
            assert draai.returncode != 0, controle["sh"]
            schoon = subprocess.run(
                ["sh", "-c", controle["sh"].replace("{{.IMAGE_VERSION}}", PROEFTAG)],
                cwd=REPO_ROOT,
            )
            assert schoon.returncode == 0, controle["sh"]

    @pytest.mark.parametrize("image", sorted(PUBLICEER))
    def test_hij_pusht_de_calver_en_latest(self, image: str) -> None:
        """Eén bouw en één digest, dus `latest` kost niets en blijft werken voor een
        handmatige pull."""
        meegegeven = _aangeroepen(PUBLICEER[image], "docker-build-and-push")["vars"]

        assert meegegeven["IMAGE_TAG"] == "{{.IMAGE_VERSION}}"
        assert meegegeven["EXTRA_TAG"] == "latest"

    @pytest.mark.parametrize("image", sorted(PUBLICEER))
    def test_hij_pusht_naar_de_naam_die_gepind_wordt(self, image: str) -> None:
        """Lopen die twee uiteen, dan pullt de pod een image dat niemand bouwt."""
        meegegeven = _aangeroepen(PUBLICEER[image], "docker-build-and-push")["vars"]
        naam = f"{meegegeven['REGISTRY_IMAGE'].rstrip('/')}/{meegegeven['IMAGE_NAME']}"

        assert naam == GEKOZEN[image]

    def test_de_sidecar_wordt_na_de_bouw_gepind(self) -> None:
        """De pin is de belofte dat het artefact er is. go-task breekt de keten af bij een
        mislukt commando, dus de ORDE weerhoudt een mislukte bouw ervan alsnog te pinnen."""
        aangeroepen = [
            cmd["task"] for cmd in _taak(PUBLICEER["rig-cmp-argo-kustomize-sops"])["cmds"] if isinstance(cmd, dict)
        ]

        assert aangeroepen.index("docker-build-and-push") < aangeroepen.index("pin-cmp-image")

    def test_precies_de_gebouwde_tag_wordt_gepind(self) -> None:
        """Een tweede berekening ertussen zou een tag pinnen die niet gepusht is."""
        taak = PUBLICEER["rig-cmp-argo-kustomize-sops"]

        gebouwd = _aangeroepen(taak, "docker-build-and-push")["vars"]["IMAGE_TAG"]
        gepind = _aangeroepen(taak, "pin-cmp-image")["vars"]["IMAGE_TAG"]
        assert gebouwd == gepind == "{{.IMAGE_VERSION}}"


class TestEenNaamPerImage:
    """De Taskfile en docker-images.yml publiceerden onder verschillende namen."""

    @pytest.mark.parametrize("image", sorted(VERWORPEN))
    def test_de_verworpen_naam_wordt_nergens_gepind(self, image: str) -> None:
        gezocht = VERWORPEN[image]
        bronnen = [REPO_ROOT / "bootstrap", REPO_ROOT / "operations-manager/python/manifests"]

        treffers = [
            f"{pad.relative_to(REPO_ROOT)}"
            for wortel in bronnen
            for pad in wortel.rglob("*")
            if pad.is_file() and gezocht in pad.read_text(errors="ignore")
        ]
        assert treffers == []

    @pytest.mark.parametrize("image", sorted(VERWORPEN))
    def test_de_workflow_zegt_dat_zijn_naam_niet_gepind_wordt(self, image: str) -> None:
        """Hij blijft staan omdat GITHUB_TOKEN alleen in de eigen org mag pushen. Zonder die
        regel leest docker-images.yml als de bouwer van wat er draait."""
        inhoud = DOCKER_IMAGES.read_text()
        if VERWORPEN[image] not in inhoud:
            pytest.skip(f"{VERWORPEN[image]} staat niet meer in de workflow")

        regel = next(n for n, r in enumerate(inhoud.splitlines()) if VERWORPEN[image] in r)
        toelichting = "\n".join(inhoud.splitlines()[max(0, regel - 4) : regel])
        assert "niet gepind" in toelichting, toelichting


class TestDeDigestStapStaatEenKeer:
    """Plan 3a: één digeststap, door beide pintaken aangeroepen."""

    @pytest.mark.parametrize("pintaak", PINTAKEN)
    def test_de_pintaak_gebruikt_de_gedeelde_digeststap(self, pintaak: str) -> None:
        meegegeven = _aangeroepen(pintaak, DIGESTSTAP)
        assert meegegeven is not None, pintaak
        assert meegegeven["vars"]["IMAGE_TAG"] == "{{.IMAGE_TAG}}"

    @pytest.mark.parametrize("pintaak", PINTAKEN)
    def test_de_pintaak_zoekt_zelf_geen_digest_op(self, pintaak: str) -> None:
        """Een tweede kopie van die stap zou stil van de eerste afwijken."""
        eigen = _shell(pintaak)

        assert "imagetools" not in eigen, eigen
        assert "sed" not in eigen, eigen

    def test_de_digest_komt_uit_de_registry_en_niet_uit_een_lokale_bouw(self) -> None:
        """Gepind wordt wat er in de registry staat; een lokale bouw levert een andere digest."""
        eigen = _shell(DIGESTSTAP)

        assert "docker buildx imagetools inspect {{.REPO}}:{{.IMAGE_TAG}}" in eigen
        assert "docker buildx build" not in eigen
        assert "docker build -" not in eigen

    def test_zonder_digest_schrijft_hij_niets(self) -> None:
        """Een lege inspect-uitvoer zou anders `@` zonder digest in de manifesten zetten."""
        eigen = _shell(DIGESTSTAP)

        assert "sha256:*)" in eigen
        assert "exit 1" in eigen

    @pytest.mark.parametrize("pintaak", PINTAKEN)
    def test_de_pintaak_noemt_bestanden_die_bestaan(self, pintaak: str) -> None:
        bestanden = _pinbestanden(pintaak)
        assert bestanden, pintaak

        for bestand in bestanden:
            assert (REPO_ROOT / bestand.strip()).is_file(), bestand

    def test_de_sidecar_staat_in_alle_drie_de_bestanden(self) -> None:
        """Pint hij er een niet, dan draait de sandbox of productie iets anders dan bedoeld."""
        assert sorted(_pinbestanden("pin-cmp-image")) == sorted(CMP_BESTANDEN)


class TestDeSedRaaktElkeVerwijzing:
    """De expressie zelf, gedraaid in plaats van gelezen: hij doet stil niets als hij mist."""

    @pytest.mark.parametrize("bestand", CMP_BESTANDEN)
    def test_hij_zet_tag_en_digest_en_houdt_het_registrypad(self, bestand: str, tmp_path: Path) -> None:
        kopie = tmp_path / Path(bestand).name
        shutil.copy(REPO_ROOT / bestand, kopie)
        voor = [r.strip() for r in kopie.read_text().splitlines() if "rig-cmp-argo-kustomize-sops" in r and ":" in r]

        expressie = _sedexpressie("rig-cmp-argo-kustomize-sops", PROEFTAG, PROEFDIGEST)
        subprocess.run(["sed", "-i", "-E", expressie, str(kopie)], check=True)

        na = [r.strip() for r in kopie.read_text().splitlines() if "rig-cmp-argo-kustomize-sops" in r]
        # Elke verwijzing, niet alleen de eerste: de component en de odcn-overlay noemen de
        # sidecar twee keer, voor cmp-server en voor cmp-platform.
        assert [r for r in na if r.endswith(f":{PROEFTAG}@{PROEFDIGEST}")] == na
        # Het voorvoegsel blijft: de sandbox houdt ghcr.io, productie de rcr-spiegel.
        assert [r.split()[1].rsplit(":", 2)[0] for r in na] == [
            r.split()[1].split("@")[0].rsplit(":", 1)[0] for r in voor
        ]

    def test_hij_laat_een_ander_image_in_hetzelfde_bestand_staan(self, tmp_path: Path) -> None:
        """De ArgoCD-CR draagt ook argocd-rig; die hoort deze pin niet mee te krijgen."""
        bestand = "bootstrap/rig-system/kustomize/overlays/odcn-production/argocd-deployment.yaml"
        kopie = tmp_path / "argocd-deployment.yaml"
        shutil.copy(REPO_ROOT / bestand, kopie)

        expressie = _sedexpressie("rig-cmp-argo-kustomize-sops", PROEFTAG, PROEFDIGEST)
        subprocess.run(["sed", "-i", "-E", expressie, str(kopie)], check=True)

        assert "image: rcr.rijksapps.nl/ghcr-rig/minbzk/base-images/argocd-rig\n" in kopie.read_text()

    def test_hij_vervangt_een_bestaande_digest_en_stapelt_niet(self, tmp_path: Path) -> None:
        """Twee digests achter elkaar is geen verwijzing die een kubelet kan pullen."""
        kopie = tmp_path / "component.yaml"
        shutil.copy(REPO_ROOT / CMP_BESTANDEN[0], kopie)

        expressie = _sedexpressie("rig-cmp-argo-kustomize-sops", PROEFTAG, PROEFDIGEST)
        for _ in range(2):
            subprocess.run(["sed", "-i", "-E", expressie, str(kopie)], check=True)

        verwijzingen = [r for r in kopie.read_text().splitlines() if "rig-cmp-argo-kustomize-sops" in r]
        assert [r.count("@sha256:") for r in verwijzingen] == [1] * len(verwijzingen)
