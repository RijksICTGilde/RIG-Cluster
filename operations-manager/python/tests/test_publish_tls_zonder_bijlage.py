"""Een TLS-keuze die je zonder bijlage niet kunt waarmaken (RC-132).

Het gemeten geval: iemand wijzigt het webadres van deployment ``pr-19`` naar een eigen
domein, kiest in de certificaatstap 'Eigen certificaat op de ingress (aangeleverd)' --
wat het veld zelf aanraadt voor een eigen domein -- en loopt vast. ``tls: provided``
eist een ``attachment``, het project heeft geen bijlagen, en het bijlageveld dat naast
de keuze verschijnt biedt bij een lege catalogus geen enkele waarde. Een modus die je
kunt kiezen en niet kunt waarmaken, met een afkeuring in rauwe pydantic-uitvoer erbij.

Wat deze tests vasthouden:

* zonder bijlagen is 'provided' NIET te kiezen -- op beide lagen, en zichtbaar met de
  reden erbij in plaats van stil weggelaten;
* met een bijlage is het gewoon een keuze;
* elke optie die het formulier zonder bijlagen wel aanbiedt, levert een project op dat
  de validatie accepteert (de toets loopt door het FORMULIER, niet alleen langs het model);
* de afkeuring bevat geen pydantic-binnenwerk meer en zegt wat je moet doen;
* het webadres van EEN deployment wijzigen raakt de andere deployments niet, ook niet
  als die geen publish-on-web-configuratie hebben.
"""

from __future__ import annotations

import asyncio
import copy
import re
from typing import Any

import pytest
from opi.core.project_schema import ProjectIntegrityError
from opi.forms.editables.processor import EditableFormProcessor
from opi.forms.visualizers.flows import get_flow
from opi.forms.visualizers.providers import PublishTlsModeOptionsProvider, PublishTlsOverrideOptionsProvider
from opi.forms.wizard.save import guard_target_still_points_at_the_same_item
from opi.forms.wizard.state import WizardState
from opi.forms.wizard.write_set import apply_write_paths, flow_write_paths
from opi.manager.project_validation import validate_service_configs
from opi.web.router_detail_edit import _fully_owned_list_keys, _pad_sparse_submission
from opi.web.router_wizard import _create_renderer, _extract_section_data, _split_data_across_sections

#: De bijlage-inhoud is AGE-versleuteld in het bestand; de catalogus telt de envelop.
_AGE_BLOCK = "-----BEGIN AGE ENCRYPTED FILE-----\nx\n-----END AGE ENCRYPTED FILE-----"

#: De deployment die bewerkt wordt. Niet index 0: een fout in de doelkeuze valt alleen op
#: als het doel niet toevallig het eerste item is.
PR_19 = 2


def _project() -> dict[str, Any]:
    """Het gemeten project: een component op 'standard', drie deployments, geen bijlagen.

    ``production`` draagt ``services: null`` op zijn component -- de deployment die in de
    afkeuring genoemd werd en die zelf geen publish-on-web-configuratie heeft.
    """
    return {
        "name": "toets-hn7",
        "components": [{"name": "frontend", "services": [{"publish-on-web": {"config": {"tls": "standard"}}}]}],
        "deployments": [
            {"name": "production", "cluster": "c", "components": [{"reference": "frontend", "services": None}]},
            {"name": "test", "cluster": "c", "components": [{"reference": "frontend"}]},
            {"name": "pr-19", "cluster": "c", "components": [{"reference": "frontend"}]},
        ],
    }


def _with_attachment(project: dict[str, Any]) -> dict[str, Any]:
    project = copy.deepcopy(project)
    project["services"] = [
        {"attachments": {"data": [{"id": "eigen-cert", "filename": "cert.pem", "content": _AGE_BLOCK}]}}
    ]
    return project


def _option(options: list[dict[str, Any]], value: str) -> dict[str, Any]:
    return next(option for option in options if option["value"] == value)


# --- 1. 'provided' is niet te kiezen zonder bijlage ------------------------------------


def test_component_biedt_provided_uitgeschakeld_aan_zonder_bijlagen() -> None:
    options = PublishTlsModeOptionsProvider(yaml_data=_project()).get_options()

    assert [option["value"] for option in options] == ["standard", "passthrough", "provided"]
    provided = _option(options, "provided")
    assert provided["disabled"] is True
    assert "Bijlagen" in provided["label"]
    # De andere twee blijven gewoon kiesbaar.
    assert not _option(options, "standard").get("disabled")
    assert not _option(options, "passthrough").get("disabled")


def test_override_biedt_provided_uitgeschakeld_aan_zonder_bijlagen() -> None:
    """Dezelfde grendel op de per-deployment override; anders is de omweg de andere laag."""
    options = PublishTlsOverrideOptionsProvider(
        yaml_data=_project(),
        yaml_path=f"deployments[{PR_19}]/components[0]/services/publish-on-web/config/tls",
    ).get_options()

    assert options[0]["value"] == ""  # erven blijft de eerste keuze
    assert _option(options, "provided")["disabled"] is True


@pytest.mark.parametrize("provider", ["component", "override"])
def test_met_een_bijlage_is_provided_gewoon_te_kiezen(provider: str) -> None:
    project = _with_attachment(_project())
    if provider == "component":
        options = PublishTlsModeOptionsProvider(yaml_data=project).get_options()
    else:
        options = PublishTlsOverrideOptionsProvider(
            yaml_data=project,
            yaml_path=f"deployments[{PR_19}]/components[0]/services/publish-on-web/config/tls",
        ).get_options()

    provided = _option(options, "provided")
    assert not provided.get("disabled")
    assert "Bijlagen" not in provided["label"]


def test_zonder_project_blijven_alle_modi_staan() -> None:
    """Een kale render (geen projectcontext) gokt niet dat er geen bijlagen zijn."""
    options = PublishTlsModeOptionsProvider().get_options()
    assert [option["value"] for option in options] == ["standard", "passthrough", "provided"]
    assert not _option(options, "provided").get("disabled")


# --- 2. en dat is op het scherm ook zo -------------------------------------------------


def _render_cert_step(project: dict[str, Any]) -> str:
    section = get_flow(f"modal-edit-domain-{PR_19}").sections[1]
    return _create_renderer().render_fields_from_editables(
        editables=section.editables, yaml_data=project, layout=section.layout or [], edit_mode=True
    )


def _optie_regel(html: str, waarde: str) -> str:
    match = re.search(rf'<option value="{waarde}"[^>]*>[^<]*</option>', html)
    assert match is not None, f"optie {waarde!r} staat niet in het scherm"
    return match.group(0)


def test_het_scherm_toont_provided_uitgeschakeld_zonder_bijlagen() -> None:
    """Empirisch gerenderd: een ``disabled`` in de optiedict die het sjabloon niet doorzet
    is geen grendel."""
    html = _render_cert_step(_project())

    assert "disabled" in _optie_regel(html, "provided")
    assert "Bijlagen" in _optie_regel(html, "provided")
    assert "disabled" not in _optie_regel(html, "standard")


def test_het_scherm_laat_provided_staan_met_een_bijlage() -> None:
    html = _render_cert_step(_with_attachment(_project()))
    assert "disabled" not in _optie_regel(html, "provided")


# --- 3. door het formulier: geen keuze leidt nog tot een onopslaanbaar project ----------


class _Formulier:
    """De certificaatstap van modal-edit-domain-N, gedreven zoals de router hem drijft."""

    def __init__(self, project: dict[str, Any], index: int = PR_19) -> None:
        self.project = project
        self.index = index
        self.flow = get_flow(f"modal-edit-domain-{index}")
        self.section = self.flow.sections[1]
        werk = copy.deepcopy(project)
        self.state = WizardState(
            flow_id=self.flow.flow_id, current_step=self.section.section_id, project_name=project.get("name")
        )
        self.state.step_data = _split_data_across_sections(self.flow, werk)
        owned = _fully_owned_list_keys(self.flow)
        self.state.base_data = {k: v for k, v in werk.items() if k not in owned}
        self.state.active_sections = [section.section_id for section in self.flow.sections]

    def kies(self, tls: str, attachment: str | None = None) -> dict[str, str]:
        """Verstuur de stap met deze TLS-keuze; geeft de veldfouten terug.

        Het bijlageveld reist alleen mee als het zichtbaar is (``show_when`` op
        'provided'), precies zoals de browser het stuurt.
        """
        config: dict[str, Any] = {"tls": tls}
        if attachment is not None:
            config["attachment"] = attachment
        body = {
            "deployments": [
                {"components": [{"reference": "frontend", "services": {"publish-on-web": {"config": config}}}]}
            ]
        }
        padded = _pad_sparse_submission(body, self.flow, self.section.section_id)
        processor = EditableFormProcessor()
        submitted, errors = asyncio.run(
            processor.process_json_submission(
                padded, self.section.editables, self.state.get_merged_data(), edit_mode=True
            )
        )
        processor.clear_hidden_depends_on(self.section.editables, submitted)
        self.state.store_step_data(self.section.section_id, _extract_section_data(self.section.editables, submitted))
        return errors

    def opslaan(self) -> dict[str, Any]:
        """Wat er van dit formulier in het projectbestand terechtkomt."""
        merged = self.state.get_merged_data(strip_cleared=False)
        return apply_write_paths(
            copy.deepcopy(self.project), copy.deepcopy(merged), flow_write_paths(list(self.flow.sections))
        )


def _kiesbare_modi(project: dict[str, Any]) -> list[str]:
    options = PublishTlsOverrideOptionsProvider(
        yaml_data=project,
        yaml_path=f"deployments[{PR_19}]/components[0]/services/publish-on-web/config/tls",
    ).get_options()
    return [option["value"] for option in options if not option.get("disabled")]


@pytest.mark.parametrize("modus", ["", "standard", "passthrough"])
def test_elke_kiesbare_modus_levert_een_opslaanbaar_project(modus: str) -> None:
    """Wat het formulier zonder bijlagen aanbiedt, moet ook op te slaan zijn."""
    project = _project()
    assert modus in _kiesbare_modi(project)

    formulier = _Formulier(project)
    assert formulier.kies(modus) == {}
    validate_service_configs(formulier.opslaan())  # geen ProjectIntegrityError


def test_de_dode_keuze_is_geen_kiesbare_keuze_meer() -> None:
    """De toestand uit de melding: 'provided' zonder bijlage. Het formulier biedt hem niet
    meer aan, en de validatie blijft hem weigeren als hij er langs een andere weg komt."""
    project = _project()
    assert "provided" not in _kiesbare_modi(project)

    formulier = _Formulier(project)
    formulier.kies("provided", attachment="")
    with pytest.raises(ProjectIntegrityError):
        validate_service_configs(formulier.opslaan())


def test_met_een_bijlage_werkt_provided_wel() -> None:
    project = _with_attachment(_project())
    assert "provided" in _kiesbare_modi(project)

    formulier = _Formulier(project)
    assert formulier.kies("provided", attachment="eigen-cert") == {}
    opgeslagen = formulier.opslaan()
    validate_service_configs(opgeslagen)
    config = opgeslagen["deployments"][PR_19]["components"][0]["services"]["publish-on-web"]["config"]
    assert config == {"tls": "provided", "attachment": "eigen-cert"}


# --- 4. de melding: geen pydantic-binnenwerk, wel een uitweg ---------------------------


def _afkeuring(project: dict[str, Any]) -> str:
    with pytest.raises(ProjectIntegrityError) as fout:
        validate_service_configs(project)
    return str(fout.value)


def test_de_melding_draagt_geen_pydantic_binnenwerk() -> None:
    project = _project()
    project["deployments"][PR_19]["components"][0]["services"] = {"publish-on-web": {"config": {"tls": "provided"}}}

    melding = _afkeuring(project)

    assert "type=value_error" not in melding
    assert "input_value=" not in melding
    assert "input_type=" not in melding
    assert "errors.pydantic.dev" not in melding
    assert "Value error," not in melding
    assert "validation error" not in melding


def test_de_melding_zegt_wat_je_moet_doen() -> None:
    project = _project()
    project["deployments"][PR_19]["components"][0]["services"] = {"publish-on-web": {"config": {"tls": "provided"}}}

    melding = _afkeuring(project)

    assert "deployment 'pr-19'" in melding  # welke deployment het betreft
    assert "Standaard certificaat" in melding
    assert "Bijlagen" in melding


def test_de_melding_noemt_de_deployment_waar_de_waarde_staat() -> None:
    """De afkeuring wijst de deployment aan die de configuratie draagt, niet de eerste."""
    project = _project()
    project["deployments"][0]["components"][0]["services"] = {"publish-on-web": {"config": {"tls": "provided"}}}

    assert "deployment 'production'" in _afkeuring(project)


# --- 4b. een index-doel dat inmiddels een andere deployment aanwijst --------------------


def _doel_van_de_webadresflow() -> Any:
    return get_flow(f"modal-edit-domain-{PR_19}").target


def test_een_verschoven_lijst_weigert_de_opslag() -> None:
    """Het scherm werd geopend op index 2 = pr-19; als daar inmiddels een andere
    deployment staat, mag de bewerking daar niet in landen."""
    sessie = WizardState(flow_id=f"modal-edit-domain-{PR_19}", current_step="x")
    sessie.base_data = {"deployments": _project()["deployments"]}

    # Ondertussen is 'test' verdwenen: pr-19 schuift op, en op index 2 staat nu iets anders.
    verschoven = _project()
    verschoven["deployments"] = [
        verschoven["deployments"][0],
        verschoven["deployments"][2],
        {"name": "pr-20", "cluster": "c", "components": []},
    ]

    with pytest.raises(ProjectIntegrityError) as fout:
        guard_target_still_points_at_the_same_item(verschoven, sessie, _doel_van_de_webadresflow())

    melding = str(fout.value)
    assert "pr-19" in melding
    assert "pr-20" in melding
    assert "opnieuw" in melding


def test_een_ongewijzigde_lijst_laat_de_opslag_door() -> None:
    sessie = WizardState(flow_id=f"modal-edit-domain-{PR_19}", current_step="x")
    sessie.base_data = {"deployments": _project()["deployments"]}

    guard_target_still_points_at_the_same_item(_project(), sessie, _doel_van_de_webadresflow())


def test_zonder_lijst_in_de_sessie_wordt_er_niet_gegokt() -> None:
    """Een flow die de lijst niet in base_data draagt (een add, of een lijst die een
    editable volledig bezit) mag hier niet op stuklopen."""
    sessie = WizardState(flow_id=f"modal-edit-domain-{PR_19}", current_step="x")
    sessie.base_data = {}

    guard_target_still_points_at_the_same_item(_project(), sessie, _doel_van_de_webadresflow())


# --- 5. een webadres wijzigen raakt alleen die deployment ------------------------------


def test_wijzigen_van_een_deployment_laat_de_andere_ongemoeid() -> None:
    """Het gemeten geval nagebouwd: pr-19 bewerken terwijl production geen
    publish-on-web-configuratie heeft."""
    project = _project()
    formulier = _Formulier(project)
    assert formulier.kies("standard") == {}

    opgeslagen = formulier.opslaan()
    validate_service_configs(opgeslagen)

    assert opgeslagen["deployments"][PR_19]["components"][0]["services"] == {
        "publish-on-web": {"config": {"tls": "standard"}}
    }
    # De twee andere deployments staan er nog precies zoals ze stonden.
    assert opgeslagen["deployments"][0] == project["deployments"][0]
    assert opgeslagen["deployments"][1] == project["deployments"][1]


def test_een_onaangeroerde_certificaatstap_schrijft_geen_override() -> None:
    """Erven is de standaardwaarde: doorklikken zonder iets te kiezen mag geen
    override achterlaten, ook geen lege."""
    project = _project()
    formulier = _Formulier(project)
    assert formulier.kies("") == {}

    opgeslagen = formulier.opslaan()
    validate_service_configs(opgeslagen)
    assert opgeslagen["deployments"] == project["deployments"]
