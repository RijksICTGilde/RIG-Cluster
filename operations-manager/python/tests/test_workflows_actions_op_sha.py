"""De workflows pinnen hun actions op een commit-SHA en verdelen de schrijfrechten per job.

Een action op een verplaatsbare tag kan onder diezelfde tag van inhoud veranderen
(OpenSSF Scorecard "Pinned-Dependencies"), en een token met schrijfrecht hoort alleen in
de job die het nodig heeft ("Token-Permissions").
"""

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
GITHUB_DIR = REPO_ROOT / ".github"
WORKFLOW_DIR = GITHUB_DIR / "workflows"
ACTION_DIR = GITHUB_DIR / "actions"
DOCKER_WORKFLOW = WORKFLOW_DIR / "docker.yml"

# De SHA, plus het versiecommentaar dat Renovate nodig heeft om hem bij te werken.
GEPINDE_ACTION = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}\s+# \S+$")


def _workflows() -> list[Path]:
    """De workflows plus de composite actions: ook daar staan externe uses die een pin horen."""
    paden = sorted(p for p in WORKFLOW_DIR.iterdir() if p.suffix in {".yml", ".yaml"})
    assert paden, "geen workflows gevonden in .github/workflows"
    return paden + sorted(ACTION_DIR.glob("*/action.yml"))


def _uses_regels(pad: Path) -> list[tuple[int, str]]:
    regels = []
    for nummer, regel in enumerate(pad.read_text().splitlines(), 1):
        match = re.search(r"uses:\s*(.+?)\s*$", regel)
        if match and not match[1].startswith("./"):
            regels.append((nummer, match[1]))
    return regels


@pytest.mark.parametrize("workflow", _workflows(), ids=lambda p: str(p.relative_to(GITHUB_DIR)))
def test_externe_actions_staan_op_een_sha_met_versiecommentaar(workflow: Path) -> None:
    naam = workflow.relative_to(GITHUB_DIR)
    losse = [f"{naam}:{nummer}: {uses}" for nummer, uses in _uses_regels(workflow) if not GEPINDE_ACTION.match(uses)]
    assert not losse, "action zonder 40-teken SHA of zonder versiecommentaar:\n" + "\n".join(losse)


def test_alleen_de_tag_job_mag_naar_de_repo_schrijven() -> None:
    """De build-job pusht de image, de tag-job zet de git-tag. Alleen die laatste schrijft."""
    workflow = yaml.safe_load(DOCKER_WORKFLOW.read_text())
    assert workflow["permissions"] == {"contents": "read"}
    schrijvers = {
        naam for naam, job in workflow["jobs"].items() if job.get("permissions", {}).get("contents") == "write"
    }
    assert schrijvers == {"tag"}, f"contents: write staat bij {sorted(schrijvers)}"


ALLEEN_MAIN = "github.ref == 'refs/heads/main'"


def test_alleen_main_pusht_een_image() -> None:
    """De poort bouwt wel, de push staat erachter en komt alleen op main aan de beurt.

    Dezelfde guard als de job die tekent, zodat een dispatch op een tak geen ongetekende
    image achterlaat.
    """
    workflow = yaml.safe_load(DOCKER_WORKFLOW.read_text())
    stappen = workflow["jobs"]["build"]["steps"]
    pushers = [s for s in stappen if "push" in str(s.get("with", {}).get("outputs", ""))]
    assert pushers, "geen stap in build die een image pusht"
    zonder = [s["name"] for s in pushers if s.get("if") != ALLEEN_MAIN]
    assert not zonder, f"pushende stap zonder guard op main: {zonder}"

    publish = workflow["jobs"]["publish"]
    assert publish["if"] == ALLEEN_MAIN, f"de job die tekent hangt niet aan main: {publish['if']}"
    assert any("image-sign" in str(s.get("uses", "")) for s in publish["steps"]), "de job publish tekent niet"


OIDC_RECHTEN = {"id-token", "attestations"}


@pytest.mark.parametrize("workflow", _workflows(), ids=lambda p: str(p.relative_to(GITHUB_DIR)))
def test_een_job_met_een_oidc_token_draait_nooit_op_een_pull_request(workflow: Path) -> None:
    """Op een pull request draait de definitie van de PR-head, met de rechten van de job.

    Staat id-token: write dan bij een job die ook op een PR draait, dan kan een extra stap
    in die PR met dat token een willekeurige image keyless tekenen op naam van deze repo,
    en het certificaat noemt dan een PR-ref. Daarom: of de workflow kent geen
    pull_request-trigger, of de job hangt aan main.
    """
    inhoud = yaml.safe_load(workflow.read_text())
    if "jobs" not in inhoud:
        pytest.skip("composite action, geen workflow met jobs")
    op_pull_request = "pull_request" in (inhoud.get("on") or inhoud.get(True) or {})
    if not op_pull_request:
        return

    los = [
        naam
        for naam, job in inhoud["jobs"].items()
        if OIDC_RECHTEN & {r for r, w in (job.get("permissions") or {}).items() if w == "write"}
        and ALLEEN_MAIN not in str(job.get("if", ""))
    ]
    assert not los, f"{workflow.name}: job met een OIDC-token zonder guard op main: {los}"


def test_een_herbouw_verbruikt_geen_calver_en_geen_git_tag() -> None:
    """Een dispatch is dezelfde herbouw als de nachtelijke run: nightly-tag, geen git-tag.

    Zonder deze guard zet een handmatige run een CalVer-nummer en een git-tag voor iets wat
    niemand heeft gemerged.
    """
    workflow = yaml.safe_load(DOCKER_WORKFLOW.read_text())
    herbouw = workflow["jobs"]["build"]["env"]["HERBOUW"]
    assert "schedule" in herbouw, f"HERBOUW kent de nachtelijke run niet: {herbouw}"
    assert "workflow_dispatch" in herbouw, f"HERBOUW kent de dispatch niet: {herbouw}"

    calver = next(s for s in workflow["jobs"]["build"]["steps"] if s.get("id") == "calver")
    assert "${HERBOUW}" in calver["run"], "de calver-stap kijkt niet naar HERBOUW"
    assert "nightly-" in calver["run"], "een herbouw krijgt geen nightly-tag"

    verwacht = f"{ALLEEN_MAIN} && github.event_name == 'push'"
    assert workflow["jobs"]["tag"]["if"] == verwacht, f"de git-tag hangt niet alleen aan een merge: {verwacht}"
