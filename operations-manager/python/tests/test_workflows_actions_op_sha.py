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
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
DOCKER_WORKFLOW = WORKFLOW_DIR / "docker.yml"

# De SHA, plus het versiecommentaar dat Renovate nodig heeft om hem bij te werken.
GEPINDE_ACTION = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}\s+# \S+$")


def _workflows() -> list[Path]:
    paden = sorted(p for p in WORKFLOW_DIR.iterdir() if p.suffix in {".yml", ".yaml"})
    assert paden, "geen workflows gevonden in .github/workflows"
    return paden


def _uses_regels(pad: Path) -> list[tuple[int, str]]:
    regels = []
    for nummer, regel in enumerate(pad.read_text().splitlines(), 1):
        match = re.search(r"uses:\s*(.+?)\s*$", regel)
        if match and not match[1].startswith("./"):
            regels.append((nummer, match[1]))
    return regels


@pytest.mark.parametrize("workflow", _workflows(), ids=lambda p: p.name)
def test_externe_actions_staan_op_een_sha_met_versiecommentaar(workflow: Path) -> None:
    losse = [
        f"{workflow.name}:{nummer}: {uses}" for nummer, uses in _uses_regels(workflow) if not GEPINDE_ACTION.match(uses)
    ]
    assert not losse, "action zonder 40-teken SHA of zonder versiecommentaar:\n" + "\n".join(losse)


def test_alleen_de_tag_job_mag_naar_de_repo_schrijven() -> None:
    """De build-job pusht de image, de tag-job zet de git-tag. Alleen die laatste schrijft."""
    workflow = yaml.safe_load(DOCKER_WORKFLOW.read_text())
    assert workflow["permissions"] == {"contents": "read"}
    schrijvers = {
        naam for naam, job in workflow["jobs"].items() if job.get("permissions", {}).get("contents") == "write"
    }
    assert schrijvers == {"tag"}, f"contents: write staat bij {sorted(schrijvers)}"


def test_een_pull_request_pusht_geen_image() -> None:
    stap = next(
        s
        for s in yaml.safe_load(DOCKER_WORKFLOW.read_text())["jobs"]["build"]["steps"]
        if s.get("name") == "Build and push"
    )
    assert stap["with"]["push"] == "${{ github.event_name != 'pull_request' }}"
