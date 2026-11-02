"""Approval decisions (approved/denied) may only be set by router_approvals.py.

Tests two layers: (1) form path rejects platform-managed fields like the API,
and (2) store rejects decisions unless marked with approval_decision=True (RC-286).
"""

from __future__ import annotations

import ast
import copy
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import AsyncMock

import pytest
from opi.core.project_schema import ProjectIntegrityError
from opi.forms.editables.enforcers import DomainConfigEnforcer
from opi.forms.editables.processor import EditableFormProcessor
from opi.forms.visualizers.flows import get_flow
from opi.forms.wizard.mutation import apply_services_mutation
from opi.forms.wizard.resolver import resolve_active_sections
from opi.forms.wizard.save import apply_modal_edit
from opi.forms.wizard.state import WizardState
from opi.manager.project_validation import validate_project_structure
from opi.services.approvals import ensure_approval_requests
from opi.services.services import service_entry_config, service_entry_name
from opi.web.router_detail_edit import _fully_owned_list_keys
from opi.web.router_wizard import _extract_section_data, _split_data_across_sections

OPI = Path(__file__).resolve().parents[1] / "opi"

PROJECT_NAME = "aanvrager"
FLOW_ID = "modal-edit-services"
SECTION_ID = "services-edit"

#: Het domein dat het project aanvraagt, en dat niemand heeft goedgekeurd.
DOMEIN = "mijn-app.nl"


def _project(domains: dict[str, Any] | None = None) -> dict[str, Any]:
    """Een project dat een eigen domein claimt, met de opgeslagen goedkeuringstoestand."""
    publish_config: dict[str, Any] = {}
    if domains is not None:
        publish_config["domains"] = domains
    return {
        "schema-version": 2,
        "name": PROJECT_NAME,
        "display-name": "Aanvrager",
        "users": [{"email": "admin@rijksoverheid.nl", "roles": ["admin", "ops"]}],
        "clusters": ["sandboxed-local"],
        "services": [{"name": "publish-on-web", "config": publish_config}],
        "components": [{"name": "frontend", "type": "single", "services": ["publish-on-web"]}],
        "deployments": [
            {
                "name": "productie",
                "cluster": "sandboxed-local",
                "namespace": PROJECT_NAME,
                "services": [
                    {
                        "reference": "publish-on-web",
                        "config": {
                            "domain-format": "subdomain",
                            "base-domain": DOMEIN,
                            "subdomain": "app",
                        },
                    }
                ],
                "components": [{"reference": "frontend", "image": "example.invalid/frontend:1.0"}],
            }
        ],
    }


def _domains(status: str, subdomain_status: str | None = None) -> dict[str, Any]:
    """Het opgeslagen goedkeuringsblok van publish-on-web."""
    block: dict[str, Any] = {"allowed-domains": [{"domain": DOMEIN, "status": status}]}
    if subdomain_status is not None:
        block["allowed-subdomains"] = [{"domain": DOMEIN, "subdomains": [{"name": "app", "status": subdomain_status}]}]
    return block


def _publish_config(project_data: dict[str, Any]) -> dict[str, Any]:
    entry = next(e for e in project_data["services"] if service_entry_name(e) == "publish-on-web")
    return service_entry_config(entry) or {}


def _domain_status(project_data: dict[str, Any]) -> str | None:
    """De opgeslagen status van ``DOMEIN``, of None als er geen aanvraag op staat."""
    for entry in _publish_config(project_data).get("domains", {}).get("allowed-domains", []):
        if entry.get("domain") == DOMEIN:
            return entry.get("status")
    return None


async def _save_through_the_services_form(
    project_data: dict[str, Any],
    posted_services: list[Any],
) -> dict[str, Any]:
    """De hele weg van de services-selectie: post, verwerking, merge, opslagvorm.

    Hetzelfde pad als ``modal_wizard_submit_step`` + ``_modal_do_submit`` in
    ``router_detail_edit``, op platte dicts. ``posted_services`` is wat de browser stuurt:
    een lijst met namen, waarin een entry ook een dict met config kan zijn.
    """
    flow = get_flow(FLOW_ID)
    processor = EditableFormProcessor()
    for section in flow.sections:
        processor.populate_deferred_fields(project_data, section.editables)

    state = WizardState(
        flow_id=FLOW_ID,
        current_step=SECTION_ID,
        project_name=PROJECT_NAME,
        step_data=_split_data_across_sections(flow, project_data),
    )
    state.populate_virt_mappings(flow.sections)
    state.active_sections = [s.section_id for s in flow.sections]
    owned = _fully_owned_list_keys(flow)
    state.base_data = {k: v for k, v in copy.deepcopy(project_data).items() if k not in owned}

    section = next(s for s in flow.sections if s.section_id == SECTION_ID)
    submitted, errors = await processor.process_json_submission(
        {"services": posted_services},
        section.editables,
        state.get_merged_data(),
        edit_mode=True,
        enforcer_context={"project_name": PROJECT_NAME},
    )
    assert errors == {}
    processor.clear_hidden_depends_on(section.editables, submitted)
    apply_services_mutation(section.editables, state.get_merged_data(), submitted)
    state.store_step_data(SECTION_ID, _extract_section_data(section.editables, submitted))

    active_sections = list(resolve_active_sections(flow, state.step_data))
    state.active_sections = [s.section_id for s in active_sections]
    return await apply_modal_edit(
        copy.deepcopy(project_data),
        state.get_merged_data(strip_cleared=False),
        flow=flow,
        active_sections=active_sections,
        state=state,
        project_name=PROJECT_NAME,
    )


class TestHetFormulierSchrijftGeenPlatformVeld:
    """Laag 1: wat de API met een 422 weigert, schrijft het formulier ook niet."""

    async def test_een_meegestuurde_goedkeuring_haalt_het_projectbestand_niet(self) -> None:
        """Er komt geen status op het domein: het formulier schrijft dit veld niet.

        En daarna is het een AANVRAAG, niet een besluit: ``ensure_approval_requests`` is de
        pas die elke schrijfweg wel mag doen, en die zet hem op ``requested``.
        """
        saved = await _save_through_the_services_form(
            _project(),
            [{"publish-on-web": {"config": {"domains": _domains("approved")}}}],
        )
        assert _domain_status(saved) is None

        ensure_approval_requests(saved)
        assert _domain_status(saved) == "requested"

    async def test_een_opgeslagen_goedkeuring_blijft_staan(self) -> None:
        """De andere helft: een besluit dat er WEL is, mag een opslag niet kwijtmaken."""
        saved = await _save_through_the_services_form(_project(_domains("approved")), ["publish-on-web"])

        assert _domain_status(saved) == "approved"

    async def test_een_opgeslagen_goedkeuring_is_niet_te_overschrijven(self) -> None:
        """Afwijzen door te posten kan net zo min als goedkeuren."""
        saved = await _save_through_the_services_form(
            _project(_domains("approved")),
            [{"publish-on-web": {"config": {"domains": _domains("denied")}}}],
        )

        assert _domain_status(saved) == "approved"

    async def test_de_dienstgoedkeuring_van_send_email_net_zo(self) -> None:
        """Dezelfde regel op een ander platform-managed veld: ``send-email.approval``.

        Hier staat de uitkomst meteen op ``requested``: de service legt zijn eigen aanvraag
        vast in de ``post_merge`` van zijn configuratiestap, en die draait NA de guard. Dat
        is precies waarom de guard op de formuliergegevens staat en niet op de uitkomst:
        anders had hij de aanvraag van het platform zelf teruggedraaid.
        """
        saved = await _save_through_the_services_form(
            _project(),
            [
                "publish-on-web",
                {"send-email": {"config": {"from-name": "Afzender", "approval": {"status": "approved"}}}},
            ],
        )

        entry = next(e for e in saved["services"] if service_entry_name(e) == "send-email")
        config = service_entry_config(entry) or {}
        assert config["from-name"] == "Afzender"  # de instelling die de gebruiker WEL mag zetten
        assert config["approval"]["status"] == "requested"


@pytest.fixture(autouse=True)
def _geen_subdomeinregister(monkeypatch: pytest.MonkeyPatch) -> None:
    """De domeinenforcer die ``validate_project_structure`` draait, wil het subdomeinregister.

    Dat is een echte database en zegt niets over deze regel, dus de beschikbaarheidstoets
    staat hier uit, zoals in ``test_bare_domain_platform_gate``.
    """
    monkeypatch.setattr(DomainConfigEnforcer, "_check_subdomain_availability", AsyncMock())


class TestDeStoreWeigertEenBesluitZonderMarkering:
    """Laag 2: het punt waar elke schrijfweg langskomt."""

    async def test_van_aangevraagd_naar_goedgekeurd_is_geweigerd(self) -> None:
        previous = _project(_domains("requested"))
        with pytest.raises(ProjectIntegrityError, match="goedkeuringsstatus"):
            await validate_project_structure(_project(_domains("approved")), previous=previous)

    async def test_met_de_markering_mag_het(self) -> None:
        previous = _project(_domains("requested"))
        await validate_project_structure(_project(_domains("approved")), previous=previous, approval_decision=True)

    async def test_afwijzen_zonder_markering_is_net_zo_geweigerd(self) -> None:
        previous = _project(_domains("requested"))
        with pytest.raises(ProjectIntegrityError, match="goedkeuringsstatus"):
            await validate_project_structure(_project(_domains("denied")), previous=previous)

    async def test_een_nieuw_domein_dat_meteen_goedgekeurd_aankomt_ook(self) -> None:
        """Niet alleen een sprong vanaf ``requested``: een besluit uit niets is er ook een."""
        with pytest.raises(ProjectIntegrityError, match="goedkeuringsstatus"):
            await validate_project_structure(_project(_domains("approved")), previous=_project())

    async def test_een_subdomeinbesluit_ook(self) -> None:
        previous = _project(_domains("approved", subdomain_status="requested"))
        with pytest.raises(ProjectIntegrityError, match="goedkeuringsstatus"):
            await validate_project_structure(
                _project(_domains("approved", subdomain_status="approved")), previous=previous
            )

    async def test_een_status_die_blijft_staan_gaat_gewoon_door(self) -> None:
        """Elke gewone opslag van een project met een goedgekeurd domein blijft werken."""
        previous = _project(_domains("approved"))
        changed = _project(_domains("approved"))
        changed["display-name"] = "Andere naam"
        await validate_project_structure(changed, previous=previous)

    async def test_een_nieuwe_aanvraag_gaat_gewoon_door(self) -> None:
        """``requested`` is geen besluit: dat mag elke schrijfweg vastleggen."""
        await validate_project_structure(_project(_domains("requested")), previous=_project())


class TestAlleenDeGoedkeuringsrouteMarkeert:
    """De sweep over alle aanroepen: een tweede markeerder haalt de guard net zo stil weg."""

    #: Waar de markering hoort te staan, en hoe vaak.
    VERWACHTE_MARKEERDERS: ClassVar[set[tuple[str, int]]] = {("web/router_approvals.py", 1)}

    @staticmethod
    def _save_calls() -> list[tuple[str, ast.Call]]:
        """Elke ``<iets>.save_and_commit_project(...)``-aanroep in opi/, met zijn pad."""
        gevonden: list[tuple[str, ast.Call]] = []
        for pad in sorted(OPI.rglob("*.py")):
            boom = ast.parse(pad.read_text())
            gevonden.extend(
                (str(pad.relative_to(OPI)), knoop)
                for knoop in ast.walk(boom)
                if isinstance(knoop, ast.Call)
                and isinstance(knoop.func, ast.Attribute)
                and knoop.func.attr == "save_and_commit_project"
            )
        return gevonden

    def test_er_zijn_opslagaanroepen_om_te_meten(self) -> None:
        """Zonder deze regel zou een hernoeming deze hele toets stil groen laten."""
        assert len(self._save_calls()) >= 20

    def test_precies_een_aanroep_meldt_zich_als_besluit(self) -> None:
        markeerders: dict[str, int] = {}
        for pad, call in self._save_calls():
            vlag = next((kw.value for kw in call.keywords if kw.arg == "approval_decision"), None)
            if vlag is None:
                continue
            assert isinstance(vlag, ast.Constant), f"{pad}: approval_decision hoort een letterlijke True te zijn"
            assert vlag.value is True, f"{pad}: approval_decision hoort een letterlijke True te zijn"

            markeerders[pad] = markeerders.get(pad, 0) + 1

        assert set(markeerders.items()) == self.VERWACHTE_MARKEERDERS
