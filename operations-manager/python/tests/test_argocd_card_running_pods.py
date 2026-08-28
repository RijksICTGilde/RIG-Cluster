"""De kaart vertelt WAT ER DRAAIT, voordat hij het alarm laat lezen (RC-162).

Bij psd-law/pr-114 (productie, 21 augustus 2026) stond er "Degraded" met "Applicatie
crasht herhaaldelijk", terwijl de pod uit ReplicaSet 849d475c4 sinds 18 augustus gewoon
verkeer bediende. De kaart las alleen ArgoCD, en die weet niet welke pod bedient.

Twee vormen, en het verschil ertussen is de hele reden dat dit blok bestaat:

- er draait iets -> een gewone regel, want een mislukte uitrol is geen storing;
- er draait niets -> een rode melding, want dan LIGT de applicatie eruit.
"""

from __future__ import annotations

from typing import Any

from opi.core.templates_lotc import templates_lotc as templates
from opi.services.deployment_diagnostics import ComponentPodSummary
from opi.services.deployment_state import collect_deployment_state
from opi.services.services import ServiceAdapter

TEMPLATE = "bg/_argocd-deployment-card.html.j2"
CLUSTER = "odcn-production"

#: Waar een <c-alert type="error"> op uitkomt in de NLDD-markup. Op het GERENDEERDE
#: attribuut getoetst en niet op de bron: dat een melding ROOD is, is het gedrag hier -
#: "de applicatie ligt eruit" mag niet als een gewone mededeling langskomen.
ROOD = 'variant="critical"'

BRON_IMAGE = (
    "ghcr.io/minbzk/moza-profiel-service@sha256:25ab6344a1b2c3d4e5f60718293a4b5c6d7e8f901234567890abcdef123456789"
)


def _pod_regel(html: str) -> str:
    """De regel die zegt wat er draait, uit de gerenderde pagina."""
    for regel in html.splitlines():
        if "draait sinds" in regel:
            return regel.strip()
    raise AssertionError("geen draairegel gevonden")


def _render(pods: list[ComponentPodSummary], errors: list[dict[str, Any]] | None = None) -> str:
    project_data = {
        "name": "psd-law",
        "deployments": [
            {
                "name": "pr-114",
                "cluster": CLUSTER,
                "components": [{"reference": "profielservice", "image": BRON_IMAGE}],
            }
        ],
    }
    deployment = project_data["deployments"][0]
    return templates.env.get_template(TEMPLATE).render(
        deployment=deployment,
        project={"name": project_data["name"]},
        argocd_status={
            "pr-114": {
                "available": True,
                "health": "Degraded",
                "sync": "Synced",
                "errors": errors or [],
                "deviations": [],
                "pods": pods,
            }
        },
        current_cluster=CLUSTER,
        deployment_states={"pr-114": collect_deployment_state(project_data, "pr-114")},
        ServiceAdapter=ServiceAdapter,
    )


class TestEenPodDieBedient:
    def _summary(self, **overrides: Any) -> ComponentPodSummary:
        velden: dict[str, Any] = {
            "reference": "profielservice",
            "is_serving": True,
            "pod_name": "pr-114-profielservice-849d475c4-4qp6p",
            "image": BRON_IMAGE,
            "running_since": "2026-08-18T11:59:12Z",
            "configured_image": BRON_IMAGE,
            "runs_configured_image": True,
        }
        velden.update(overrides)
        return ComponentPodSummary(**velden)

    def test_de_regel_noemt_component_datum_en_image(self):
        html = _render([self._summary()])

        assert "profielservice draait sinds" in html
        # De datum in Nederlandse notatie en in Amsterdamse tijd - 11:59 UTC is 13:59 hier.
        assert "18 augustus 2026 13:59" in html
        assert "ghcr.io/minbzk/moza-profiel-service@sha256:25ab6344a1b2" in html

    def test_de_digest_wordt_afgekort(self):
        """De volle 64 tekens passen niet op een regel en zeggen niet meer.

        Op de REGEL gemeten en niet op de hele pagina: de knop "Logs bekijken" krijgt de
        componentlijst als JSON mee en draagt de volle image daarom sowieso.
        """
        regel = _pod_regel(_render([self._summary()]))
        assert regel.endswith("op ghcr.io/minbzk/moza-profiel-service@sha256:25ab6344a1b2")

    def test_de_proxyvorm_van_de_registry_staat_er_niet_in(self):
        """De gebruiker kent zijn eigen registry; rcr.rijksapps.nl is een platformdetail.

        De samenvatting levert de bronvorm al aan; deze toets bewaakt dat de kaart hem
        niet alsnog ergens vandaan haalt.
        """
        html = _render([self._summary(image=BRON_IMAGE, configured_image=BRON_IMAGE)])
        assert "rcr.rijksapps.nl" not in html

    def test_een_draaiende_pod_is_geen_foutmelding(self):
        html = _render([self._summary()])
        assert "Er draait niets" not in html
        assert ROOD not in html

    def test_een_andere_image_krijgt_een_zin_erachter(self):
        html = _render(
            [
                self._summary(
                    configured_image="ghcr.io/minbzk/moza-profiel-service@sha256:2c0728edaaaabbbb",
                    runs_configured_image=False,
                )
            ]
        )
        assert "niet de image die in je projectbestand staat" in html
        assert "sha256:2c0728edaaaa" in html

    def test_zonder_verdict_komt_er_geen_zin_over_een_andere_image(self):
        """Een digest tegenover een tag: ongelijkheid zegt daar niets, dus geen uitspraak."""
        html = _render(
            [
                self._summary(
                    configured_image="ghcr.io/minbzk/moza-profiel-service:2.1",
                    runs_configured_image=None,
                )
            ]
        )
        assert "niet de image die in je projectbestand staat" not in html
        assert "profielservice draait sinds" in html


class TestGeenPodDieBedient:
    def test_er_draait_niets_rendert_als_fout(self):
        """De Recreate-stand: de oude pod is weg en de nieuwe komt niet omhoog."""
        html = _render(
            [
                ComponentPodSummary(
                    reference="profielservice",
                    is_serving=False,
                    configured_image=BRON_IMAGE,
                )
            ]
        )

        assert "Er draait niets voor profielservice" in html
        assert ROOD in html
        assert "niet bereikbaar" in html


class TestReikwijdte:
    def test_een_kaart_zonder_podsamenvatting_toont_het_blok_niet(self):
        """Een gezonde kaart vraagt de pods niet op, en dan hoort er ook niets te staan."""
        html = _render([])
        assert "draait sinds" not in html
        assert "Er draait niets" not in html
