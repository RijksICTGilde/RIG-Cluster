"""Tests for the layer layout of operations-manager/Dockerfile, en de image-pins.

What matters is not the total size but how much of it an ordinary code change rebuilds.
The four regressions guarded here are silent: they cost size, never a failing test.
Background and measurements: features/image-lagen.md.

TestManifestImages leest de manifesten die op ODCN terechtkomen: een `latest`-tag wijst
morgen naar een andere image dan vandaag, zonder commit. Zie features/image-versioning-calver.md.
"""

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from opi.core.config import settings
from opi.utils.naming import split_image_reference

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCKERFILE = REPO_ROOT / "operations-manager" / "Dockerfile"
BACKUP_DOCKERFILE = REPO_ROOT / "operations-manager" / "backup-image" / "Dockerfile"
MC_DOCKERFILE = REPO_ROOT / "images" / "mc" / "Dockerfile"
TASKFILE = REPO_ROOT / "Taskfile.yaml"
MC_IMAGE = "ghcr.io/minbzk/base-images/mc"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"
STATIC_DIR = REPO_ROOT / "operations-manager" / "python" / "static"

APP_STAGE = "application"

#: De manifesten die op ODCN terechtkomen. Elk pad is een kustomization-wortel: de toets
#: volgt de resources, components en patches die hij opneemt, zodat een nieuw bestand
#: meekomt zonder dat hier iets bij hoeft.
PRODUCTIEWORTELS = (
    "bootstrap/rig-system/kustomize/overlays/odcn-production",
    "infrastructure/bootstrap/clusters/odcn",
)

#: Los erbij, want een component die nog door geen enkele overlay wordt opgenomen hoort
#: ook niet op latest te staan.
COMPONENTEN = REPO_ROOT / "bootstrap/rig-system/kustomize/components"

#: De back-up- en restoresjablonen uit de tabel van issue #181. Ze halen hun image uit de
#: setting BACKUP_IMAGE, dus hier mag geen registrypad meer in staan.
BACKUP_SJABLONEN = (
    "backup-pod.yaml.jinja",
    "backup-database-pod.yaml.jinja",
    "backup-bucket-pod.yaml.jinja",
    "backup-bucket-mirror-pod.yaml.jinja",
    "restore-pod.yaml.jinja",
    "restore-database-pod.yaml.jinja",
    "restore-bucket-pod.yaml.jinja",
)

#: Verwijzingen die met opzet niet gepind zijn, met de reden per regel. Zonder reden hoort
#: een regel hier niet te staan.
UITZONDERINGEN = {
    (
        "bootstrap/rig-system/kustomize/operations-manager/base/deployment.yaml",
        "operations-manager:latest",
    ): "de odcn-patch overschrijft dit image; de basis draagt de sandboxverwijzing",
    (
        "bootstrap/rig-system/kustomize/overlays/odcn-production/argocd-deployment.yaml",
        "rcr.rijksapps.nl/ghcr-rig/minbzk/base-images/argocd-rig",
    ): "de ArgoCD-CR draagt de versie in spec.version en niet in de image-verwijzing",
}

IMAGE_LIJN = re.compile(r"^\s*image:\s*(\S+)\s*$")


def _instructions(text: str) -> list[tuple[str, str]]:
    """The Dockerfile as (stage, instruction) pairs, continuations joined, comments gone."""
    joined: list[str] = []
    buffer = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            buffer += line[:-1] + " "
            continue
        joined.append(buffer + line)
        buffer = ""
    if buffer:
        joined.append(buffer)

    stage = ""
    pairs: list[tuple[str, str]] = []
    for instruction in joined:
        if instruction.upper().startswith("FROM "):
            match = re.search(r"\bAS\s+(\S+)", instruction, re.IGNORECASE)
            stage = match.group(1) if match else ""
        pairs.append((stage, instruction))
    return pairs


@pytest.fixture(scope="module")
def instructions() -> list[tuple[str, str]]:
    return _instructions(DOCKERFILE.read_text())


@pytest.fixture(scope="module")
def app_stage(instructions: list[tuple[str, str]]) -> list[str]:
    return [instruction for stage, instruction in instructions if stage == APP_STAGE]


def _mc_version(dockerfile: Path) -> str:
    """De tag die dit Dockerfile aan de mc-spiegel geeft."""
    match = re.search(r"^ARG MC_VERSION=(\S+)", dockerfile.read_text(), re.MULTILINE)
    return match.group(1) if match else ""


def _copy_sources(instruction: str) -> list[str]:
    """The source arguments of a COPY, without its flags and without the destination."""
    parts = instruction.split()[1:]
    parts = [part for part in parts if not part.startswith("--")]
    return parts[:-1]


def _copy_dest(instruction: str) -> str:
    return instruction.split()[-1]


def _copies(app_stage: list[str]) -> list[tuple[int, str]]:
    return [(index, line) for index, line in enumerate(app_stage) if line.upper().startswith("COPY ")]


def _kustomization(map_pad: Path) -> Path | None:
    for naam in ("kustomization.yaml", "kustomization.yml"):
        if (map_pad / naam).is_file():
            return map_pad / naam
    return None


def _opgenomen_bestanden(wortel: Path, gezien: set[Path] | None = None) -> list[Path]:
    """De bestanden die een kustomization opneemt, de verwijzingen volgend.

    Niet een glob over de map: wat niet wordt opgenomen, komt ook niet op het cluster, en
    een nieuw opgenomen bestand komt zo mee zonder dat deze toets wordt bijgewerkt.
    """
    gezien = set() if gezien is None else gezien
    kustomization = _kustomization(wortel)
    if kustomization is None or kustomization in gezien:
        return []

    gezien.add(kustomization)
    gevonden = [kustomization]
    doc = yaml.safe_load(kustomization.read_text()) or {}

    verwijzingen: list[str] = []
    for sleutel in ("resources", "components", "bases", "patchesStrategicMerge"):
        verwijzingen += [entry for entry in doc.get(sleutel) or [] if isinstance(entry, str)]
    verwijzingen += [
        entry["path"] for entry in doc.get("patches") or [] if isinstance(entry, dict) and entry.get("path")
    ]

    for verwijzing in verwijzingen:
        if verwijzing.startswith(("http://", "https://")):
            continue
        pad = (kustomization.parent / verwijzing).resolve()
        if pad.is_dir():
            gevonden += _opgenomen_bestanden(pad, gezien)
        elif pad.is_file() and pad not in gezien:
            gezien.add(pad)
            gevonden.append(pad)
    return gevonden


#: De velden waarin een patch een image kan zetten. CNPG draagt zijn image in imageName,
#: niet in image, en zonder die tweede naam blijft die verwijzing ongelezen.
IMAGE_VELDEN = ("/image", "/imageName")


def _patchwaarden(inhoud: str) -> list[str]:
    """De waarden die de JSON6902-patches van een kustomization op een image-veld zetten."""
    doc = yaml.safe_load(inhoud) or {}
    waarden: list[str] = []
    for patch in doc.get("patches") or []:
        ops = yaml.safe_load(patch["patch"]) if isinstance(patch, dict) and patch.get("patch") else None
        waarden += [
            str(op["value"])
            for op in (ops if isinstance(ops, list) else [])
            if isinstance(op, dict) and str(op.get("path", "")).endswith(IMAGE_VELDEN)
        ]
    return waarden


def _imageverwijzingen(pad: Path) -> list[str]:
    """Elke image-verwijzing in een manifest: `image:`-regels, `newTag` en de JSON6902-patches."""
    inhoud = pad.read_text()
    verwijzingen = [match.group(1).strip("'\"") for match in map(IMAGE_LIJN.match, inhoud.splitlines()) if match]
    if not pad.name.startswith("kustomization."):
        return verwijzingen

    doc = yaml.safe_load(inhoud) or {}
    verwijzingen += [
        f"{entry.get('newName') or entry.get('name')}:{entry['newTag']}"
        for entry in doc.get("images") or []
        if entry.get("newTag")
    ]
    return verwijzingen + _patchwaarden(inhoud)


def _is_gepind(verwijzing: str) -> bool:
    """Een digest of een tag die niet beweegt. Een poort in de host is geen tag."""
    if "@sha256:" in verwijzing:
        return True
    naam, _, tag = verwijzing.rpartition(":")
    return bool(naam) and "/" not in tag and tag != "latest"


def _productieverwijzingen() -> list[tuple[str, str]]:
    bestanden = [pad for wortel in PRODUCTIEWORTELS for pad in _opgenomen_bestanden(REPO_ROOT / wortel)]
    bestanden += sorted(COMPONENTEN.rglob("*.yaml"))
    return [
        (str(pad.relative_to(REPO_ROOT)), verwijzing)
        for pad in dict.fromkeys(bestanden)
        for verwijzing in _imageverwijzingen(pad)
    ]


class TestDockerignore:
    def test_node_modules_is_excluded_at_every_depth(self) -> None:
        """Without `**/` the pattern is only matched against the root of the build context."""
        lines = [line.strip() for line in DOCKERIGNORE.read_text().splitlines()]

        assert "**/node_modules/" in lines
        assert "node_modules/" not in lines

    @pytest.mark.parametrize("pattern", ["__pycache__/", "*.pyc", "*.pyo", "*.pyd"])
    def test_python_bytecode_is_excluded_at_every_depth(self, pattern: str) -> None:
        """Bytecode under opi/ changes on every test run and would rebuild the opi layer."""
        lines = [line.strip() for line in DOCKERIGNORE.read_text().splitlines()]

        assert f"**/{pattern}" in lines
        assert pattern not in lines

    def test_yaml_is_excluded_at_the_root_only(self) -> None:
        """opi/configs/*.yaml must reach the image, so this one must NOT get `**/`."""
        lines = [line.strip() for line in DOCKERIGNORE.read_text().splitlines()]

        assert "*.yaml" in lines
        assert "**/*.yaml" not in lines


class TestApplicationStage:
    def test_no_recursive_chown_or_chmod(self, app_stage: list[str]) -> None:
        """A recursive rewrite of /app duplicates every layer above it in a new layer."""
        assert app_stage, f"stage {APP_STAGE} not found"
        offenders = [line for line in app_stage if re.search(r"(chown|chmod)\s+-R", line)]

        assert offenders == []

    def test_every_copy_sets_ownership(self, app_stage: list[str]) -> None:
        """Ownership belongs on the COPY, which is what replaces the `chown -R` layer."""
        copies = [line for line in app_stage if line.upper().startswith("COPY ")]
        assert copies, "no COPY found in the application stage"

        missing = [line for line in copies if "--chown=appuser:appuser" not in line]
        assert missing == []

    def test_app_directory_itself_is_owned_by_the_user(self, app_stage: list[str]) -> None:
        """COPY --chown covers what it writes, not /app, which WORKDIR created as root."""
        user_setup = [line for line in app_stage if "useradd" in line]
        assert user_setup, "no user creation found in the application stage"

        assert any("chown appuser:appuser /app" in line for line in user_setup)

    def test_entrypoint_is_made_executable_after_it_is_copied(self, app_stage: list[str]) -> None:
        """The dropped `chmod -R 755 /app` ran last, so the order used to be free. It is not."""
        copied = [index for index, line in _copies(app_stage) if line.endswith("/app/entrypoint.sh")]
        made_executable = [index for index, line in enumerate(app_stage) if "chmod +x /app/entrypoint.sh" in line]
        assert len(copied) == 1
        assert len(made_executable) == 1

        assert copied[0] < made_executable[0]

    def test_runs_as_uid_1001(self, app_stage: list[str]) -> None:
        """OpenShift compatibility hangs on the number; the name says nothing about the uid."""
        assert "USER appuser" in app_stage

        assert any("groupadd -g 1001 appuser" in line for line in app_stage)
        assert any("useradd" in line and "-u 1001" in line for line in app_stage)

    def test_a_copied_directory_keeps_its_own_name(self, app_stage: list[str]) -> None:
        """COPY writes the CONTENTS of a directory, so a dest of ./static drops css a level up."""
        for _, line in _copies(app_stage):
            dest = _copy_dest(line).rstrip("/")
            for source in _copy_sources(line):
                if (REPO_ROOT / source).is_dir():
                    assert dest.endswith("/" + Path(source).name), line

    def test_every_application_directory_is_copied(self, app_stage: list[str]) -> None:
        """Splitting the block means naming the parts, and a dropped part only fails at runtime."""
        sources = {source for _, line in _copies(app_stage) for source in _copy_sources(line)}

        required = {
            "operations-manager/python/alembic.ini",
            "operations-manager/docker-entrypoint.sh",
            "operations-manager/python/manifests",
            "operations-manager/python/opi",
        }
        assert required - sources == set()

    def test_every_copied_source_exists(self, app_stage: list[str]) -> None:
        """A COPY of a directory that is gone only fails at docker build, long after the merge."""
        missing = [
            source
            for _, line in _copies(app_stage)
            if "--from=" not in line
            for source in _copy_sources(line)
            if not any(REPO_ROOT.glob(source))
        ]
        assert missing == []


class TestStaticLayers:
    def _static_copies(self, app_stage: list[str]) -> list[tuple[int, list[str]]]:
        copies: list[tuple[int, list[str]]] = []
        for index, line in enumerate(app_stage):
            if not line.upper().startswith("COPY "):
                continue
            sources = [src for src in _copy_sources(line) if "python/static" in src]
            if sources:
                copies.append((index, sources))
        return copies

    def test_media_is_copied_before_the_files_that_change(self, app_stage: list[str]) -> None:
        """In one layer a changed stylesheet rebuilds the media that never changes with it."""
        copies = self._static_copies(app_stage)
        assert len(copies) > 1, "static is still copied in a single layer"

        media = [index for index, sources in copies if any(src.endswith((".mp4", ".jpg", ".ico")) for src in sources)]
        changing = [index for index, sources in copies if any(src.endswith(("/css", "/js")) for src in sources)]
        assert media
        assert changing

        assert max(media) < min(changing)

    def test_the_application_code_is_copied_last(self, app_stage: list[str]) -> None:
        """A rebuilt layer drags every layer below it along, so COPY opi must sit below the media."""
        copies = [index for index, line in enumerate(app_stage) if line.upper().startswith("COPY ")]
        opi_copy = [index for index, line in enumerate(app_stage) if line.endswith("./opi")]
        assert len(opi_copy) == 1

        assert opi_copy[0] == max(copies)

    def test_every_static_entry_is_copied(self, app_stage: list[str]) -> None:
        """Splitting the COPY means naming the parts, so a forgotten one is a 404 on the portal."""
        covered: set[Path] = set()
        for _, sources in self._static_copies(app_stage):
            for source in sources:
                covered.update(REPO_ROOT.glob(source))

        expected = {entry for entry in STATIC_DIR.iterdir() if entry.name != "node_modules"}
        missing = [entry for entry in expected if not any(path == entry or path in entry.parents for path in covered)]
        assert missing == []


class TestPinnedTools:
    def test_uv_is_pinned(self, instructions: list[tuple[str, str]]) -> None:
        """`uv:latest` invalidates its own layer and the `uv sync` after it."""
        uv_refs = [line for _, line in instructions if "astral-sh/uv" in line]
        assert uv_refs

        for line in uv_refs:
            assert ":latest" not in line
            assert "${UV_VERSION}" in line

        args = [line for _, line in instructions if line.startswith("ARG UV_VERSION=")]
        assert len(args) == 1
        assert args[0] != "ARG UV_VERSION="

    def test_no_image_from_outside_floats_on_latest(self, instructions: list[tuple[str, str]]) -> None:
        """A floating base rebuilds its own stage and every stage after it."""
        stages = {stage for stage, _ in instructions if stage}
        bases = [line.split()[1] for _, line in instructions if line.upper().startswith("FROM ")]

        external = [base for base in bases if base not in stages]
        assert external

        for base in external:
            image, _, tag = base.rpartition(":")
            assert image, f"{base} carries no tag, which means latest"
            assert tag != "latest", base

    @pytest.mark.parametrize("dockerfile", [DOCKERFILE, BACKUP_DOCKERFILE], ids=["opi", "backup"])
    def test_mc_komt_uit_onze_eigen_spiegel(self, dockerfile: Path) -> None:
        """Elk adres van MinIO zelf is dichtgegaan, en de GitHub-release kan de vierde zijn.

        dl.min.io geeft 410 met een tekstbody die `curl -LO` als binary opsloeg, quay.io en
        docker.io geven 401 sinds MinIO zijn images achter een abonnement zette, en de
        productiepin van 2 september is daardoor niet meer te herbouwen. Daarom haalt een
        OPI-build mc niet meer zelf op maar uit images/mc, onze eigen spiegel.
        """
        lines = [line for _, line in _instructions(dockerfile.read_text())]

        for dood in ("dl.min.io", "quay.io/minio", "docker.io/minio"):
            assert not [line for line in lines if dood in line], f"{dood} staat nog in het bouwpad"
        assert not [line for line in lines if "github.com/minio/mc/releases" in line], (
            "de build haalt mc nog bij MinIO op en hangt dus aan dat kanaal"
        )

        stage = [
            line
            for line in lines
            if line.upper().startswith("FROM ")
            and line.split()[1] == f"{MC_IMAGE}:${{MC_VERSION}}"
            and re.search(r"\bAS\s+mc$", line)
        ]
        assert stage, f"geen bouwfase op {MC_IMAGE}:${{MC_VERSION}}"

        # Het pad is hetzelfde als de quay-image had, en de spiegel houdt het zo.
        haal = [line for line in lines if line.upper().startswith("COPY ") and "--from=mc" in line]
        assert haal, "de mc-stage wordt nergens uitgelezen"
        for line in haal:
            assert "/usr/bin/mc" in line, f"mc wordt op een ander pad gezocht dan de spiegel biedt: {line}"

    def test_de_afnemers_pinnen_de_tag_die_de_spiegel_bouwt(self) -> None:
        """Een afnemer die naar een ongevulde tag wijst, valt om op een manifest-404.

        De spiegel wordt met de hand gevuld (`task publish-mc`), dus die twee waarden lopen
        uit elkaar zodra iemand er een bijwerkt en de ander vergeet.
        """
        versies = {pad: _mc_version(pad) for pad in (MC_DOCKERFILE, DOCKERFILE, BACKUP_DOCKERFILE)}
        for pad, versie in versies.items():
            assert versie, f"{pad} pint geen MC_VERSION"
        assert len(set(versies.values())) == 1, f"de mc-tags lopen uiteen: {versies}"

    def test_de_spiegel_wordt_met_een_literale_checksum_gevuld(self) -> None:
        """De grendel is verhuisd naar images/mc, want daar wordt de release nog opgehaald.

        Waarom de som literaal staat en niet naast de binary opgehaald wordt: zie de kop van
        images/mc/Dockerfile. In de twee afnemers is hij niet meer nodig, die adresseren een
        image en krijgen de integriteitscontrole van het register.
        """
        lines = [line for _, line in _instructions(MC_DOCKERFILE.read_text())]

        assert not [line for line in lines if ".sha256sum" in line], (
            "de checksum wordt naast de binary opgehaald en komt dus van dezelfde bron"
        )
        sommen = {
            regel.group(1)
            for regel in (re.fullmatch(r"ARG (MC_SHA256\w*)=[0-9a-f]{64}", line) for line in lines)
            if regel
        }
        assert sommen, "er staat geen literale mc-checksum in het Dockerfile"

        haal = [line for line in lines if "github.com/minio/mc/releases/download" in line]
        assert haal, "de spiegel haalt mc nergens uit de GitHub-release"
        for line in haal:
            assert "sha256sum -c -" in line, f"mc wordt gehaald zonder checksumcontrole: {line}"
            # Per curl in de instructie en niet op de instructie als geheel: staan er ooit
            # weer twee, dan dekt een -f op de ene de andere af.
            zonder_f = [vlaggen for vlaggen in re.findall(r"curl\s+(-\S+)", line) if "f" not in vlaggen]
            assert not zonder_f, f"een mc-download zonder -f slaat een foutpagina op: curl {zonder_f} in {line}"

        controle = [line for line in lines if "sha256sum -c -" in line]
        for naam in sorted(sommen):
            assert any(naam in regel for regel in controle), f"de literale som {naam} wordt niet gecontroleerd"

        # En de andere kant op, want de lus hierboven ziet alleen wat er STAAT: een som die
        # de controle per arch opzoekt maar die hier geen literaal heeft, is een lege som.
        zonder_waarde = {naam for regel in controle for naam in re.findall(r"\$\{(MC_SHA256_\w+)\}", regel)} - sommen
        assert not zonder_waarde, f"deze som wordt opgezocht maar staat hier niet: {sorted(zonder_waarde)}"

    def test_de_spiegel_heeft_een_beschreven_weg_om_gevuld_te_worden(self) -> None:
        """Zonder die weg weet alleen degene die hem de eerste keer vulde hoe het moet."""
        taken = TASKFILE.read_text()
        assert "\n  publish-mc:\n" in taken, "er is geen task die de spiegel vult"
        publish = taken.split("\n  publish-mc:\n", 1)[1].split("\n  publish-", 1)[0]
        assert "images/mc" in publish, "publish-mc bouwt iets anders dan de spiegel"
        assert f"REGISTRY_IMAGE: {MC_IMAGE.rsplit('/', 1)[0]}" in publish

        uitleg = (MC_DOCKERFILE.parent / "README.md").read_text()
        assert "task publish-mc" in uitleg, "de weg om de spiegel te vullen noemt de task niet"


class TestAptLayers:
    def test_every_apt_install_uses_the_cache_mounts(self, instructions: list[tuple[str, str]]) -> None:
        """Without the mounts the package lists land in the layer and the download repeats."""
        installs = [line for _, line in instructions if line.upper().startswith("RUN ") and "apt-get install" in line]
        assert installs

        missing = [
            line
            for line in installs
            if "--mount=type=cache,target=/var/cache/apt" not in line
            or "--mount=type=cache,target=/var/lib/apt" not in line
        ]
        assert missing == []


# Elke image van derden onder deze paden noemt een volledige versie: drie of meer
# cijfergroepen met een optioneel achtervoegsel zoals -alpine of -rootless. Een tag die
# meebeweegt (`redis:7-alpine`, `registry:2`) geeft bij twee pulls een andere versie zonder
# dat er in git iets verandert, en Renovate kan hem niet bijwerken.
VERSIEPADEN = ("infrastructure", "bootstrap", "images", "scripts")
VOLLEDIGE_VERSIE = re.compile(r"v?\d+(\.\d+){2,}([.-][\w.-]+)?")
YAML_VERWIJZING = re.compile(r"^\s*-?\s*(?:image|imageName|newTag):\s*[\"\']?([^\"\'\s]+)[\"\']?\s*$", re.MULTILINE)
# De default van een *_IMAGE-variabele in een shellscript, met of zonder ${VAR:-...} eromheen.
SHELL_VERWIJZING = re.compile(r"^\w*IMAGE=[\"\']?(?:\$\{\w+:-)?([^\"\'}\s]+)", re.MULTILINE)

# Op naam, dus elke tag erop is toegestaan. Alleen voor een naam waarvan de tag per uitrol
# wisselt: een sleutel op naam en tag wordt dan stil rood zodra iets de tag herschrijft.
ZWEVENDE_NAMEN_TOEGESTAAN: dict[str, str] = {
    "localhost:5001/operations-manager": (
        "eigen image; task sandbox:configure-operations-manager-image zet de tag per uitrol op de commit-sha (RC-348)"
    ),
    "ghcr.io/minbzk/base-images/argocd-rig": "eigen image, de operator-CR pint de tag in het veld version:",
    "rcr.rijksapps.nl/ghcr-rig/minbzk/base-images/argocd-rig": (
        "eigen image, de operator-CR pint de tag in het veld version:"
    ),
}

# Op naam en tag, voor een verwijzing die geen volledige versie KAN dragen. Een image dat
# alleen nog niet gepind is hoort hier niet: dat is een gat met een reden ervoor.
ZWEVEND_TOEGESTAAN: dict[str, str] = {
    # Eigen images en lokale namen: dat gaat over `latest` op een eigen pad, en dat is RC-348.
    "operations-manager:latest": "eigen image, de overlay zet de tag (RC-348)",
    "rig-cmp-argo-kustomize-sops:latest": "eigen image (RC-348)",
    "ghcr.io/minbzk/base-images/rig-cmp-argo-kustomize-sops:latest": "eigen image (RC-348)",
    "ghcr.io/minbzk/base-images/minio:RELEASE.2025-07-23T15-54-02Z": (
        "eigen spiegel van de MinIO-release; RELEASE.<datum> is daar de volledige versie"
    ),
    "RELEASE.2025-07-23T15-54-02Z": "newTag van diezelfde MinIO-spiegel",
    "<IMAGE>": "placeholder in de voorbeeldtekst van task cr-snippet, wordt niet uitgerold",
    "quay.io/jupyter/minimal-notebook:2026-10-12": "datumtag, die vult jupyter eenmalig",
    "quay.io/argoproj/argocd": "de ArgoCD-CR pint de versie ernaast in /spec/repo/version, niet in de verwijzing",
    # CNPG nummert zijn image naar de PostgreSQL-release, dus twee cijfergroepen is daar de
    # volledige versie; de plain tag heeft geen datumstempel-variant zoals de bookworm-lijn.
    "ghcr.io/cloudnative-pg/postgresql:17.11": "CNPG heeft geen derde cijfergroep; 17.11 is daar de volledige versie",
    "rcr.rijksapps.nl/ghcr-rig/cloudnative-pg/postgresql:17.11": (
        "dezelfde image via de rcr-proxy, die de odcn-overlays patchen"
    ),
}


def _gevolgde_bestanden() -> list[Path]:
    uitvoer = subprocess.run(
        ["git", "ls-files", "-z", *VERSIEPADEN],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [REPO_ROOT / naam for naam in uitvoer.split("\0") if naam]


def _verwijzingen_in_bestand(bestand: Path) -> list[str]:
    """De images waar dit bestand naar wijst, zoals ze er staan."""
    if bestand.name.startswith("Dockerfile"):
        instructies = _instructions(bestand.read_text())
        fasen = {fase for fase, _ in instructies if fase}
        bases = [
            next(deel for deel in instructie.split()[1:] if not deel.startswith("--"))
            for _, instructie in instructies
            if instructie.upper().startswith("FROM ")
        ]
        return [basis for basis in bases if basis not in fasen and basis != "scratch"]
    if bestand.suffix in (".yaml", ".yml"):
        inhoud = bestand.read_text()
        if bestand.name.startswith("kustomization."):
            return YAML_VERWIJZING.findall(inhoud) + _patchwaarden(inhoud)
        return YAML_VERWIJZING.findall(inhoud)
    if bestand.suffix == ".sh":
        return SHELL_VERWIJZING.findall(bestand.read_text())
    return []


def _tag(verwijzing: str) -> str:
    """Een newTag staat er al als tag; een image-ref draagt hem achter de laatste dubbele punt."""
    if ":" not in verwijzing:
        return "" if "/" in verwijzing else verwijzing
    return verwijzing.rpartition(":")[2]


def _zweeft(verwijzing: str) -> bool:
    naam, _, draagt_digest = split_image_reference(verwijzing)
    if verwijzing in ZWEVEND_TOEGESTAAN or naam in ZWEVENDE_NAMEN_TOEGESTAAN:
        return False
    if draagt_digest:
        return False
    return not VOLLEDIGE_VERSIE.fullmatch(_tag(verwijzing))


def _pad(bestand: Path) -> str:
    """Het pad vanaf de repo-root, want de bestandsnaam alleen zegt vaak deployment.yaml."""
    return str(bestand.relative_to(REPO_ROOT)) if bestand.is_relative_to(REPO_ROOT) else str(bestand)


def _zwevende_images(bestanden: list[Path]) -> list[tuple[str, str]]:
    return [
        (_pad(bestand), verwijzing)
        for bestand in bestanden
        for verwijzing in _verwijzingen_in_bestand(bestand)
        if _zweeft(verwijzing)
    ]


class TestVasteVersies:
    """Elke image van derden onder infrastructure/, bootstrap/, images/ en scripts/ noemt een vaste versie.

    Vaste versie betekent hier: een volledige versietag, een digest, of een regel in een van
    de twee uitzonderingslijsten hierboven met de reden erbij. De meting per image en wat er
    nog niet gedekt is: features/renovate.md.
    """

    def test_geen_image_op_een_zwevende_tag(self) -> None:
        bestanden = _gevolgde_bestanden()
        assert bestanden, "git ls-files gaf geen bestanden"

        assert _zwevende_images(bestanden) == []

    def test_de_grendel_ziet_een_teruggezette_tag(self, tmp_path: Path) -> None:
        """Zonder deze toets zegt de test hierboven niets zodra de vorm van een bestand wijzigt."""
        bron = REPO_ROOT / "infrastructure/bootstrap/infrastructure/redis/controller/base/deployment.yaml"
        kopie = tmp_path / "deployment.yaml"
        kopie.write_text(bron.read_text().replace("redis:7.4.11-alpine", "redis:7-alpine"))

        assert _zwevende_images([bron]) == []
        assert _zwevende_images([kopie]) == [(str(kopie), "redis:7-alpine")] * 2

    def test_de_grendel_leest_de_image_van_een_shellscript(self, tmp_path: Path) -> None:
        """Zonder deze toets zegt scripts/ in VERSIEPADEN niets: shell is geen yaml en geen Dockerfile."""
        bron = REPO_ROOT / "scripts/setup-kind-registry.sh"
        assert _verwijzingen_in_bestand(bron) == ["registry:2.8.3"]

        kopie = tmp_path / "setup-kind-registry.sh"
        kopie.write_text(bron.read_text().replace("registry:2.8.3", "registry:2"))

        assert _zwevende_images([kopie]) == [(str(kopie), "registry:2")]

    def test_de_grendel_leest_de_imagename_van_een_cnpg_cluster(self, tmp_path: Path) -> None:
        """CNPG draagt zijn image in imageName; las de grendel alleen image:, dan bleef die tag ongezien."""
        bron = REPO_ROOT / "infrastructure/bootstrap/infrastructure/postgresql/database/base/cluster.yaml"
        kopie = tmp_path / "cluster.yaml"
        kopie.write_text(bron.read_text().replace("postgresql:17.11", "postgresql:17"))

        assert _zwevende_images([kopie]) == [(str(kopie), "ghcr.io/cloudnative-pg/postgresql:17")]

    def test_de_grendel_leest_de_patchwaarde_op_een_imagename(self, tmp_path: Path) -> None:
        """De odcn-overlays zetten imageName via een JSON6902-patch, en die staat niet op een image:-regel."""
        bron = (
            REPO_ROOT / "infrastructure/bootstrap/infrastructure/postgresql/database/overlays/odcn/kustomization.yaml"
        )
        kopie = tmp_path / "kustomization.yaml"
        kopie.write_text(bron.read_text().replace("postgresql:17.11", "postgresql:17"))

        zwevend = _zwevende_images([kopie])
        assert zwevend == [(str(kopie), "rcr.rijksapps.nl/ghcr-rig/cloudnative-pg/postgresql:17")]

    def test_een_naam_blijft_toegestaan_als_de_tag_herschreven_wordt(self) -> None:
        """De sandbox-tag van OPI wisselt per uitrol, dus de uitzondering staat op de naam."""
        assert not _zweeft("localhost:5001/operations-manager:8102b1a45")
        assert not _zweeft("localhost:5001/operations-manager:deadbee12")


class TestManifestImages:
    """De images in de manifesten die op ODCN terechtkomen, en in de back-upketen.

    Het gat dat dit dicht: TestPinnedTools dekt de twee Dockerfiles, maar niet de
    verwijzingen in de manifesten. Daar wees de SOPS-sidecar van ArgoCD naar `latest`.
    """

    def test_elke_productieverwijzing_is_gepind(self) -> None:
        """Een `latest`-tag of een verwijzing zonder tag wijst morgen naar iets anders."""
        verwijzingen = _productieverwijzingen()
        assert verwijzingen, "er is geen enkele image-verwijzing gevonden; volgt de toets de manifesten nog?"

        los = [paar for paar in verwijzingen if not _is_gepind(paar[1]) and paar not in UITZONDERINGEN]
        assert los == []

    def test_elke_uitzondering_staat_er_nog(self) -> None:
        """Anders houdt de lijst redenen vast voor regels die niet meer bestaan."""
        verwijzingen = set(_productieverwijzingen())

        verdwenen = [paar for paar in UITZONDERINGEN if paar not in verwijzingen]
        assert verdwenen == []

    def test_elke_uitzondering_heeft_een_reden(self) -> None:
        """Zonder reden is het geen uitzondering maar een gat."""
        zonder = [paar for paar, reden in UITZONDERINGEN.items() if not reden.strip()]
        assert zonder == []

    @pytest.mark.parametrize("sjabloon", BACKUP_SJABLONEN)
    def test_een_backupsjabloon_haalt_zijn_image_uit_de_setting(self, sjabloon: str) -> None:
        """Hard in het sjabloon kan geen cluster hem anders zetten, en de sandbox moet dat."""
        inhoud = (REPO_ROOT / "operations-manager/python/manifests" / sjabloon).read_text()

        regels = [regel.strip() for regel in inhoud.splitlines() if regel.strip().startswith("image:")]

        assert regels == ["image: {{ backup_image }}"]

    def test_de_standaard_van_backup_image_is_gepind(self) -> None:
        """Dit is de verwijzing die productie krijgt: de configmap daar zet hem niet."""
        assert _is_gepind(settings.BACKUP_IMAGE), settings.BACKUP_IMAGE


#: De overlays van infrastructuurcomponenten die op een ODCN-cluster terechtkomen. Daar kan
#: Docker Hub niet bij, en een image dat niet al in git naar de rcr-proxy wijst, wordt door
#: admission herschreven en blijft daarna eeuwig OutOfSync.
ODCN_COMPONENTOVERLAYS = (
    "infrastructure/bootstrap/infrastructure/seaweedfs/controller/overlays/odcn",
    "infrastructure/bootstrap/infrastructure/seaweedfs/controller/overlays/odcn-staging",
)

RCR = "rcr.rijksapps.nl/"


def _gerenderde_images(overlay: str) -> list[str]:
    """De images die kustomize voor deze overlay oplevert, niet wat de manifesten zeggen.

    Gerenderd en niet gegrepen, want het pad komt uit een `images:`-blok in de overlay en
    de digest uit de basis; alleen de uitkomst zegt of ze samenkomen, en een tweede
    container of initContainer komt zo mee zonder dat deze toets wordt bijgewerkt.

    Een CronJob zet zijn podspec een laag dieper (`jobTemplate`), dus die wordt apart
    opgezocht: zonder dat viel de lifecycle-ronde van seaweedfs stil buiten deze toets.
    """
    proc = subprocess.run(
        ["kustomize", "build", str(REPO_ROOT / overlay)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return [
        container["image"]
        for doc in yaml.safe_load_all(proc.stdout)
        if doc
        for spec in _podspecs(doc)
        for container in (spec.get("containers") or []) + (spec.get("initContainers") or [])
    ]


def _podspecs(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """De podspecs in een manifest: die van een workload, en die van een CronJob."""
    spec = doc.get("spec") or {}
    kandidaten = [
        (spec.get("template") or {}).get("spec"),
        ((spec.get("jobTemplate") or {}).get("spec", {}).get("template") or {}).get("spec"),
    ]
    return [podspec for podspec in kandidaten if podspec]


@pytest.mark.parametrize("overlay", ODCN_COMPONENTOVERLAYS)
class TestOdcnComponentImages:
    """Elk image in een ODCN-overlay van een infrastructuurcomponent komt van de rcr-proxy
    en staat op een digest.

    Het gat dat dit dicht: TestManifestImages volgt alleen de kustomization-wortels van
    `clusters/odcn` en toetst alleen DAT er een digest staat, niet het pad, en
    `odcn-staging` valt er helemaal buiten.
    """

    def test_elk_image_komt_van_de_rcr_proxy_en_staat_op_een_digest(self, overlay: str) -> None:
        if shutil.which("kustomize") is None:
            pytest.skip("kustomize ontbreekt")
        images = _gerenderde_images(overlay)
        assert images, f"{overlay} levert geen enkel image op; volgt de toets de overlay nog?"

        fout = [image for image in images if not image.startswith(RCR) or "@sha256:" not in image]
        assert fout == []
