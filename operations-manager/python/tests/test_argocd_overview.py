"""De gebundelde ArgoCD-bevraging achter de statuskolom (RC-76).

De statuskolom van de deploymenttabel moet er zijn - een statusoverzicht zonder status is
geen overzicht - maar niet ten koste van een bevraging per rij. Deze module haalt de LIJST
op en pikt daar de applicaties van dit project uit.

Wat deze toetsen bewaken:

1. TWINTIG RIJEN, EEN BEVRAGING. Dat is de hele reden dat deze module bestaat; als hij
   ooit stilletjes naar een aanroep per deployment terugvalt, hoort dat hier stuk te gaan.
2. De korte cache spaart een STOOT verzoeken uit en niet meer dan dat: na de vervaltijd
   wordt er opnieuw opgehaald, want een verouderde "Healthy" is erger dan geen status.
3. Een deployment die ArgoCD niet kent levert een stand op die dat zegt, geen KeyError.
"""

from __future__ import annotations

from typing import Any

import pytest
from opi.services import argocd_overview


class _ArgoDubbel:
    """Een ArgoCD-connector die telt hoe vaak de lijst is opgehaald."""

    def __init__(self, applications: list[dict[str, Any]], *, verbonden: bool = True) -> None:
        self.auth_token = "voorbeeld-token" if verbonden else None
        self._applications = applications
        self.aanroepen = 0

    async def list_applications(self) -> list[dict[str, Any]]:
        self.aanroepen += 1
        return self._applications


def _applicatie(naam: str, health: str = "Healthy", sync: str = "Synced") -> dict[str, Any]:
    return {
        "metadata": {"name": naam},
        "status": {
            "health": {"status": health},
            "sync": {"status": sync, "revision": "abcdef1234567890"},
            "operationState": {"finishedAt": "2026-08-12T10:00:00Z"},
        },
    }


@pytest.fixture(autouse=True)
def _schone_cache():
    argocd_overview.clear_cache()
    yield
    argocd_overview.clear_cache()


@pytest.fixture
def argo(monkeypatch):
    """Een dubbel dat elke deployment van het project kent."""
    dubbel = _ArgoDubbel([_applicatie(f"demo-dep-{nummer}") for nummer in range(20)])
    monkeypatch.setattr(argocd_overview, "create_argo_connector", lambda: dubbel)
    return dubbel


async def test_twintig_deployments_kosten_een_bevraging(argo) -> None:
    """De reden dat deze module bestaat, en dus de toets die moet blijven staan."""
    namen = [f"dep-{nummer}" for nummer in range(20)]

    standen = await argocd_overview.get_project_argocd_statuses("demo", namen)

    assert len(standen) == 20
    assert argo.aanroepen == 1


async def test_de_stand_leest_gezondheid_sync_en_laatste_sync(monkeypatch) -> None:
    dubbel = _ArgoDubbel([_applicatie("demo-productie", health="Degraded", sync="OutOfSync")])
    monkeypatch.setattr(argocd_overview, "create_argo_connector", lambda: dubbel)

    standen = await argocd_overview.get_project_argocd_statuses("demo", ["productie"])

    assert standen["productie"]["health"] == "Degraded"
    assert standen["productie"]["sync"] == "OutOfSync"
    assert standen["productie"]["last_sync"] == "2026-08-12T10:00:00Z"
    assert standen["productie"]["revision"] == "abcdef1"


async def test_een_deployment_die_argocd_niet_kent_is_niet_beschikbaar(monkeypatch) -> None:
    """Anders zou de tabel "nog niets uitgerold" niet van "we weten het niet" kunnen
    onderscheiden - of erger, omvallen op een ontbrekende sleutel."""
    dubbel = _ArgoDubbel([])
    monkeypatch.setattr(argocd_overview, "create_argo_connector", lambda: dubbel)

    standen = await argocd_overview.get_project_argocd_statuses("demo", ["productie"])

    assert standen["productie"]["available"] is False


async def test_zonder_verbinding_komt_er_niets_terug(monkeypatch) -> None:
    """De pagina zegt zelf al dat ArgoCD niet verbonden is; hier een verzonnen "Unknown"
    per rij van maken zou dat overschreeuwen."""
    dubbel = _ArgoDubbel([], verbonden=False)
    monkeypatch.setattr(argocd_overview, "create_argo_connector", lambda: dubbel)

    assert await argocd_overview.get_project_argocd_statuses("demo", ["productie"]) == {}
    assert dubbel.aanroepen == 0


async def test_een_project_zonder_deployments_bevraagt_niets(argo) -> None:
    assert await argocd_overview.get_project_argocd_statuses("demo", []) == {}
    assert argo.aanroepen == 0


# --------------------------------------------------------------------------- de cache


async def test_een_tweede_render_binnen_de_vervaltijd_hergebruikt(argo) -> None:
    """De stoot die dit opvangt: de pagina openen, verversen, tabblad wisselen."""
    await argocd_overview.get_project_argocd_statuses("demo", ["dep-1"])
    await argocd_overview.get_project_argocd_statuses("demo", ["dep-1"])

    assert argo.aanroepen == 1


async def test_na_de_vervaltijd_wordt_er_opnieuw_opgehaald(argo, monkeypatch) -> None:
    """Een verouderde "Healthy" is erger dan geen status; de cache vangt een stoot op en
    houdt niets langer vast dan de vastgelegde vervaltijd."""
    verstreken = [0.0]
    monkeypatch.setattr(argocd_overview.time, "monotonic", lambda: verstreken[0])

    await argocd_overview.get_project_argocd_statuses("demo", ["dep-1"])
    verstreken[0] = argocd_overview.CACHE_TTL_SECONDS + 1
    await argocd_overview.get_project_argocd_statuses("demo", ["dep-1"])

    assert argo.aanroepen == 2


async def test_een_nieuwe_deployment_wordt_niet_uit_de_cache_beantwoord(argo) -> None:
    """De bewaarde stand kent hem niet, en dan is hij geen antwoord op deze vraag."""
    await argocd_overview.get_project_argocd_statuses("demo", ["dep-1"])

    standen = await argocd_overview.get_project_argocd_statuses("demo", ["dep-1", "dep-2"])

    assert argo.aanroepen == 2
    assert set(standen) == {"dep-1", "dep-2"}


async def test_twee_projecten_delen_hun_stand_niet(monkeypatch) -> None:
    dubbel = _ArgoDubbel([_applicatie("een-productie"), _applicatie("twee-productie", health="Degraded")])
    monkeypatch.setattr(argocd_overview, "create_argo_connector", lambda: dubbel)

    een = await argocd_overview.get_project_argocd_statuses("een", ["productie"])
    twee = await argocd_overview.get_project_argocd_statuses("twee", ["productie"])

    assert een["productie"]["health"] == "Healthy"
    assert twee["productie"]["health"] == "Degraded"
