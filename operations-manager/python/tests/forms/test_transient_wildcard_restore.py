"""Transients met een wildcardpad herstellen per item, in plaats van te crashen (RC-388).

``_render_step_html`` in de wizard zet de waarde van een afhankelijke terug in het
transiente veld (``deployments[*]/backup/schedule:time`` uit ``.../schedule``). Met een
``[*]`` in het pad was dat een ValueError in ``set_value``: de deployments-stap van de
bewerkwizard gaf een 500 op elk project met een backup-schema of een base-domain.
Gemeten op de sandbox, voor deze reparatie.
"""

from __future__ import annotations

from types import SimpleNamespace

from opi.forms.visualizers.wizard_sections import DEPLOYMENTS_SECTION
from opi.web.router_wizard import _render_step_html


def _request() -> SimpleNamespace:
    """Het kleinste dat _render_step_html van een verzoek leest (als test_backup_schedule_flow)."""
    return SimpleNamespace(query_params={}, cookies={})


def _data() -> dict:
    return {
        "deployments": [
            {
                "name": "prod",
                "backup": {"schedule": "FREQ=WEEKLY;BYDAY=MO"},
                "services": [{"reference": "publish-on-web", "config": {"base-domain": "rijksapp.dev"}}],
            }
        ]
    }


def test_de_deployments_stap_rendert_met_een_schema_en_een_base_domain() -> None:
    """Voor de reparatie eindigde dit in ``Cannot use wildcard [*] in set_value``."""
    html = _render_step_html(_request(), DEPLOYMENTS_SECTION, yaml_data=_data())
    assert html
    assert "Wekelijks" in html


def test_zonder_afhankelijke_verandert_er_niets() -> None:
    data = {"deployments": [{"name": "prod"}]}
    _render_step_html(_request(), DEPLOYMENTS_SECTION, yaml_data=data)
    assert data == {"deployments": [{"name": "prod"}]}
