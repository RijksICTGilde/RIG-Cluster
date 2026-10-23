"""De productie-image van de operations-manager draagt een tag die terug te zoeken is.

Deze toetsen lezen de Taskfile en een manifest, geen Python. Dat is met opzet: een tag die
naar bron wijst die niemand kan opzoeken gaat stil fout, en draait daarna maanden mee.
"""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

# Uit de Keycloak-toetsen, want het is dezelfde tag van hetzelfde script: `calver-tag.sh`
# schrijft voor beide images deze vorm. Een tweede regex ernaast zou stil kunnen afwijken.
from tests.test_keycloak_eigen_image import GEPUSHTE_TAG, _aangeroepen, _taak

REPO_ROOT = Path(__file__).resolve().parents[3]
TASKFILE = REPO_ROOT / "Taskfile.yaml"
BRON_TAG = REPO_ROOT / "scripts/bron-tag.sh"
CALVER_TAG = REPO_ROOT / "scripts/calver-tag.sh"
PRODUCTIE_PATCH = Path(
    "bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production/patches/deployment.yaml"
)

PUBLICEER = "publish-operations-manager"
PIN = "pin-operations-manager-image"

SCHONE_TAG = "2026.01.01.0000-deadbeef"
DIRTY_TAG = f"{SCHONE_TAG}-dirty"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _kopie(tmp_path: Path, met_remote: bool) -> Path:
    """Een git-repo met de echte Taskfile en de echte productie-overlay erin.

    De taken pinnen een bestand in hun eigen repo, dus het meten gebeurt op een kopie.
    `bron-tag.sh` en `calver-tag.sh` gaan mee omdat de Taskfile er zijn tags mee uitrekent
    (SANDBOX_OM_TAG is een globale var, dus zonder die scripts valt elke taak om), en
    `operations-manager/` omdat dat de bronpaden van dit image zijn.
    """
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy(BRON_TAG, repo / "scripts" / BRON_TAG.name)
    shutil.copy(CALVER_TAG, repo / "scripts" / CALVER_TAG.name)
    shutil.copy(TASKFILE, repo / TASKFILE.name)
    (repo / "operations-manager").mkdir()
    (repo / "operations-manager/bron.txt").write_text("bron\n")
    (repo / PRODUCTIE_PATCH.parent).mkdir(parents=True)
    shutil.copy(REPO_ROOT / PRODUCTIE_PATCH, repo / PRODUCTIE_PATCH)

    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "bron")

    if met_remote:
        remote = tmp_path / "remote.git"
        subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True, capture_output=True)
        _git(repo, "remote", "add", "origin", str(remote))
        _git(repo, "push", "-q", "origin", "main")
    return repo


def _guard() -> str:
    """Het eerste cmd van de publicatietaak: de twee controles voor de bouw."""
    cmd = _taak(PUBLICEER)["cmds"][0]
    assert isinstance(cmd, str), cmd
    return cmd


def _draai_guard(tag: str, repo: Path) -> subprocess.CompletedProcess[str]:
    """De guard draaien met de tag ingevuld: go-task vult die var in, bash niet."""
    return subprocess.run(
        ["bash", "-c", _guard().replace("{{.IMAGE_VERSION}}", tag)],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _draai_pin(repo: Path, tag: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["task", PIN, f"IMAGE_TAG={tag}"],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _gepinde_tag(patch: Path) -> str:
    deployment = yaml.safe_load(patch.read_text())
    image = deployment["spec"]["template"]["spec"]["containers"][0]["image"]
    # Sinds de pin ook een digest draagt is de vorm <repo>:<tag>@sha256:<hex>.
    return image.split("@")[0].split(":")[-1]


@pytest.fixture(scope="module")
def task_beschikbaar() -> None:
    if shutil.which("task") is None:
        pytest.skip("task ontbreekt")


class TestDePublicatieBouwtNietsDatNietTerugTeZoekenIs:
    """Gemeten door de guard te DRAAIEN: een verschoven case of een vergeten exit leest goed."""

    def test_een_dirty_tag_wordt_geweigerd(self, tmp_path: Path) -> None:
        """Was een waarschuwing, en publiceerde toch. De tag beschreef dan niet wat erin zat."""
        resultaat = _draai_guard(DIRTY_TAG, _kopie(tmp_path, met_remote=True))

        assert resultaat.returncode == 1, resultaat.stdout + resultaat.stderr
        assert "hoort niet op productie" in resultaat.stderr, resultaat.stderr

    def test_een_commit_die_op_geen_remote_staat_wordt_geweigerd(self, tmp_path: Path) -> None:
        """Een schone tag zegt niets zolang de commit alleen op deze machine bestaat."""
        resultaat = _draai_guard(SCHONE_TAG, _kopie(tmp_path, met_remote=False))

        assert resultaat.returncode == 1, resultaat.stdout + resultaat.stderr
        assert "geen enkele remote" in resultaat.stderr, resultaat.stderr

    def test_een_schone_gepushte_commit_mag_door(self, tmp_path: Path) -> None:
        """De guard moet de gewone gang niet in de weg zitten."""
        resultaat = _draai_guard(SCHONE_TAG, _kopie(tmp_path, met_remote=True))

        assert resultaat.returncode == 0, resultaat.stdout + resultaat.stderr

    def test_de_controles_staan_voor_de_bouw(self) -> None:
        """Weigeren na de bouw laat een image achter dat niemand mag pinnen.

        go-task breekt de keten af bij een mislukt commando, dus de ORDE is wat de bouw
        tegenhoudt.
        """
        cmds = _taak(PUBLICEER)["cmds"]

        assert cmds.index(_guard()) == 0, cmds
        assert _aangeroepen(PUBLICEER, "docker-build-and-push") is not None
        assert {"task": "version:generate"} in cmds


class TestDePinWeigertDezelfdeTag:
    """Publiceren en pinnen zijn losse aanroepen, dus de pin heeft zijn eigen weigering."""

    @pytest.mark.usefixtures("task_beschikbaar")
    def test_een_dirty_tag_komt_niet_in_de_overlay(self, tmp_path: Path) -> None:
        repo = _kopie(tmp_path, met_remote=True)
        voor = (repo / PRODUCTIE_PATCH).read_text()

        resultaat = _draai_pin(repo, DIRTY_TAG)

        assert resultaat.returncode != 0, resultaat.stdout
        assert "hoort niet op productie" in resultaat.stdout + resultaat.stderr
        assert (repo / PRODUCTIE_PATCH).read_text() == voor, "de overlay is toch geschreven"

    @pytest.mark.usefixtures("task_beschikbaar")
    def test_terugrollen_naar_een_schone_tag_blijft_werken(self, tmp_path: Path) -> None:
        """De weigering mag niet ook de weg naar een oudere, schone tag afsluiten."""
        repo = _kopie(tmp_path, met_remote=True)

        resultaat = _draai_pin(repo, SCHONE_TAG)

        assert resultaat.returncode == 0, resultaat.stdout + resultaat.stderr
        assert _gepinde_tag(repo / PRODUCTIE_PATCH) == SCHONE_TAG


class TestDeProductiepinIsEenGepusteCalVer:
    def test_de_overlay_draagt_geen_tag_die_niemand_gepusht_kan_hebben(self) -> None:
        """Zoals de Keycloak-basis: de tag hier wordt niet met de hand geschreven.

        Een `-dirty`-tag komt van bron die niet in git staat, en `latest` beweegt mee met de
        laatste publicatie. In beide gevallen is niet na te gaan wat er op productie draait.
        """
        tag = _gepinde_tag(REPO_ROOT / PRODUCTIE_PATCH)

        assert tag not in ("", "latest")
        assert not tag.endswith("-dirty"), tag
        assert GEPUSHTE_TAG.fullmatch(tag), f"{tag!r} heeft niet de vorm die de pintaak schrijft"
