"""De uitzonderingslijst van de container-gate: elke uitzondering heeft een reden en verloopt.

Zonder die twee is een uitzondering een stille vrijstelling: niemand weet later nog waarom
hij er staat, en de bevinding komt nooit meer terug. Zie features/security-scanning-pipeline.md.
"""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
UITZONDERINGEN = REPO_ROOT / ".trivyignore.yaml"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "security.yml"
POORT = REPO_ROOT / ".github" / "actions" / "image-gate" / "action.yml"

MAX_GELDIGHEID = timedelta(days=90)


def _bezwaren(uitzondering: dict[str, Any], vandaag: date) -> list[str]:
    """Wat er mis is met een uitzondering, leeg als hij in orde is."""
    bezwaren = []
    if not str(uitzondering.get("statement") or "").strip():
        bezwaren.append("geen statement, dus geen vastgelegde reden")
    vervalt = uitzondering.get("expired_at")
    if not isinstance(vervalt, date):
        bezwaren.append(f"geen expired_at als datum, gevonden: {vervalt!r}")
    elif vervalt - vandaag > MAX_GELDIGHEID:
        bezwaren.append(f"expired_at {vervalt} ligt verder dan {MAX_GELDIGHEID.days} dagen vooruit")
    bezwaren += _bezwaren_purls(uitzondering)
    return bezwaren


def _bezwaren_purls(uitzondering: dict[str, Any]) -> list[str]:
    """Een purls-scope hoort op naamniveau: met een versie erin stopt hij na de eerste bump."""
    purls = uitzondering.get("purls")
    if purls is None:
        return []
    if not isinstance(purls, list) or not purls:
        return [f"purls is leeg of geen lijst, gevonden: {purls!r}"]
    return [f"purl {purl!r} noemt een versie, scope op naamniveau zonder @" for purl in purls if "@" in str(purl)]


def _uitzonderingen() -> list[dict[str, Any]]:
    """Alle uitzonderingen uit het bestand, ongeacht onder welke soort ze staan."""
    inhoud = yaml.safe_load(UITZONDERINGEN.read_text()) or {}
    return [regel for soort in inhoud.values() for regel in (soort or [])]


def test_de_gate_verwijst_naar_de_uitzonderingslijst() -> None:
    stappen = yaml.safe_load(WORKFLOW.read_text())["jobs"]["container-scan"]["steps"]
    verwijzingen = [stap.get("with", {}).get("trivyignores") for stap in stappen]
    assert UITZONDERINGEN.name in verwijzingen, (
        f"geen stap in container-scan die {UITZONDERINGEN.name} meegeeft, de lijst doet dan niets"
    )
    assert UITZONDERINGEN.is_file(), f"{UITZONDERINGEN} ontbreekt, terwijl de gate ernaar verwijst"


def test_de_trivy_stappen_pinnen_hun_versie() -> None:
    """Draait de gate een andere scanner dan de meting, dan onderbouwt die meting niets."""
    stappen = yaml.safe_load(WORKFLOW.read_text())["jobs"]["container-scan"]["steps"]
    trivy = [stap for stap in stappen if "trivy-action" in str(stap.get("uses", ""))]
    assert trivy, "geen trivy-action in container-scan"
    zonder = [stap["name"] for stap in trivy if not stap.get("with", {}).get("version")]
    assert not zonder, f"trivy-stappen zonder version, die draaien de default van de action: {zonder}"


def test_de_poort_voor_de_push_draait_dezelfde_scanner() -> None:
    """De poort in de publicatiepad en de scan in security.yml meten tegen dezelfde lijst.

    Lopen hun versies uiteen, dan onderbouwt de meting in de kop van .trivyignore.yaml maar
    een van de twee, en kan de een rood zijn waar de ander groen staat.
    """
    in_workflow = yaml.safe_load(WORKFLOW.read_text())["env"]["TRIVY_SCANNER_VERSION"]
    in_poort = yaml.safe_load(POORT.read_text())["inputs"]["trivy-version"]["default"]
    assert in_poort == in_workflow, f"poort draait {in_poort}, security.yml {in_workflow}"


def test_de_poort_geeft_de_uitzonderingslijst_mee() -> None:
    stappen = yaml.safe_load(POORT.read_text())["runs"]["steps"]
    trivy = [stap for stap in stappen if "trivy-action" in str(stap.get("uses", ""))]
    assert trivy, "geen trivy-action in de poort"
    zonder = [stap["name"] for stap in trivy if stap["with"].get("trivyignores") != UITZONDERINGEN.name]
    assert not zonder, f"trivy-stappen in de poort zonder {UITZONDERINGEN.name}: {zonder}"


def test_elke_uitzondering_heeft_een_reden_en_verloopt() -> None:
    vandaag = datetime.now(tz=UTC).date()
    fouten = [
        f"{regel.get('id', '<zonder id>')}: {bezwaar}"
        for regel in _uitzonderingen()
        for bezwaar in _bezwaren(regel, vandaag)
    ]
    assert not fouten, "uitzonderingen zonder reden of met een te ruime vervaldatum:\n" + "\n".join(fouten)


@pytest.mark.parametrize(
    ("uitzondering", "verwacht"),
    [
        ({"id": "CVE-2026-0001", "statement": "niet bereikbaar"}, "expired_at"),
        ({"id": "CVE-2026-0001", "statement": "niet bereikbaar", "expired_at": "binnenkort"}, "expired_at"),
        ({"id": "CVE-2026-0001", "expired_at": date(2026, 11, 16)}, "statement"),
        ({"id": "CVE-2026-0001", "statement": "  ", "expired_at": date(2026, 11, 16)}, "statement"),
        (
            {"id": "CVE-2026-0001", "statement": "niet bereikbaar", "expired_at": date(2027, 6, 1)},
            "verder dan 90 dagen",
        ),
        (
            {
                "id": "CVE-2026-0001",
                "purls": ["pkg:deb/ubuntu/openssl@3.5.5-1ubuntu3.3"],
                "statement": "niet bereikbaar",
                "expired_at": date(2026, 11, 16),
            },
            "noemt een versie",
        ),
        (
            {"id": "CVE-2026-0001", "purls": [], "statement": "niet bereikbaar", "expired_at": date(2026, 11, 16)},
            "purls is leeg",
        ),
    ],
)
def test_een_onvolledige_uitzondering_valt_om(uitzondering: dict[str, Any], verwacht: str) -> None:
    bezwaren = _bezwaren(uitzondering, date(2026, 10, 17))
    assert any(verwacht in bezwaar for bezwaar in bezwaren), f"geen bezwaar over {verwacht!r}, wel: {bezwaren}"
