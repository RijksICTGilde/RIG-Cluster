"""Proof that a subdomain field error actually renders at the subdomain input.

Fix A keys the "niet beschikbaar" error to the subdomain field's own path. This
verifies the render pipeline surfaces that message in the section HTML, instead
of silently anchoring it to the invisible deployment-group path.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.responses import HTMLResponse
from opi.forms.editables.enforcers import DomainConfigEnforcer, FieldError
from opi.forms.visualizers.flows import build_domain_edit_flow
from opi.forms.visualizers.wizard_sections import COMPONENTS_SECTION, build_domain_section
from opi.services.catalog.publish_on_web.domain_config import DomainSetting, domain_setting_path
from opi.web import router_detail_edit, router_wizard
from opi.web.router_detail_edit import _create_renderer, _render_section_html


@pytest.mark.asyncio
async def test_subdomain_field_error_appears_in_rendered_html():
    section = build_domain_section(1, edit_mode=True)
    yaml_data = {
        "deployments": [
            {"name": "main"},
            {
                "name": "stable",
                "services": [
                    {
                        "reference": "publish-on-web",
                        "config": {
                            "base-domain": "rijksapp.dev",
                            "domain-format": "component.subdomain",
                            "subdomain": "moza",
                        },
                    }
                ],
            },
        ],
    }
    message = "Het subdomein 'moza' voor domein 'rijksapp.dev' is niet beschikbaar, in gebruik door project 'mozad-dle'"

    html = _render_section_html(
        section,
        yaml_data,
        errors={domain_setting_path(DomainSetting.SUBDOMAIN, 1): [message]},
        locked_services=None,
    )

    # The message is surfaced in the rendered field HTML (not swallowed).
    assert "niet beschikbaar" in html
    assert "mozad-dle" in html


@pytest.mark.asyncio
async def test_reserved_subdomain_error_appears_in_rendered_html(monkeypatch):
    """De reserveringsmelding kwam uit de veldvalidator en hangt nu aan de enforcer. Hij
    hoort nog steeds bij het subdomeinveld te renderen, niet op het groepspad.

    Pad en tekst komen uit de enforcer zelf: een met de hand ingevulde melding zou hier
    blijven renderen ook als de enforcer hem ergens anders aan hangt.
    """
    monkeypatch.setattr("opi.core.config.settings", type("S", (), {"CLUSTER_MANAGER": "odcn-production"})())

    section = build_domain_section(1, edit_mode=True)
    yaml_data = {
        "deployments": [
            {"name": "main"},
            {
                "name": "stable",
                "services": [
                    {
                        "reference": "publish-on-web",
                        "config": {
                            "base-domain": "rijks.app",
                            "domain-format": "subdomain",
                            "subdomain": "admin",
                        },
                    }
                ],
            },
        ],
    }

    with pytest.raises(FieldError) as exc_info:
        await DomainConfigEnforcer(deployment_index=1).enforce(yaml_data, {"project_name": "test-project"})

    assert exc_info.value.field_path == domain_setting_path(DomainSetting.SUBDOMAIN, 1)

    html = _render_section_html(
        section,
        yaml_data,
        errors={exc_info.value.field_path: [str(exc_info.value)]},
        locked_services=None,
    )

    assert "Subdomein &#39;admin&#39; is niet beschikbaar" in html


class TestAnErrorWithoutAFieldGoesToTheGeneralBar:
    """Een melding op een pad dat dit scherm niet tekent verdwijnt, en dat is precies hoe
    de knop stuk leek.

    Een enforcer die een gewone ``ValueError`` heft landt op het pad van de GROEP
    (``deployments[1]``), en dat is een container, geen invoerveld: "klikken op 'Volgende'
    werkt gewoon niet" (RIG-Cluster#179).
    """

    def test_the_group_path_renders_nothing(self):
        """De aanleiding, eerst gemeten: het scherm toont de melding niet."""
        section = build_domain_section(1, edit_mode=True)
        html = _render_section_html(
            section,
            {"deployments": [{"name": "main"}, {"name": "stable"}]},
            errors={"deployments[1]": ["Kaal domein is alleen beschikbaar voor een eigen domein"]},
            locked_services=None,
        )

        assert "Kaal domein" not in html

    def test_it_is_taken_out_and_handed_back(self):
        section = build_domain_section(1, edit_mode=True)
        errors = {"deployments[1]": ["Kaal domein is alleen beschikbaar voor een eigen domein"]}

        orphans = _create_renderer().take_unrendered_errors(
            section.editables,
            {"deployments": [{"name": "main"}, {"name": "stable"}]},
            errors,
            edit_mode=True,
        )

        assert orphans == ["Kaal domein is alleen beschikbaar voor een eigen domein"]
        assert errors == {}

    def test_a_real_field_keeps_its_error(self):
        """De tegenkant: een melding die WEL een veld heeft blijft er hangen.

        Het subdomeinveld wordt alleen getekend bij een formaat dat er een vraagt, dus
        dezelfde sleutel is in de ene stand een veldfout en in de andere een melding
        zonder veld.
        """
        section = build_domain_section(1, edit_mode=True)
        field = domain_setting_path(DomainSetting.SUBDOMAIN, 1)
        errors = {field: ["Een subdomein is vereist voor het gekozen URL-formaat"]}
        yaml_data = {
            "deployments": [
                {"name": "main"},
                {
                    "name": "stable",
                    "services": [
                        {
                            "reference": "publish-on-web",
                            "config": {"base-domain": "rijksapp.dev", "domain-format": "subdomain"},
                        }
                    ],
                },
            ],
        }

        orphans = _create_renderer().take_unrendered_errors(section.editables, yaml_data, errors, edit_mode=True)

        assert orphans == []
        assert list(errors) == [field]

    def test_two_errors_are_sorted_one_by_one(self):
        """Beide soorten in één inzending: alleen de melding zonder veld gaat naar de balk.

        Eén sonde per pad, niet één voor de hele inzending: anders maakt het getekende
        subdomeinveld ook het groepspad "getekend" en verdwijnt die melding opnieuw.
        """
        section = build_domain_section(1, edit_mode=True)
        field = domain_setting_path(DomainSetting.SUBDOMAIN, 1)
        errors = {
            "deployments[1]": ["Kaal domein is alleen beschikbaar voor een eigen domein"],
            field: ["Een subdomein is vereist voor het gekozen URL-formaat"],
        }
        yaml_data = {
            "deployments": [
                {"name": "main"},
                {
                    "name": "stable",
                    "services": [
                        {
                            "reference": "publish-on-web",
                            "config": {"base-domain": "rijksapp.dev", "domain-format": "subdomain"},
                        }
                    ],
                },
            ],
        }

        orphans = _create_renderer().take_unrendered_errors(section.editables, yaml_data, errors, edit_mode=True)

        assert orphans == ["Kaal domein is alleen beschikbaar voor een eigen domein"]
        assert list(errors) == [field]


class TestDeModalStapZetHemInDeBalk:
    """Dezelfde weg als in productie: de stap die niet verdergaat moet zeggen waarom.

    De enforcer van de GROEP heft hier een gewone ``ValueError`` ("Een aangepast domein is
    geselecteerd maar niet ingevuld"): de tweede melding die op deze manier onzichtbaar
    was.
    """

    @staticmethod
    def _submission() -> dict:
        """Een eigen domein gekozen, maar het invulveld leeg gelaten.

        Eén element, zoals de browser hem stuurt: json-enc klapt de ijle array dicht en
        ``_pad_sparse_submission`` zet hem terug op index 1.
        """
        return {
            "deployments": [
                {
                    "_services-config": [
                        {
                            "reference": "publish-on-web",
                            "config": {"base-domain": "__custom__", "domain-format": "subdomain", "subdomain": "web"},
                        }
                    ]
                }
            ]
        }

    @pytest.mark.asyncio
    async def test_de_melding_komt_in_global_errors(self):
        flow = build_domain_edit_flow(1)
        state = MagicMock()
        state.flow_id = flow.flow_id
        state.base_data = {}
        state.get_merged_data.return_value = {"name": "demo", "deployments": [{"name": "main"}, {"name": "stable"}]}

        request = MagicMock()
        request.headers.get.return_value = "application/json"
        request.json = AsyncMock(return_value=self._submission())

        gezien: dict = {}

        def _vang(*args, **kwargs):
            gezien.update(kwargs)
            return "<div></div>"

        with (
            patch.object(router_detail_edit, "require_project_edit_access"),
            patch.object(router_detail_edit, "_get_wizard_token", return_value="token"),
            patch.object(router_detail_edit, "get_modal_state_by_token", return_value=state),
            patch.object(router_detail_edit, "get_flow", return_value=flow),
            patch.object(router_detail_edit, "_render_modal_step", side_effect=_vang),
        ):
            await router_detail_edit.modal_wizard_submit_step(request, "demo", flow.flow_id, "domain-edit-1")

        assert gezien["global_errors"] == ["Een aangepast domein is geselecteerd maar niet ingevuld"]
        assert gezien["errors"] == {}, "de melding hoort niet ook nog op het groepspad te blijven staan"


class TestEenRijFoutInEenReeksBlijftStaan:
    """De valkuil die de browsertoets ving: een reeks tekent zijn rijen, de kaart niet.

    ``_build_fields_from_editables`` zet alleen de REEKS zelf in zijn kaart
    (``components``); elk veld van een rij hangt als kind onder die reeks. Een
    vergelijking op de sleutels van die kaart hield ``components[0]/name`` daarom voor
    een pad zonder veld, en haalde de verplicht-melding van de componentenstap weg.
    """

    def test_de_verplicht_melding_van_een_component_wordt_niet_weggehaald(self):
        errors = {"components[0]/name": ["Dit veld is verplicht"]}

        orphans = _create_renderer().take_unrendered_errors(COMPONENTS_SECTION.editables, {"components": [{}]}, errors)

        assert orphans == []
        assert list(errors) == ["components[0]/name"]

    def test_een_rij_die_er_niet_is_heeft_ook_geen_veld(self):
        """De tegenkant: een melding op een rij die niemand tekent heeft geen veld."""
        errors = {"components[7]/name": ["Dit veld is verplicht"]}

        orphans = _create_renderer().take_unrendered_errors(COMPONENTS_SECTION.editables, {"components": [{}]}, errors)

        assert orphans == ["Dit veld is verplicht"]
        assert errors == {}


class TestDeWizardstapZetHemInDeBalk:
    """Dezelfde melding, op de andere route die hem moet tonen.

    De aanmaakwizard en de bewerkmodal lopen allebei door ``take_unrendered_errors``; de
    vormtoets die hier stond ("pad eindigt op ``]``") liet een virtueel dienstpad zonder
    veld gewoon verdwijnen.
    """

    @staticmethod
    def _submission() -> dict:
        """Een eigen domein gekozen, het invulveld leeg gelaten."""
        return {
            "deployments": [
                {
                    "_services-config": [
                        {
                            "reference": "publish-on-web",
                            "config": {"base-domain": "__custom__", "domain-format": "subdomain", "subdomain": "web"},
                        }
                    ]
                }
            ],
            "_goto": "next",
        }

    @pytest.mark.asyncio
    async def test_de_melding_komt_in_global_errors(self):
        state = MagicMock()
        state.flow_id = "create-project"
        state.project_name = None
        state.is_edit = False
        state.get_merged_data.return_value = {"name": "demo", "deployments": [{"name": "main"}]}

        request = MagicMock()
        request.json = AsyncMock(return_value=self._submission())

        gezien: dict = {}

        def _vang(*args, **kwargs):
            gezien.update(kwargs)
            return {}

        with (
            patch.object(router_wizard, "get_wizard_state", return_value=state),
            patch.object(router_wizard, "_render_step_html", return_value="<div></div>"),
            patch.object(router_wizard, "_build_step_context", side_effect=_vang),
            patch.object(router_wizard, "_step_response", return_value=HTMLResponse("")),
        ):
            await router_wizard.submit_step(request, "create-project", "domains")

        assert gezien["global_errors"] == ["Een aangepast domein is geselecteerd maar niet ingevuld"]
        assert gezien["errors"] == {}, "de melding hoort niet ook nog op het groepspad te blijven staan"


class TestDeKaalDomeinMeldingBereiktDeGebruiker:
    """De melding moet niet alleen een veld hebben, hij moet ook op het scherm komen.

    Dit is de route uit de melding zelf (``modal-edit-domain-1``). Een ``FieldWarning``
    houdt de stap niet tegen, dus hij reist mee naar de volgende stap, waar
    ``_render_modal_step`` alle veldwaarschuwingen samenvoegt tot de "Let op"-balk.
    """

    @staticmethod
    def _submission() -> dict:
        """Een eigen domein, het kale domein aangevinkt, en nog geen aanvraag."""
        return {
            "deployments": [
                {
                    "base-domain:custom": "uitbetrouwbarebron.nl",
                    "_services-config": [
                        {
                            "reference": "publish-on-web",
                            "config": {
                                "base-domain": "__custom__",
                                "domain-format": "component-deployment-project",
                                "expose-component-on-bare-domain": "frontend",
                            },
                        }
                    ],
                }
            ]
        }

    @pytest.mark.asyncio
    async def test_de_waarschuwing_gaat_mee_naar_het_scherm(self):
        flow = build_domain_edit_flow(1)
        state = MagicMock()
        state.flow_id = flow.flow_id
        state.base_data = {}
        state.get_merged_data.return_value = {
            "name": "demo",
            "components": [{"name": "frontend"}],
            "deployments": [{"name": "main"}, {"name": "stable", "components": [{"reference": "frontend"}]}],
        }

        request = MagicMock()
        request.headers.get.return_value = "application/json"
        request.json = AsyncMock(return_value=self._submission())

        gezien: dict = {}

        def _vang(*args, **kwargs):
            gezien.update(kwargs)
            return "<div></div>"

        with (
            patch.object(router_detail_edit, "require_project_edit_access"),
            patch.object(router_detail_edit, "_get_wizard_token", return_value="token"),
            patch.object(router_detail_edit, "get_modal_state_by_token", return_value=state),
            patch.object(router_detail_edit, "get_flow", return_value=flow),
            patch.object(router_detail_edit, "_render_modal_step", side_effect=_vang),
            patch.object(router_detail_edit, "save_modal_state_by_token"),
            patch("opi.forms.editables.enforcers.get_supported_base_domains", return_value={"rijksapp.dev"}),
            patch.object(DomainConfigEnforcer, "_check_bare_domain_availability", new=AsyncMock()),
        ):
            await router_detail_edit.modal_wizard_submit_step(request, "demo", flow.flow_id, "domain-edit-1")

        assert not gezien.get("errors"), "een waarschuwing hoort de stap niet tegen te houden"
        meldingen = [msg for msgs in (gezien["warnings"] or {}).values() for msg in msgs]
        assert meldingen == [
            "Het kale domein van 'uitbetrouwbarebron.nl' kan pas gebruikt worden als het domein is goedgekeurd. "
            "Vink 'Domein aanvragen' aan."
        ]
        assert list(gezien["warnings"]) == [domain_setting_path(DomainSetting.BARE_DOMAIN_COMPONENT, 1)]
