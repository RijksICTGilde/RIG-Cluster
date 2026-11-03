"""Elke job begint met een harden-runner op egress-policy: block.

Zonder die eerste stap kan een gecompromitteerde action of buildstap ongemerkt naar buiten
praten; de SHA-pins van de actions houden alleen tegen dat de inhoud verandert, niet wat een
stap doet zodra hij draait. Zie features/security-scanning-pipeline.md.
"""

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"

HARDEN = "step-security/harden-runner"

# Tijdelijk audit en niet block: in block weigert harden-runner ook domeinen uit de allowlist,
# en dan lopen jobs vast (step-security/harden-runner#708). Terug naar "block" zodra dat is
# opgelost; de allowlist blijft daarom staan en gevuld.
EGRESS_POLICY = "audit"


def _jobs() -> list[tuple[str, str, dict]]:
    paden = sorted(p for p in WORKFLOW_DIR.iterdir() if p.suffix in {".yml", ".yaml"})
    assert paden, "geen workflows gevonden in .github/workflows"
    return [(pad.name, naam, job) for pad in paden for naam, job in yaml.safe_load(pad.read_text())["jobs"].items()]


def _allowlist(workflow: str) -> list[str]:
    """De endpoints uit env.HARDEN_ENDPOINTS van die workflow."""
    env = yaml.safe_load((WORKFLOW_DIR / workflow).read_text()).get("env") or {}
    return [regel.strip() for regel in (env.get("HARDEN_ENDPOINTS") or "").splitlines() if regel.strip()]


ALLE_JOBS = _jobs()
IDS = [f"{workflow}:{naam}" for workflow, naam, _ in ALLE_JOBS]


@pytest.mark.parametrize(("workflow", "naam", "job"), ALLE_JOBS, ids=IDS)
def test_elke_job_begint_met_een_harden_runner(workflow: str, naam: str, job: dict) -> None:
    eerste = job["steps"][0]
    assert HARDEN in str(eerste.get("uses", "")), f"{workflow}:{naam} begint met {eerste.get('uses') or eerste}"


@pytest.mark.parametrize(("workflow", "naam", "job"), ALLE_JOBS, ids=IDS)
def test_de_egress_staat_op_de_afgesproken_policy_met_een_allowlist(workflow: str, naam: str, job: dict) -> None:
    mee = job["steps"][0].get("with", {})
    assert mee.get("egress-policy") == EGRESS_POLICY, f"{workflow}:{naam} staat niet op {EGRESS_POLICY}"
    assert mee.get("allowed-endpoints") == "${{ env.HARDEN_ENDPOINTS }}", (
        f"{workflow}:{naam} krijgt zijn allowlist niet uit env.HARDEN_ENDPOINTS"
    )


@pytest.mark.parametrize("workflow", sorted({w for w, _, _ in ALLE_JOBS}))
def test_de_allowlist_is_gevuld_en_heeft_poorten(workflow: str) -> None:
    """Een endpoint zonder poort matcht niets, en dan blokkeert block stil wat hij moet doorlaten."""
    endpoints = _allowlist(workflow)
    assert endpoints, f"{workflow} heeft geen env.HARDEN_ENDPOINTS"
    zonder = [e for e in endpoints if ":" not in e or not e.rsplit(":", 1)[1].isdigit()]
    assert not zonder, f"{workflow}: endpoint zonder poortnummer: {zonder}"
