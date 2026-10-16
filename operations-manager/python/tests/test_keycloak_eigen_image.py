"""De eigen Keycloak-image, en de pin die per omgeving staat.

Deze toetsen lezen MANIFESTEN, een Dockerfile en taken uit de Taskfile, geen Python. Dat is
met opzet: wat hier fout kan gaan, gaat stil fout, en meestal pas bij de eerste podstart na
een wijziging.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]

DOCKERFILE = REPO_ROOT / "images/keycloak-zad/Dockerfile"
KEYCLOAK = REPO_ROOT / "infrastructure/bootstrap/infrastructure/keycloak/controller"
MODULES = REPO_ROOT / "keycloak-migration"
BRON_TAG = REPO_ROOT / "scripts/bron-tag.sh"
CALVER_TAG = REPO_ROOT / "scripts/calver-tag.sh"
TASKFILE = REPO_ROOT / "Taskfile.yaml"
CI_WORKFLOW = REPO_ROOT / ".github/workflows/ci.yml"
DOCKER_IMAGES = REPO_ROOT / ".github/workflows/docker-images.yml"

IMAGE_NAAM = "keycloak-zad"
IMAGE_REPO = f"ghcr.io/rijksictgilde/zad/{IMAGE_NAAM}"

SANDBOX_OVERLAY = KEYCLOAK / "overlays/sandboxed-local/kustomization.yaml"

#: De twee taken die het werk doen. Ze staan in de Taskfile en niet in een eigen script,
#: zodat een CI-job later dezelfde taak kan aanroepen. Promoveren is geen derde taak: dat is
#: dezelfde pin, met de basis als doel.
PUBLICEER = "publish-keycloak-image"
PIN = "pin-keycloak-image"

#: Hoe ODCN een ghcr-pad spiegelt. Dezelfde vorm als de argocd- en de postgres-overlay.
RCR_VOORVOEGSEL = "rcr.rijksapps.nl/ghcr-rig/"

#: De enige tagvorm die publish-keycloak-image pusht en pint: de CalVer van de Taskfile,
#: dezelfde die de operations-manager draagt, bij ongecommit werk met `-dirty` erachter.
GEPUSHTE_TAG = re.compile(r"\d{4}\.\d{2}\.\d{2}\.\d{4}-[0-9a-f]{8,}(-dirty)?")


#: De bouwopties van Keycloak 25, zoals `kc.sh build --help` ze opsomt (gemeten in RC-212).
#: Ze horen in de Dockerfile: als startargument weigert een pod met --optimized te starten,
#: ook bij een gelijke waarde, en als omgevingsvariabele doen ze niets.
BOUWOPTIES = (
    "db",
    "db-driver",
    "features",
    "features-disabled",
    "fips-mode",
    "health-enabled",
    "http-management-relative-path",
    "http-relative-path",
    "https-client-auth",
    "https-management-client-auth",
    "legacy-observability-interface",
    "metrics-enabled",
    "transaction-xa-enabled",
    "vault",
)

#: Wat deze image inbakt. Deze vier staan na `kc.sh build` in
#: META-INF/keycloak-persisted.properties in de image; een spi-optie staat daar niet
#: (gemeten in RC-212), en die moet dus bij de start blijven staan.
INGEBAKKEN = ("db=postgres", "health-enabled=true", "metrics-enabled=true", "features=admin-api")

#: Runtime-opties die RC-212 uit de args haalde omdat de env ze al zette, met de waarde die
#: daar geldt. Zo'n verhuizing is pas gepind als die WAARDE erbij staat: `forwarded` in plaats
#: van `xforwarded` zet de val van 48c2dbbb terug zonder dat er een vlag bij komt. Waarom
#: `xforwarded` de juiste is, staat bij de env in base/deployment.yaml.
UIT_DE_ARGS_GEHAALD = (("proxy-headers", "xforwarded"), ("hostname-strict", "false"))


def _dockerfile() -> str:
    return DOCKERFILE.read_text()


def _eindfase() -> str:
    """De Dockerfile vanaf de laatste FROM: alleen wat daar staat komt in de image terecht."""
    starts = [m.start() for m in re.finditer(r"^FROM ", _dockerfile(), re.MULTILINE)]
    assert len(starts) > 1, "de Dockerfile heeft geen aparte bouwfase"
    return _dockerfile()[starts[-1] :]


def _overgenomen_uit_de_bouw() -> dict[str, str]:
    """Wat de eindfase uit een eerdere fase overneemt: bronpad -> doelpad."""
    return dict(re.findall(r"^COPY --from=\S+ (\S+) (\S+)", _eindfase(), re.MULTILINE))


def _bouwfase_namen() -> set[str]:
    """De fasen waaruit de eindfase overneemt."""
    namen = set(re.findall(r"^COPY --from=(\S+) ", _eindfase(), re.MULTILINE))
    assert namen, "de eindfase neemt uit geen enkele fase over"
    return namen


def _thema_jar() -> str:
    """De naam waaronder de Dockerfile het thema van MinBZK wegschrijft."""
    match = re.search(r"\bcurl .*?-o (\S+)", _dockerfile())
    assert match is not None, "de Dockerfile haalt het thema niet op"
    return match.group(1)


def _basis_container() -> dict:
    return _basis_deployment()["spec"]["template"]["spec"]["containers"][0]


def _env_namen(container: dict) -> list[str]:
    return [e["name"] for e in container["env"]]


def _omgevingsnaam(optie: str) -> str:
    """De omgevingsvorm van een Keycloak-optie: `db-driver` wordt `KC_DB_DRIVER`."""
    return "KC_" + optie.upper().replace("-", "_")


def _kc_build_vlaggen() -> list[str]:
    """De vlaggen van de `kc.sh build`-regel in de Dockerfile, zonder de twee streepjes."""
    tekst = _dockerfile()
    # De RUN-regel en niet de eerste vermelding: het commentaar erboven noemt `kc.sh build` ook.
    start = re.search(r"^RUN .*kc\.sh build", tekst, re.MULTILINE)
    assert start is not None, "de Dockerfile draagt geen RUN met kc.sh build"
    regels = []
    for regel in tekst[start.start() :].splitlines():
        regels.append(regel)
        if not regel.rstrip().endswith("\\"):
            break
    vlaggen = re.findall(r"--(\S+)", "\n".join(regels))
    assert vlaggen, "de kc.sh build-regel in de Dockerfile draagt geen vlaggen"
    return vlaggen


def _basis_image() -> str:
    return _basis_deployment()["spec"]["template"]["spec"]["containers"][0]["image"]


def _basis_deployment() -> dict:
    return yaml.safe_load((KEYCLOAK / "base/deployment.yaml").read_text())


def _overlay(naam: str) -> dict:
    return yaml.safe_load((KEYCLOAK / "overlays" / naam / "kustomization.yaml").read_text())


def _image_entry(overlay: dict) -> dict | None:
    return next((i for i in overlay.get("images", []) if i["name"] == IMAGE_REPO), None)


def _module_namen() -> list[str]:
    return sorted(p.name for p in MODULES.iterdir() if (p / "pom.xml").is_file())


def _docker_images_job() -> dict:
    return yaml.safe_load(DOCKER_IMAGES.read_text())["jobs"][IMAGE_NAAM]


class TestDeImageIsHetArtefact:
    """De jars komen uit de bron in deze repo, dus een jar kan niet losraken van zijn code."""

    def test_de_pod_draait_onze_eigen_image(self) -> None:
        assert _basis_image().startswith(f"{IMAGE_REPO}:")

    def test_allebei_de_modules_worden_gebouwd(self) -> None:
        """De modulenamen komen uit de mappen, niet uit een lijst hier.

        Komt er een module bij zonder dat de Dockerfile hem bouwt, dan valt deze toets om
        in plaats van de eerste pod die zijn provider mist.
        """
        modules = sorted(p.name for p in MODULES.iterdir() if (p / "pom.xml").is_file())
        assert modules, "geen Java-module gevonden onder keycloak-migration/"

        for naam in modules:
            assert f"COPY keycloak-migration/{naam} " in _dockerfile(), naam
            assert f"-f {naam}/pom.xml clean package" in _dockerfile(), naam

    def test_de_basisversie_is_die_waar_de_modules_tegen_bouwen(self) -> None:
        """Lopen ze uiteen, dan draaien de providers tegen een andere Keycloak dan ze kennen.

        De SPI's die ze implementeren zijn intern en mogen zonder aankondiging veranderen
        (zie features/keycloak-26-upgrade.md), dus dat gaat niet vanzelf goed.
        """
        match = re.search(r"^FROM quay\.io/keycloak/keycloak:(\S+)", _dockerfile(), re.MULTILINE)
        assert match is not None, "de Dockerfile noemt geen Keycloak-basisimage"
        basis = match.group(1)

        for pom in sorted(MODULES.glob("*/pom.xml")):
            versie = re.search(r"<keycloak\.version>([^<]+)</keycloak\.version>", pom.read_text())
            assert versie is not None, pom
            assert versie.group(1) == basis, f"{pom.relative_to(REPO_ROOT)} bouwt tegen {versie.group(1)}"

    def test_het_thema_wordt_op_checksum_gecontroleerd(self) -> None:
        """Het thema blijft een externe download, maar tijdens de BOUW.

        Zonder controle is een gewijzigd of half binnengehaald bestand pas zichtbaar op het
        inlogscherm van een draaiend cluster. Waarom de som uitgeschreven staat en niet in een
        ARG: zie de Dockerfile.
        """
        tekst = _dockerfile()
        assert "keycloak-nl-design-system.jar" in tekst
        assert "sha256sum -c" in tekst
        assert re.search(r'"[0-9a-f]{64}  keycloak-nl-design-system\.jar"', tekst), tekst

    def test_de_providers_worden_ingebakken(self) -> None:
        """Zonder deze stap doet de pod de augmentatie bij elke start alsnog (gemeten)."""
        assert "kc.sh build" in _dockerfile()

    def test_elke_jar_belandt_in_de_providersmap(self) -> None:
        """Bouwen is niet hetzelfde als meeleveren: de eindfase moet ze ook overnemen.

        Alleen de verzender heeft een canarie, en die dekt zijn eigen jar: landt die ergens
        anders, dan lost het provider-id niet op en faalt `kc.sh build`. Voor de andere twee
        bouwt de image gewoon door: de SAML-mapper is dan stil weg, en het inlogscherm draait
        zonder nl-design-system.
        """
        overgenomen = _overgenomen_uit_de_bouw()
        assert overgenomen, "de eindfase neemt niets uit de bouwfase over"
        for bron, doel in overgenomen.items():
            assert doel.rstrip("/") == "/opt/keycloak/providers", (bron, doel)

        bronnen = " ".join(overgenomen)
        for naam in _module_namen():
            assert f"/{naam}/target/" in bronnen, naam
        assert f"/{_thema_jar()}" in bronnen, _thema_jar()

    def test_de_bouwfase_draait_niet_per_doelarchitectuur(self) -> None:
        """De jars zijn architectuuronafhankelijk, de image gaat naar amd64 en arm64.

        Zonder BUILDPLATFORM draait deze fase voor allebei, de tweede keer onder emulatie, en
        dat is alleen aan de duur van de bouw te merken.
        """
        for naam in _bouwfase_namen():
            fase = re.search(rf"^FROM (.*) AS {re.escape(naam)}$", _dockerfile(), re.MULTILINE)
            assert fase is not None, f"geen bouwfase {naam} in de Dockerfile"
            assert "--platform=$BUILDPLATFORM" in fase.group(1), (naam, fase.group(1))


class TestNietsWordtBijHetStartenOpgehaald:
    """Aan een van de providers hangt een startvlag die een stille terugval onmogelijk maakt."""

    def test_geen_enkel_manifest_draagt_nog_een_initcontainer(self) -> None:
        for pad in sorted(KEYCLOAK.rglob("*.yaml")):
            zonder_commentaar = "\n".join(
                regel for regel in pad.read_text().splitlines() if not regel.lstrip().startswith("#")
            )
            assert "initContainers" not in zonder_commentaar, pad

    def test_er_is_geen_provider_configmap_meer(self) -> None:
        for pad in sorted(KEYCLOAK.rglob("kustomization.yaml")):
            assert "configMapGenerator" not in yaml.safe_load(pad.read_text()), pad

    def test_niets_mount_over_de_providersmap_heen(self) -> None:
        """DE STILLE VAL. Een volume op dat pad verbergt precies de jars uit de image.

        Er stond er een: het emptyDir waar de initContainer in kopieerde.
        """
        container = _basis_deployment()["spec"]["template"]["spec"]["containers"][0]
        paden = [m["mountPath"].rstrip("/") for m in container.get("volumeMounts", [])]
        assert "/opt/keycloak/providers" not in paden

        for pad in sorted((KEYCLOAK / "overlays").rglob("*.yaml")):
            assert "/opt/keycloak/providers" not in pad.read_text(), pad


class TestDePodBouwtDeImageNietOpnieuwOm:
    """De augmentatie hoort in de imagebouw, en daar alleen.

    De eerste ronde zette `kc.sh build` in de Dockerfile en liet de bouwopties ook bij de
    deployment staan. Gemeten op de sandbox deed de pod het werk toen alsnog: "Changes detected
    in configuration" en ruim negen seconden augmentatie bij ELKE start, precies wat die bouw
    moest voorkomen.
    """

    def test_de_pod_start_met_optimized(self) -> None:
        """Zonder die vlag vergelijkt Keycloak zelf en herbouwt hij stil bij elk verschil."""
        args = _basis_container()["args"]
        assert args[0] == "start", args
        assert "--optimized" in args, args

    def test_geen_bouwoptie_staat_bij_de_start(self) -> None:
        """Met --optimized WEIGERT Keycloak daarop te starten, ook bij een gelijke waarde.

        Gemeten: "Build time option: '--db' not usable with pre-built image and --optimized".
        """
        args = _basis_container()["args"]
        for optie in BOUWOPTIES:
            assert not any(a == f"--{optie}" or a.startswith(f"--{optie}=") for a in args), optie

    def test_geen_bouwoptie_staat_in_de_omgeving(self) -> None:
        """Daar is hij stil: met --optimized waarschuwt Keycloak en doet hij niets.

        Zo bleef KC_SPI_ADMIN_REALM_RESTAPI_EXTENSION_ENABLED wel staan: een spi-optie wordt
        niet ingebakken, dus die MOET bij de pod staan om iets te doen.
        """
        namen = _env_namen(_basis_container())
        for optie in BOUWOPTIES:
            assert _omgevingsnaam(optie) not in namen, optie

    def test_de_ingebakken_opties_staan_in_de_dockerfile(self) -> None:
        """Ze staan nergens anders meer, dus valt de image om zonder deze regels."""
        vlaggen = _kc_build_vlaggen()
        for optie in INGEBAKKEN:
            assert optie in vlaggen, (optie, vlaggen)

    def test_geen_startargument_staat_ook_in_de_omgeving(self) -> None:
        """Twee plekken voor een optie: dan beslist de precedentie, en niet de lezer.

        Zo won `--proxy-headers=forwarded` bij de args tien maanden van de
        KC_PROXY_HEADERS=xforwarded die als fix bedoeld was (48c2dbbb).
        """
        container = _basis_container()
        namen = _env_namen(container)
        for arg in container["args"]:
            if not arg.startswith("--"):
                continue
            naam = _omgevingsnaam(arg[2:].split("=")[0])
            assert naam not in namen, f"{arg} staat ook als {naam}"

    def test_proxy_headers_staat_op_precies_een_plek(self) -> None:
        """De vondst van RC-212: twee keer gevraagd, met twee verschillende waarden."""
        container = _basis_container()
        plekken = [a for a in container["args"] if "proxy-headers" in a]
        plekken += [n for n in _env_namen(container) if n == "KC_PROXY_HEADERS"]
        assert len(plekken) == 1, plekken

    @pytest.mark.parametrize(("optie", "waarde"), UIT_DE_ARGS_GEHAALD)
    @pytest.mark.parametrize("omgeving", ["basis", "sandboxed-local", "odcn"])
    def test_wat_uit_de_args_verdween_geldt_nog_steeds_via_de_omgeving(
        self, omgeving: str, optie: str, waarde: str
    ) -> None:
        """Een vlag weghalen omdat de env hetzelfde zegt, pint die env niet.

        Deze ronde haalde --proxy-headers en --hostname-strict bij de args weg. Wat daarna
        GELDT is de waarde in de env; staat daar de verkeerde, dan is de val van 48c2dbbb
        terug zonder dat er een vlag bij staat. De args-helft hoort erbij omdat een argument
        bij Keycloak voor de env gaat: komt hij terug, dan zegt de env niets meer.
        """
        if omgeving == "basis":
            container = _basis_container()
        else:
            if shutil.which("kustomize") is None:
                pytest.skip("kustomize ontbreekt")
            container = _rendered_container(REPO_ROOT, omgeving)
        naam = _omgevingsnaam(optie)
        env = {e["name"]: e.get("value") for e in container["env"]}
        assert env.get(naam) == waarde, f"{omgeving}: {naam}={env.get(naam)!r}, verwacht {waarde!r}"
        wint = [a for a in container["args"] if a == f"--{optie}" or a.startswith(f"--{optie}=")]
        assert not wint, f"{omgeving}: {wint} gaat voor {naam}"

    def test_een_spi_vlag_in_de_bouw_doet_alleen_iets_als_de_pod_hem_ook_noemt(self) -> None:
        """`kc.sh build` legt geen spi-optie vast, dus daar staat hij voor niets.

        De verzender is de uitzondering die het doc opschrijft: hij staat in de bouw als
        canarie EN bij de pod, waar zijn waarde geldt. Komt er een tweede spi-vlag bij de
        bouw, dan leest hij als geregeld terwijl de pod hem niet kent.
        """
        uit_pod = {a[2:].split("=")[0]: a[2:] for a in _basis_container()["args"] if a.startswith("--spi-")}
        for vlag in _kc_build_vlaggen():
            if not vlag.startswith("spi-"):
                continue
            naam = vlag.split("=")[0]
            assert uit_pod.get(naam) == vlag, (vlag, uit_pod.get(naam))

    def test_de_bouwcanarie_noemt_dezelfde_verzender_als_de_pod(self) -> None:
        """`kc.sh build` faalt op een provider-id dat niet oplost, een pod met --optimized niet.

        Gemeten in RC-212: de bouw geeft exit 1 met "Failed to find provider ... for
        emailSender", terwijl een pod met --optimized op zo'n id gewoon opkomt. De canarie uit
        RC-158 staat daarmee in de imagebouw; lopen de twee id's uiteen, dan bewaakt die bouw
        een andere provider dan de pod kiest.
        """
        vlag = "spi-email-sender-provider="
        uit_bouw = [v for v in _kc_build_vlaggen() if v.startswith(vlag)]
        uit_pod = [a[2:] for a in _basis_container()["args"] if a.startswith(f"--{vlag}")]
        assert len(uit_bouw) == 1, uit_bouw
        assert uit_pod == uit_bouw, (uit_pod, uit_bouw)

    def test_het_welkomstthema_staat_alleen_bij_de_pod(self) -> None:
        """Een spi-optie wordt niet ingebakken, dus de bouw kan hem niet voor de pod zetten.

        Gemeten: met de vlag in `kc.sh build` en niet bij de start komt de welkomstpagina
        zonder nl-design-system op.
        """
        vlag = "spi-theme-welcome-theme="
        assert any(a.startswith(f"--{vlag}") for a in _basis_container()["args"])
        assert not any(v.startswith(vlag) for v in _kc_build_vlaggen()), _kc_build_vlaggen()

    @pytest.mark.parametrize("overlay", ["sandboxed-local", "odcn"])
    def test_geen_overlay_zet_een_bouwoptie_terug(self, overlay: str) -> None:
        """Wat een omgeving echt start, en niet wat de basis zegt."""
        if shutil.which("kustomize") is None:
            pytest.skip("kustomize ontbreekt")
        container = _rendered_container(REPO_ROOT, overlay)
        assert "--optimized" in container["args"], (overlay, container["args"])
        namen = [e["name"] for e in container["env"]]
        for optie in BOUWOPTIES:
            assert _omgevingsnaam(optie) not in namen, (overlay, optie)
            assert not any(a.startswith(f"--{optie}") for a in container["args"]), (overlay, optie)

    @pytest.mark.parametrize(
        ("overlay", "host"),
        [
            ("sandboxed-local", "https://keycloak.sandbox.rijksapp.dev"),
            ("odcn", "https://keycloak.rijksapp.nl"),
        ],
    )
    def test_elke_omgeving_houdt_zijn_eigen_hostname(self, overlay: str, host: str) -> None:
        """Die patch wijst de env-lijst op INDEX aan, en deze ronde haalde er vier uit.

        De `op: test` ernaast maakt een verschoven index luid, maar alleen voor wie hem
        bijwerkt; dit meet de uitkomst. Zonder de juiste hostname wijst Keycloak zijn
        OIDC-endpoints naar keycloak.kind en logt niemand meer in.
        """
        if shutil.which("kustomize") is None:
            pytest.skip("kustomize ontbreekt")
        env = {e["name"]: e.get("value") for e in _rendered_container(REPO_ROOT, overlay)["env"]}
        assert env["KC_HOSTNAME"] == host, env["KC_HOSTNAME"]


class TestDePinStaatPerOmgeving:
    """Een proef in de sandbox mag de basis en ODCN niet raken."""

    def test_de_basis_draagt_geen_tag_die_niemand_gepusht_kan_hebben(self) -> None:
        """De tag hier wordt niet met de hand geschreven, zie `task pin-keycloak-image TARGET=base`.

        Een `-dirty`tag komt van bron die niet in git staat: dan is op productie niet na te
        gaan wat er draait, en is de image ook niet opnieuw te maken.
        """
        tag = _basis_image().split(":")[-1]
        assert tag not in ("", "latest")
        assert not tag.endswith("-dirty"), tag

    def test_de_sandbox_pint_zijn_eigen_tag(self) -> None:
        """Zonder eigen pin is iets proberen in de sandbox hetzelfde als uitrollen naar ODCN."""
        entry = _image_entry(_overlay("sandboxed-local"))
        assert entry is not None, "de sandbox-overlay pint het Keycloak-image niet"
        assert entry.get("newTag"), entry
        # Een tag van alleen cijfers is onaangehaald een yaml-getal, en dan weigert kustomize
        # de HELE overlay. De pintaak haalt hem daarom aan; een pin met de hand kan dat missen.
        assert isinstance(entry["newTag"], str), f"newTag {entry['newTag']!r} moet aangehaald in de overlay staan"
        assert "newName" not in entry, "de sandbox haalt uit dezelfde registry als de basis"

    @pytest.mark.parametrize("plek", ["basis", "sandbox"], ids=["de basis", "de sandbox-overlay"])
    def test_geen_van_de_pinnen_is_met_de_hand_geschreven(self, plek: str) -> None:
        """DE VAL DIE ER STOND: de basis pinde `2026.9.1`, een CalVer die alleen CI uitrekent.

        Die image stond op ghcr niet, en dat blijkt pas bij de eerstvolgende podstart. Beide
        pinnen hebben maar een schrijver (`pin-keycloak-image`, voor de basis met TARGET=base,
        en die neemt de sandboxtag over), dus een tag die niet de vorm van die schrijver heeft,
        kan er alleen met de hand in zijn gezet.

        De CalVer van CI (`{YYYY}.{M}.{patch}`) valt daar buiten: die wordt in dezelfde run
        uitgerekend en gepusht en hoort in geen enkel manifest.
        """
        tag = _basis_image().split(":")[-1] if plek == "basis" else _image_entry(_overlay("sandboxed-local"))["newTag"]

        # Een plaatshouder ("er is hier nog niets gebouwd") draagt geen cijfer en is daarmee
        # ook geen versie die eruitziet als iets dat te halen valt.
        assert GEPUSHTE_TAG.fullmatch(tag) or not any(teken.isdigit() for teken in tag), (
            f"{plek} pint {tag!r}: alleen de taken schrijven deze regel, en die pinnen een CalVer"
        )

    def test_odcn_erft_de_tag_van_de_basis(self) -> None:
        """Promoveren is het verplaatsen van een tag naar de basis, niet een tweede pin."""
        entry = _image_entry(_overlay("odcn"))
        assert entry is not None, "de odcn-overlay noemt het Keycloak-image niet"
        assert "newTag" not in entry, entry

    def test_odcn_wijst_naar_de_rcr_spiegel(self) -> None:
        """Staat het pad niet in deze vorm in git, dan blijft de applicatie OutOfSync."""
        entry = _image_entry(_overlay("odcn"))
        assert entry["newName"] == RCR_VOORVOEGSEL + IMAGE_REPO.removeprefix("ghcr.io/")

    def test_odcn_draagt_het_pull_secret_dat_die_spiegel_vraagt(self) -> None:
        """De proxy vraagt authenticatie, ook voor een image dat op ghcr publiek is."""
        patches = _overlay("odcn")["patches"]
        bestanden = [KEYCLOAK / "overlays/odcn" / p["path"] for p in patches if "path" in p]
        secrets = [
            s["name"]
            for pad in bestanden
            for s in (yaml.safe_load(pad.read_text())["spec"]["template"]["spec"].get("imagePullSecrets") or [])
        ]
        assert secrets == ["ghcr-rig-robot-pull-secret"]

    def test_odcn_rendert_dat_pull_secret_ook_echt(self) -> None:
        """Een patch die zijn doel niet raakt is geen fout: kustomize rendert hem stil weg.

        Dan staat het secret wel in git en niet in de pod, en dat blijkt pas als de pod voor
        het eerst opnieuw wordt aangemaakt.
        """
        if shutil.which("kustomize") is None:
            pytest.skip("kustomize ontbreekt")
        pod = _rendered_pod(REPO_ROOT, "odcn")
        assert [s["name"] for s in pod.get("imagePullSecrets", [])] == ["ghcr-rig-robot-pull-secret"]


class TestDeImageWordtGebouwdEnGepusht:
    """Zonder deze wiring is de image niet te bouwen en draaien de toetsen van de jars nergens."""

    def test_de_imagebouw_is_te_kiezen(self) -> None:
        """Staat de naam niet bij de keuzes, dan is de job er wel maar nooit te starten."""
        workflow = yaml.safe_load(DOCKER_IMAGES.read_text())
        # workflow[True]: yaml leest de sleutel `on` als de boolean.
        keuzes = workflow[True]["workflow_dispatch"]["inputs"]["image"]["options"]
        assert IMAGE_NAAM in keuzes

    def test_de_job_bouwt_precies_de_dockerfile_van_dit_image(self) -> None:
        job = _docker_images_job()
        stap = next(s for s in job["steps"] if s.get("uses", "").startswith("docker/build-push-action"))
        assert stap["with"]["file"] == str(DOCKERFILE.relative_to(REPO_ROOT))
        assert stap["with"]["context"] == ".", "de Maven-fase kopieert uit de repo-wortel"
        assert stap["with"]["push"] is True

    def test_de_job_pusht_naar_het_pad_dat_de_manifesten_pinnen(self) -> None:
        """Wijkt de naam af, dan pullt de pod een image dat niemand bouwt."""
        assert _docker_images_job()["env"]["IMAGE_NAME"] == IMAGE_REPO

    def test_de_sha_tag_wordt_meegepusht(self) -> None:
        """Dat is de tag waarmee je een net gebouwde jar draait zonder een versie te verzinnen."""
        stap = next(
            s for s in _docker_images_job()["steps"] if s.get("uses", "").startswith("docker/build-push-action")
        )
        tags = [t.strip() for t in stap["with"]["tags"].splitlines() if t.strip()]
        assert all(t.startswith("${{ env.IMAGE_NAME }}:") for t in tags), tags

        achtervoegsels = {t.split(":", 1)[1] for t in tags}
        assert "sha-${{ github.sha }}" in achtervoegsels
        assert "${{ needs.prepare.outputs.calver_tag }}" in achtervoegsels

    def test_ci_draait_de_toetsen_van_elke_module(self) -> None:
        """De imagebouw draait alleen op workflow_dispatch, dus anders draaien ze nergens.

        De modulenamen komen uit de mappen: een derde module zonder job valt hier om.
        """
        job = yaml.safe_load(CI_WORKFLOW.read_text())["jobs"]["keycloak-providers"]
        assert sorted(job["strategy"]["matrix"]["module"]) == _module_namen()

        stap = next(s for s in job["steps"] if "mvn" in s.get("run", ""))
        assert stap["working-directory"] == "keycloak-migration/${{ matrix.module }}"
        assert stap["run"].strip() == "mvn -B test"

    def test_elke_module_blijft_los_te_bouwen_en_te_toetsen(self) -> None:
        """De image bundelt de jars, hij vervangt ze niet: wie aan de mapper werkt, bouwt de mapper."""
        taken = yaml.safe_load(TASKFILE.read_text())["tasks"]
        for naam in _module_namen():
            for vorm in ("build", "test"):
                taak = f"{vorm}-keycloak-{naam}"
                assert taak in taken, taak
                assert f"keycloak-migration/{naam}" in yaml.dump(taken[taak])

    def test_elke_module_bouwt_reproduceerbaar(self) -> None:
        """Bouwt een van de twee jars op de klok, dan is de imagelaag alsnog elke keer nieuw.

        De waarde moet vast staan: `${maven.build.timestamp}` zet de klok er weer in.
        """
        for naam in _module_namen():
            waarde = re.search(
                r"<project\.build\.outputTimestamp>(.*?)</project\.build\.outputTimestamp>",
                (MODULES / naam / "pom.xml").read_text(),
            )
            assert waarde is not None, naam
            assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(Z|[+-]\d{2}:\d{2})|\d{9,}", waarde.group(1)), (
                naam,
                waarde.group(1),
            )


class TestDePublicatietaakHeeftDeVormVanDeOperationsManager:
    """Het werk staat in de Taskfile en niet in een eigen script, dus CI kan dezelfde taak aanroepen.

    Deze toetsen lezen de taken; wat er WERKELIJK gebeurt bij het pinnen en promoveren staat
    lager in dit bestand, waar die twee taken echt gedraaid worden. De publicatietaak zelf is
    niet te draaien zonder een echte bouw, een inlog bij ghcr en `requirements-check`.
    """

    def test_hij_bouwt_via_de_gedeelde_taak_en_niet_zelf(self) -> None:
        """Een eigen `docker buildx build` zou de vlaggen van die taak gaan herhalen.

        Over alle drie de taken gekeken: een eigen bouwaanroep die ergens in deze keten
        opduikt valt hier ook om.
        """
        assert _aangeroepen(PUBLICEER, "docker-build-and-push") is not None

        eigen = [r for r in _shell(PUBLICEER, PIN).splitlines() if r.lstrip().startswith("docker ")]
        assert eigen == [], eigen

    def test_hij_bouwt_dezelfde_dockerfile_en_context_als_ci(self) -> None:
        """Anders draagt de sandboxtag een ander artefact dan CI onder diezelfde bron bouwt.

        Uit docker-images.yml gelezen, want dat is de andere bouwer; staat het hier apart
        overgetypt, dan lopen ze uiteen zonder dat iets het merkt.
        """
        meegegeven = _aangeroepen(PUBLICEER, "docker-build-and-push")["vars"]
        bij_ci = _ci_bouwstap()["with"]

        assert meegegeven["DOCKERFILE_PATH"] == bij_ci["file"]
        assert meegegeven["BUILD_CONTEXT"] == bij_ci["context"] == ".", "de Maven-fase kopieert uit de repo-wortel"

    def test_hij_pusht_voor_dezelfde_platforms_als_ci(self) -> None:
        """Wat op de sandbox draait, draait na een promotie op ODCN, en dat is amd64."""
        meegegeven = _aangeroepen(PUBLICEER, "docker-build-and-push")["vars"]
        assert meegegeven["PLATFORMS"] == _ci_bouwstap()["with"]["platforms"]

    def test_hij_pusht_naar_het_pad_dat_de_manifesten_pinnen(self) -> None:
        """De gedeelde taak zet de naam achter de registry; lopen die twee uiteen, dan pullt
        de pod een image die niemand bouwt."""
        meegegeven = _aangeroepen(PUBLICEER, "docker-build-and-push")["vars"]
        assert f"{meegegeven['REGISTRY_IMAGE']}/{meegegeven['IMAGE_NAME']}" == IMAGE_REPO

    def test_hij_pusht_de_calver_en_latest(self) -> None:
        """Dezelfde twee tags als publish-operations-manager: de CalVer plus `latest`."""
        meegegeven = _aangeroepen(PUBLICEER, "docker-build-and-push")["vars"]
        assert meegegeven["IMAGE_TAG"] == "{{.IMAGE_VERSION}}"
        assert meegegeven["EXTRA_TAG"] == "latest"

    def test_de_tag_wordt_op_dezelfde_manier_uitgerekend_als_die_van_de_operations_manager(self) -> None:
        """Dezelfde berekening, dus hetzelfde script: een eigen variant zou stil afwijken.

        Niet meer letterlijk hetzelfde blok: `-dirty` gaat over de BRONPADEN van het image
        (RC-265), en die zijn per image anders. Wat gelijk moet blijven is de berekening, en
        die staat in `scripts/calver-tag.sh`. Daarmee is dit tegelijk de toets op de vorm
        zelf; hoe die eruitziet staat in GEPUSHTE_TAG en wordt lager gemeten.
        """
        eigen = _taak(PUBLICEER)["vars"]["IMAGE_VERSION"]["sh"].split()
        van_opi = _taak("publish-operations-manager")["vars"]["IMAGE_VERSION"]["sh"].split()

        assert eigen[0] == van_opi[0] == "scripts/calver-tag.sh", (eigen, van_opi)
        assert eigen[1:], "zonder bronpaden meet het script niets"
        assert eigen[1:] != van_opi[1:], "elk image draagt zijn eigen bronpaden"

    def test_de_bronpaden_zijn_de_paden_waar_de_dockerfile_uit_kopieert(self) -> None:
        """Mist een pad, dan leest een vuile bron als schoon; staat er een te veel bij, dan
        is elke tag weer -dirty om een wijziging die niet in de image zit."""
        dockerfile = (REPO_ROOT / "images/keycloak-zad/Dockerfile").read_text()
        gekopieerd = {
            "/".join(r.split()[1].split("/")[:2])
            for r in dockerfile.splitlines()
            if r.startswith("COPY ") and not r.startswith("COPY --from")
        }

        paden = set(_bronpaden(PUBLICEER))
        assert gekopieerd <= paden, (gekopieerd, paden)
        assert "images/keycloak-zad" in paden, "de Dockerfile en kc.sh-opties horen er ook bij"

    def test_precies_de_gebouwde_tag_wordt_gepind(self) -> None:
        """Een tweede berekening ertussen zou een tag pinnen die niet gepusht is."""
        gebouwd = _aangeroepen(PUBLICEER, "docker-build-and-push")["vars"]["IMAGE_TAG"]
        gepind = _aangeroepen(PUBLICEER, PIN)["vars"]["IMAGE_TAG"]
        assert gebouwd == gepind == "{{.IMAGE_VERSION}}"

    def test_het_pinnen_komt_na_de_bouw(self) -> None:
        """De pin is de belofte dat het artefact er is, dus hij hoort na de push.

        go-task breekt de keten af bij een mislukt commando, dus de ORDE is wat een mislukte
        bouw ervan weerhoudt alsnog te pinnen.
        """
        aangeroepen = [c["task"] for c in _taak(PUBLICEER)["cmds"] if isinstance(c, dict) and "task" in c]
        assert aangeroepen.index("docker-build-and-push") < aangeroepen.index(PIN), aangeroepen

    def test_de_publicatie_pint_de_sandbox_en_niet_de_basis(self) -> None:
        """HET VERSCHIL MET DE OPERATIONS-MANAGER, en het is bewust: sandbox eerst.

        Die pint zijn odcn-overlay; hier is de basis een tweede, bewuste aanroep van dezelfde
        pintaak. Die taak KENT de basis, dus de grens zit in wat de publicatie hem meegeeft:
        geen doel, en dat is de sandbox. Dat de standaard ook echt de sandbox pint, wordt
        lager gemeten door de taak zonder doel te draaien.
        """
        meegegeven = _aangeroepen(PUBLICEER, PIN)["vars"]
        assert meegegeven.get("TARGET", "sandbox") == "sandbox", meegegeven
        assert "overlays/odcn" not in yaml.dump(_taak(PUBLICEER)), _taak(PUBLICEER)

    def test_er_wordt_niets_in_een_cluster_geladen(self) -> None:
        """DE VERVALLEN WEG. `kind load` zette een lokaal gebouwde image in de sandbox.

        Dan draait de sandbox een ander artefact dan productie na een promotie, en is
        "promoveren is dezelfde tag" leeg.
        """
        # Op de regels die DRAAIEN en niet op de hele taak: de uitleg erbij noemt de
        # kind-node van de sandbox, en die zin hoort er juist te staan.
        geladen = [r for r in _shell(PUBLICEER, PIN).splitlines() if r.lstrip().startswith("kind ")]
        assert geladen == [], geladen
        assert "kind load" not in _shell(PUBLICEER, PIN)

    def test_de_taak_waarschuwt_als_de_tag_van_ongecommitte_bron_komt(self) -> None:
        """Zonder die waarschuwing leest de tag als een commit die de image beschrijft."""
        cmds = yaml.dump([c for c in _taak(PUBLICEER)["cmds"] if isinstance(c, str)])
        assert "*-dirty)" in cmds, cmds
        assert "ongecommitte wijzigingen" in cmds, cmds

    def test_de_tag_van_een_vuile_werkboom_draagt_dat_achtervoegsel(self, tmp_path: Path) -> None:
        """De waarschuwing hierboven hangt aan `*-dirty`, dus die vorm moet de var ook maken.

        Gemeten door het blok te draaien in plaats van het te lezen: een `git status` zonder
        `--porcelain` of een verschoven printf laat de waarschuwing stil nooit afgaan.
        """
        repo = _kopie(tmp_path)
        sh = _taak(PUBLICEER)["vars"]["IMAGE_VERSION"]["sh"]

        schoon = _bash(sh, repo)
        # Buiten de bronpaden telt niet mee: dat is precies de wijziging van RC-265.
        (repo / "los.txt").write_text("iets ongecommit\n")
        assert _bash(sh, repo) == schoon, "een wijziging buiten de bron van dit image maakt de tag niet vuil"

        (repo / _bronpaden(PUBLICEER)[0] / "los.txt").write_text("iets ongecommit\n")
        vuil = _bash(sh, repo)

        assert GEPUSHTE_TAG.fullmatch(schoon), schoon
        assert not schoon.endswith("-dirty"), schoon
        assert vuil == f"{schoon}-dirty", (schoon, vuil)

    def test_zonder_inlog_bij_ghcr_weigert_hij_voor_de_bouw(self, tmp_path: Path) -> None:
        """Anders strandt een vergeten inlog pas na de volledige Maven- en kc.sh-ronde.

        Een inlog met een credential-helper laat een LEGE auths-regel achter, dus de sleutel
        is het signaal en niet de inhoud; een config zonder ghcr.io hoort te weigeren.
        """
        controle = next(p for p in _taak(PUBLICEER)["preconditions"] if "ghcr" in p["sh"])
        assert "docker login ghcr.io" in controle["msg"]
        # Zonder deze grens zou de toets een ongerenderd template meten en altijd falen.
        assert "{{" not in controle["sh"], controle["sh"]

        gevallen = {
            "met een lege ghcr-regel": '{"auths": {"ghcr.io": {}}}',
            "met een ander pakhuis": '{"auths": {"registry.example.org": {"auth": "x"}}}',
            "leeg": "{}",
        }
        for uitleg, inhoud in gevallen.items():
            map_ = tmp_path / uitleg.replace(" ", "-")
            map_.mkdir()
            (map_ / "config.json").write_text(inhoud)
            proc = subprocess.run(
                ["bash", "-c", controle["sh"]],
                env=os.environ | {"DOCKER_CONFIG": str(map_)},
                capture_output=True,
                text=True,
            )
            geslaagd = proc.returncode == 0
            assert geslaagd == (uitleg == "met een lege ghcr-regel"), (uitleg, proc.returncode)

        # En zonder config.json helemaal: dat is een verse machine.
        geen = tmp_path / "geen-config"
        geen.mkdir()
        proc = subprocess.run(
            ["bash", "-c", controle["sh"]],
            env=os.environ | {"DOCKER_CONFIG": str(geen)},
            capture_output=True,
            text=True,
        )
        assert proc.returncode != 0, proc.stdout + proc.stderr


#: De stubs staan voor de twee commando's die de taken buiten de repo aanroepen. Ze schrijven
#: op wat ze meekregen, zodat een toets op het WERKELIJK aangeroepen commando kan meten.
DOCKER_STUB = """#!/usr/bin/env bash
echo "docker $*" >> "$STUB_LOG"
exit 0
"""

#: Praat de ghcr-kant na: een anoniem token, en daarna het manifest van de tag. Precies de
#: twee vragen die de kind-node ook stelt.
CURL_STUB = """#!/usr/bin/env bash
echo "curl $*" >> "$STUB_LOG"
for arg in "$@"; do
  case "$arg" in
    *"/token?"*)
      [ -z "${STUB_GEEN_TOKEN:-}" ] || exit 22
      echo '{"token":"proef"}'
      exit 0
      ;;
    *"/manifests/"*)
      [ -z "${STUB_GEEN_MANIFEST:-}" ] || exit 22
      printf ok
      exit 0
      ;;
  esac
done
exit 0
"""


class Run:
    """Uitkomst van EEN taakrun: zijn exitcode, zijn uitvoer en wat de stubs opvingen."""

    def __init__(self, proc: subprocess.CompletedProcess[str], repo: Path) -> None:
        self.returncode = proc.returncode
        self.stdout = proc.stdout
        self.stderr = proc.stderr
        self.repo = repo
        log = repo.parent / "calls.log"
        self.log = log.read_text() if log.exists() else ""

    def overlay(self, naam: str) -> str:
        return (self.repo / KEYCLOAK.relative_to(REPO_ROOT) / "overlays" / naam / "kustomization.yaml").read_text()

    def pin(self, naam: str) -> str | None:
        """De tag zoals hij NU in de overlay staat; een volgende run schrijft er overheen."""
        entry = _image_entry(yaml.safe_load(self.overlay(naam)))
        return None if entry is None else entry.get("newTag")

    def basis_tag(self) -> str:
        pad = self.repo / KEYCLOAK.relative_to(REPO_ROOT) / "base/deployment.yaml"
        image = yaml.safe_load(pad.read_text())["spec"]["template"]["spec"]["containers"][0]["image"]
        return image.split(":")[-1]


def _taken() -> dict:
    return yaml.safe_load(TASKFILE.read_text())["tasks"]


def _taak(naam: str) -> dict:
    taken = _taken()
    assert naam in taken, f"{naam} staat niet in de Taskfile"
    return taken[naam]


def _aangeroepen(naam: str, doel: str) -> dict | None:
    """De `- task: <doel>`-stap uit de cmds van taak <naam>, met de vars die hij meegeeft."""
    return next((c for c in _taak(naam)["cmds"] if isinstance(c, dict) and c.get("task") == doel), None)


def _shell(*namen: str) -> str:
    """Alle shell die deze taken zelf draaien: hun cmds, preconditions en sh-vars.

    Niet de hele taak: een assertie op de yaml-dump meet ook de uitleg, en die noemt
    juist de dingen die er niet meer mogen gebeuren.
    """
    stukken: list[str] = []
    for naam in namen:
        taak = _taak(naam)
        stukken += [c for c in taak["cmds"] if isinstance(c, str)]
        stukken += [p["sh"] for p in taak.get("preconditions", [])]
        stukken += [v["sh"] for v in taak.get("vars", {}).values() if isinstance(v, dict) and "sh" in v]
    assert stukken, namen
    return "\n".join(stukken)


def _ci_bouwstap() -> dict:
    return next(s for s in _docker_images_job()["steps"] if s.get("uses", "").startswith("docker/build-push-action"))


def _bronpaden(taak: str) -> list[str]:
    """De paden die de tagvar van deze taak aan calver-tag.sh meegeeft."""
    return _taak(taak)["vars"]["IMAGE_VERSION"]["sh"].split()[1:]


def _bash(script: str, cwd: Path) -> str:
    return subprocess.run(["bash", "-c", script], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _kopie(tmp_path: Path) -> Path:
    """Een git-repo met de echte Taskfile en de echte Keycloak-manifesten erin.

    De taken pinnen een bestand in hun eigen repo, dus het meten gebeurt op een kopie.
    `scripts/bron-tag.sh` en `scripts/calver-tag.sh` gaan mee omdat de Taskfile er zijn
    tags mee uitrekent (SANDBOX_OM_TAG en IMAGE_VERSION); zonder die scripts valt elke taak
    om. De bronpaden van dit image gaan mee omdat de tagvar ze opvraagt.
    """
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy(BRON_TAG, repo / "scripts" / BRON_TAG.name)
    shutil.copy(CALVER_TAG, repo / "scripts" / CALVER_TAG.name)
    shutil.copy(TASKFILE, repo / TASKFILE.name)
    shutil.copytree(KEYCLOAK, repo / KEYCLOAK.relative_to(REPO_ROOT))
    for bronpad in _bronpaden(PUBLICEER):
        (repo / bronpad).mkdir(parents=True, exist_ok=True)
        (repo / bronpad / "bron.txt").write_text("bron\n")

    for cmd in (["init", "-q"], ["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "bron"]):
        subprocess.run(["git", *cmd], cwd=repo, check=True, capture_output=True)
    return repo


def _rendered_pod(wortel: Path, overlay: str) -> dict:
    """De podspec die kustomize voor deze overlay oplevert, niet wat de manifesten zeggen."""
    proc = subprocess.run(
        ["kustomize", "build", str(wortel / KEYCLOAK.relative_to(REPO_ROOT) / "overlays" / overlay)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    deployment = next(d for d in yaml.safe_load_all(proc.stdout) if d and d["kind"] == "Deployment")
    return deployment["spec"]["template"]["spec"]


def _rendered_container(wortel: Path, overlay: str) -> dict:
    return _rendered_pod(wortel, overlay)["containers"][0]


def _rendered_image(wortel: Path, overlay: str) -> str:
    return _rendered_container(wortel, overlay)["image"]


def _draai(tmp_path: Path, taak: str, *extra: str, **env_extra: str) -> Run:
    """Draait EEN taak op de kopie, met stubs voor docker en curl voor de echte op PATH."""
    repo = tmp_path / "repo"
    if not repo.exists():
        repo = _kopie(tmp_path)

    bindir = tmp_path / "bin"
    if not bindir.exists():
        bindir.mkdir()
        for naam, body in (("docker", DOCKER_STUB), ("curl", CURL_STUB)):
            stub = bindir / naam
            stub.write_text(body)
            stub.chmod(0o755)

    # Leeg per run, zodat een assertie op het log over DEZE run gaat en niet over de vorige.
    (tmp_path / "calls.log").write_text("")

    proc = subprocess.run(
        ["task", taak, *extra],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=120,
        # De stubs ervoor en niet in plaats van het PATH: `task` is hier een mise-shim en
        # git, awk en kustomize moeten gewoon te vinden blijven.
        env=os.environ
        | {"PATH": f"{bindir}:{os.environ['PATH']}", "STUB_LOG": str(tmp_path / "calls.log")}
        | env_extra,
    )
    return Run(proc, repo)


def _pin(tmp_path: Path, tag: str, **env_extra: str) -> Run:
    return _draai(tmp_path, PIN, f"IMAGE_TAG={tag}", **env_extra)


def _promoveer(tmp_path: Path, *extra: str, **env_extra: str) -> Run:
    """Promoveren is dezelfde pintaak, met de basis als doel."""
    return _draai(tmp_path, PIN, "TARGET=base", *extra, **env_extra)


#: Een tag in de vorm die publish-keycloak-image uitrekent. Vast, niet van de klok: anders
#: hangt een assertie op de tag aan het moment waarop de toets draaide.
PROEFTAG = "2026.09.26.1432-abcdef12"


@pytest.fixture
def task_aanwezig() -> None:
    if shutil.which("task") is None:
        pytest.skip("task niet gevonden op PATH")
    if shutil.which("bash") is None:
        pytest.skip("bash niet gevonden op PATH")


@pytest.mark.usefixtures("task_aanwezig")
class TestDeSandboxPultDeImageNetAlsElkeAndereOmgeving:
    """Eén artefact, gebouwd en gepusht: anders is promoveren niet dezelfde tag."""

    def test_de_sandbox_draait_de_tag_die_de_taak_meekrijgt(self, tmp_path: Path) -> None:
        run = _pin(tmp_path, PROEFTAG)

        assert run.returncode == 0, run.stderr
        assert run.pin("sandboxed-local") == PROEFTAG

    def test_de_basis_en_odcn_blijven_precies_zoals_ze_waren(self, tmp_path: Path) -> None:
        """Zonder dit is bouwen voor de sandbox hetzelfde als uitrollen naar productie."""
        run = _pin(tmp_path, PROEFTAG)

        assert run.returncode == 0, run.stderr
        gewijzigd = subprocess.run(
            ["git", "status", "--porcelain"], cwd=run.repo, capture_output=True, text=True, check=True
        ).stdout.split()
        assert gewijzigd == ["M", str(SANDBOX_OVERLAY.relative_to(REPO_ROOT))]

    def test_de_sandbox_rendert_de_verse_tag_en_odcn_de_oude(self, tmp_path: Path) -> None:
        """De acceptatie van het plan, met beide builds naast elkaar.

        De pin is pas iets waard in wat kustomize oplevert: een tweede images-regel of een
        patch elders zou hem overschrijven zonder dat de kustomization er anders uitziet.
        """
        if shutil.which("kustomize") is None:
            pytest.skip("kustomize ontbreekt")

        voor = _rendered_image(REPO_ROOT, "odcn")
        run = _pin(tmp_path, PROEFTAG)
        assert run.returncode == 0, run.stderr

        assert _rendered_image(run.repo, "sandboxed-local") == f"{IMAGE_REPO}:{PROEFTAG}"
        assert _rendered_image(run.repo, "odcn") == voor
        assert voor.startswith(RCR_VOORVOEGSEL)

    def test_een_tag_van_alleen_cijfers_blijft_aangehaald(self, tmp_path: Path) -> None:
        """GEMETEN: op een onaangehaalde newTag die als yaml-getal leest weigert kustomize de
        HELE overlay ("cannot unmarshal number into Go struct field Image.images.newTag").

        De CalVer-vorm maakt dat vandaag onmogelijk, maar de pintaak neemt ook een tag van
        CI aan, en die vorm kan veranderen. Dan valt niet alleen keycloak om maar de hele
        overlay, en dat is alles wat de sandbox uit deze map draait.
        """
        if shutil.which("kustomize") is None:
            pytest.skip("kustomize ontbreekt")

        run = _pin(tmp_path, "20260926")

        assert run.returncode == 0, run.stderr
        assert run.pin("sandboxed-local") == "20260926"
        assert isinstance(run.pin("sandboxed-local"), str)
        assert _rendered_image(run.repo, "sandboxed-local") == f"{IMAGE_REPO}:20260926"

    def test_een_ander_image_in_dezelfde_lijst_blijft_staan(self, tmp_path: Path) -> None:
        """De pin is een regel onder een naam, niet de eerste newTag die voorbijkomt."""
        repo = _kopie(tmp_path)
        pad = repo / SANDBOX_OVERLAY.relative_to(REPO_ROOT)
        pad.write_text(
            pad.read_text().replace(
                f"  - name: {IMAGE_REPO}\n",
                "  - name: ghcr.io/iets/anders\n    newTag: blijft-staan\n" + f"  - name: {IMAGE_REPO}\n",
            )
        )

        run = _pin(tmp_path, PROEFTAG)

        assert run.returncode == 0, run.stderr
        buur = next(i for i in yaml.safe_load(run.overlay("sandboxed-local"))["images"] if i["name"].endswith("anders"))
        assert buur["newTag"] == "blijft-staan"

    def test_de_tag_van_een_ander_image_in_dezelfde_lijst_wordt_niet_overschreven(self, tmp_path: Path) -> None:
        """Hij schrijft de newTag ONDER onze naam, niet de eerste die daarna voorbijkomt.

        Staat onze regel er zonder tag, dan valt er niets te pinnen. Zonder die grens zou de
        tag van de buur eronder de onze worden: de sandbox draait dan een vreemd artefact en
        de fout die hier hoort blijft weg, want de teruglezende grep vindt zijn tag alsnog.
        Dezelfde grens die de promotietaak bij het LEZEN al heeft.
        """
        repo = _kopie(tmp_path)
        pad = repo / SANDBOX_OVERLAY.relative_to(REPO_ROOT)
        pad.write_text(
            re.sub(
                rf"(  - name: {re.escape(IMAGE_REPO)}\n)    newTag: \S+\n",
                lambda m: m.group(1) + "  - name: ghcr.io/iets/anders\n    newTag: van-de-buur\n",
                pad.read_text(),
            )
        )
        # Sloeg de opzet niet aan, dan meet deze toets de gewone weg en is hij altijd groen.
        assert "van-de-buur" in pad.read_text()

        run = _pin(tmp_path, PROEFTAG)

        buur = next(i for i in yaml.safe_load(run.overlay("sandboxed-local"))["images"] if i["name"].endswith("anders"))
        assert buur["newTag"] == "van-de-buur"
        assert run.returncode != 0, run.stdout
        assert IMAGE_REPO in run.stderr

    def test_een_overlay_zonder_images_regel_is_een_luide_fout(self, tmp_path: Path) -> None:
        """Stil doorgaan zou een sandbox opleveren die de tag van productie blijft draaien."""
        repo = _kopie(tmp_path)
        pad = repo / SANDBOX_OVERLAY.relative_to(REPO_ROOT)
        pad.write_text(re.sub(rf"images:\n  - name: {re.escape(IMAGE_REPO)}\n    newTag: \S+\n", "", pad.read_text()))

        run = _pin(tmp_path, PROEFTAG)

        assert run.returncode != 0
        assert IMAGE_REPO in run.stderr
        assert not list(pad.parent.glob("*.nieuw")), "het halve bestand blijft in de repo achter"


@pytest.mark.usefixtures("task_aanwezig")
class TestErWordtNietsGepindDatDeSandboxNietKanHalen:
    """De pin is de belofte dat het artefact er is; hij komt na de push, niet ervoor."""

    @pytest.mark.parametrize("knop", ["STUB_GEEN_TOKEN", "STUB_GEEN_MANIFEST"])
    def test_een_pakket_dat_niet_zonder_inlog_te_halen_is_wordt_niet_gepind(self, tmp_path: Path, knop: str) -> None:
        """DE STILLE VAL. Een pakket staat op ghcr standaard PRIVAAT na de eerste push.

        De taak stelt daarom dezelfde twee vragen als de kind-node, die anoniem pullt en er
        anders pas bij de eerste podstart op strandt. Beide vragen apart: valt een van de twee
        uit de grendel, dan blijft de andere hem groen houden.
        """
        run = _pin(tmp_path, PROEFTAG, **{knop: "1"})

        assert run.returncode != 0, (knop, run.stdout, run.stderr)
        assert "niet zonder inlog te halen" in run.stderr, knop
        assert run.pin("sandboxed-local") == _image_entry(_overlay("sandboxed-local"))["newTag"], knop

    def test_de_controle_vraagt_naar_precies_deze_tag(self, tmp_path: Path) -> None:
        """Een controle op het PAKKET zou groen zijn zodra er ooit iets gepusht was."""
        run = _pin(tmp_path, PROEFTAG)

        assert run.returncode == 0, run.stderr
        assert any(f"/manifests/{PROEFTAG}" in regel for regel in run.log.splitlines()), run.log

    def test_zonder_tag_pint_hij_niets(self, tmp_path: Path) -> None:
        """De taak is ook los aan te roepen; zonder tag zou hij `newTag: ""` schrijven."""
        run = _draai(tmp_path, PIN)

        assert run.returncode != 0
        assert "IMAGE_TAG" in run.stdout + run.stderr
        assert run.pin("sandboxed-local") == _image_entry(_overlay("sandboxed-local"))["newTag"]


@pytest.mark.usefixtures("task_aanwezig")
class TestPromoverenIsEenVerplaatsingEnGeenHerbouw:
    """De basis draagt alleen een tag die op de sandbox heeft gedraaid, en die is gepusht."""

    def test_de_basis_krijgt_precies_de_tag_die_de_sandbox_draait(self, tmp_path: Path) -> None:
        _pin(tmp_path, PROEFTAG)

        promotie = _promoveer(tmp_path)

        assert promotie.returncode == 0, promotie.stderr
        assert promotie.basis_tag() == PROEFTAG

    def test_promoveren_bouwt_en_pusht_niets(self, tmp_path: Path) -> None:
        """Een herbouw levert een ander artefact op, en dan is het bewijs van de sandbox weg."""
        _pin(tmp_path, PROEFTAG)
        promotie = _promoveer(tmp_path)

        assert promotie.returncode == 0, promotie.stderr
        # Op de regels die een AANROEP zijn: de ghcr-controle vraagt om een docker-manifest,
        # dus dat woord staat sowieso in het log van de curl-stub.
        gebouwd = [r for r in promotie.log.splitlines() if r.startswith("docker ")]
        assert gebouwd == [], gebouwd

    def test_de_promotie_weigert_een_tag_die_niet_op_de_sandbox_draaide(self, tmp_path: Path) -> None:
        """Er valt niets te kiezen: de tag komt uit de sandbox-overlay en nergens anders.

        Promoveren is dezelfde pintaak, dus er KAN een IMAGE_TAG mee. Gold die zomaar, dan
        stond er een tag op productie die nooit op de sandbox heeft gedraaid, en is de hele
        weg sandbox-eerst voor niets.
        """
        _pin(tmp_path, PROEFTAG)

        promotie = _promoveer(tmp_path, "IMAGE_TAG=2000.01.01.0000-aaaaaaaa")

        assert promotie.returncode != 0, promotie.stdout
        assert "niet op de sandbox gedraaid" in promotie.stderr, promotie.stderr
        assert promotie.basis_tag() == _basis_image().split(":")[-1], promotie.stdout

    def test_de_tag_van_de_sandbox_meegeven_mag_wel(self, tmp_path: Path) -> None:
        """Anders leest de weigering hierboven als "IMAGE_TAG mag hier niet", en dat is het
        niet: meegeven is een bevestiging van wat de sandbox draagt."""
        _pin(tmp_path, PROEFTAG)

        promotie = _promoveer(tmp_path, f"IMAGE_TAG={PROEFTAG}")

        assert promotie.returncode == 0, promotie.stderr
        assert promotie.basis_tag() == PROEFTAG

    def test_een_onbekend_doel_pint_niets(self, tmp_path: Path) -> None:
        """Een typefout in TARGET mag niet stil op de standaard terugvallen: dan pint een
        mislukte promotie de sandbox."""
        run = _draai(tmp_path, PIN, "TARGET=odcn", f"IMAGE_TAG={PROEFTAG}")

        assert run.returncode != 0, run.stdout
        assert "TARGET" in run.stdout + run.stderr
        assert run.pin("sandboxed-local") == _image_entry(_overlay("sandboxed-local"))["newTag"]
        assert run.basis_tag() == _basis_image().split(":")[-1]

    def test_odcn_rendert_na_een_promotie_de_tag_van_de_sandbox(self, tmp_path: Path) -> None:
        """De belofte uit het plan: promoveren zet de tag die op de sandbox draaide."""
        if shutil.which("kustomize") is None:
            pytest.skip("kustomize ontbreekt")

        run = _pin(tmp_path, PROEFTAG)
        promotie = _promoveer(tmp_path)
        assert promotie.returncode == 0, promotie.stderr

        gespiegeld = f"{RCR_VOORVOEGSEL}{IMAGE_REPO.removeprefix('ghcr.io/')}"
        assert _rendered_image(run.repo, "odcn") == f"{gespiegeld}:{PROEFTAG}"
        assert _rendered_image(run.repo, "sandboxed-local") == f"{IMAGE_REPO}:{PROEFTAG}"

    def test_een_tag_van_ongecommitte_bron_wordt_geweigerd(self, tmp_path: Path) -> None:
        """Draait die op productie, dan kan niemand nagaan wat er draait.

        De vorm komt uit de tagvar van de publicatietaak: die zet er `-dirty` achter, en
        `test_de_tag_van_een_vuile_werkboom_draagt_dat_achtervoegsel` meet dat.
        """
        run = _pin(tmp_path, f"{PROEFTAG}-dirty")
        assert run.returncode == 0, run.stderr
        voor = run.basis_tag()

        promotie = _promoveer(tmp_path)

        assert promotie.returncode != 0
        assert "ongecommitte bron" in promotie.stderr
        assert promotie.basis_tag() == voor

    def test_de_tag_van_een_ander_image_in_dezelfde_lijst_wordt_niet_gepromoveerd(self, tmp_path: Path) -> None:
        """Hij leest de newTag ONDER onze naam, niet de eerste die daarna voorbijkomt.

        Staat onze regel er zonder tag, dan valt er niets te promoveren. Zonder die grens zou
        de tag van de buur eronder op productie belanden: een vreemd artefact op de
        identiteitsvoorziening, en de weigering die hier hoort blijft weg.
        """
        repo = _kopie(tmp_path)
        pad = repo / SANDBOX_OVERLAY.relative_to(REPO_ROOT)
        pad.write_text(
            re.sub(
                rf"(  - name: {re.escape(IMAGE_REPO)}\n)    newTag: \S+\n",
                lambda m: m.group(1) + "  - name: ghcr.io/iets/anders\n    newTag: van-de-buur\n",
                pad.read_text(),
            )
        )
        # Sloeg de opzet niet aan, dan meet deze toets de gewone weg en is hij altijd groen.
        assert "van-de-buur" in pad.read_text()

        promotie = _promoveer(tmp_path)

        assert promotie.returncode != 0, promotie.stdout
        assert "pint geen tag" in promotie.stderr, promotie.stderr
        assert "van-de-buur" not in promotie.stdout + promotie.stderr
        assert promotie.basis_tag() == _basis_image().split(":")[-1]

    @pytest.mark.parametrize("knop", ["STUB_GEEN_TOKEN", "STUB_GEEN_MANIFEST"])
    def test_een_pakket_dat_niet_zonder_inlog_te_halen_is_wordt_niet_gepromoveerd(
        self, tmp_path: Path, knop: str
    ) -> None:
        """Dezelfde controle als bij de sandbox, en om dezelfde reden.

        ODCN haalt hetzelfde artefact, alleen via de rcr-proxy die ghcr spiegelt. Is de tag
        daar niet anoniem te halen, dan draagt de basis een tag die productie niet kan pullen,
        en dat blijkt pas bij de eerstvolgende podstart.
        """
        _pin(tmp_path, PROEFTAG)

        promotie = _promoveer(tmp_path, **{knop: "1"})

        assert promotie.returncode != 0, (knop, promotie.stdout)
        assert "niet zonder inlog te halen" in promotie.stderr, knop
        assert promotie.basis_tag() == _basis_image().split(":")[-1], knop

    def test_een_tweede_promotie_van_dezelfde_tag_is_geen_fout(self, tmp_path: Path) -> None:
        """De commit is een losse handeling, dus deze taak wordt ook gewoon twee keer gedraaid.

        Zou de tweede run falen, dan leest dat als "de promotie is niet gelukt" terwijl de
        basis precies draagt wat hij moet dragen.
        """
        _pin(tmp_path, PROEFTAG)
        eerste = _promoveer(tmp_path)
        assert eerste.returncode == 0, eerste.stderr

        tweede = _promoveer(tmp_path)

        assert tweede.returncode == 0, tweede.stderr
        assert f"draagt {PROEFTAG} al" in tweede.stdout, tweede.stdout
        assert tweede.basis_tag() == PROEFTAG

    def test_zonder_pin_in_de_sandbox_valt_er_niets_te_promoveren(self, tmp_path: Path) -> None:
        repo = _kopie(tmp_path)
        pad = repo / SANDBOX_OVERLAY.relative_to(REPO_ROOT)
        pad.write_text(re.sub(rf"images:\n  - name: {re.escape(IMAGE_REPO)}\n    newTag: \S+\n", "", pad.read_text()))

        promotie = _promoveer(tmp_path)

        assert promotie.returncode != 0
        # De weg vooruit staat in de melding, en dat is de taak die pusht en pint.
        assert PUBLICEER in promotie.stderr, promotie.stderr

    def test_promoveren_raakt_alleen_de_image_regel_van_de_basis(self, tmp_path: Path) -> None:
        """De basis draagt ook de startvlaggen; een promotie hoort daar niets aan te doen."""
        repo = _kopie(tmp_path)
        basis = repo / KEYCLOAK.relative_to(REPO_ROOT) / "base/deployment.yaml"
        voor = basis.read_text().splitlines()

        _pin(tmp_path, PROEFTAG)
        promotie = _promoveer(tmp_path)
        assert promotie.returncode == 0, promotie.stderr

        na = basis.read_text().splitlines()
        anders = [(a, b) for a, b in zip(voor, na, strict=True) if a != b]
        assert len(anders) == 1, anders
        assert anders[0][1].strip().startswith(f"image: {IMAGE_REPO}:")
        # De inspringing blijft: een regel die opschuift is in een yaml-boom geen kleinigheid.
        assert anders[0][0].index("image:") == anders[0][1].index("image:")
