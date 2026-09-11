"""De weg terug mag geen stille weg zijn (RC-187).

Het keuzeveld bij een component verdwijnt zodra het project geen registries meer heeft.
De verwijzing in het bestand verdwijnt niet mee, dus zonder deze grendel houdt de afnemer
een component over dat naar iets wijst wat hij nergens meer ziet.
"""

from __future__ import annotations

from typing import Any

from opi.forms.visualizers.wizard_sections import _strip_removed_services_from_components
from opi.services.catalog.image_registries.references import validate_registry_references
from opi.services.registry import get_service
from opi.services.services_enums import ServiceType

_SVC = ServiceType.IMAGE_REGISTRIES.value
_REGISTRY = {"name": "code-overheid", "upstream": "code.overheid.nl/team"}


def _project(registries: list[dict[str, Any]], *, component_registry: str | None = "code-overheid") -> dict[str, Any]:
    component: dict[str, Any] = {"name": "web", "image": "code.overheid.nl/team/app:1"}
    if component_registry:
        component["services"] = [{"name": _SVC, "config": {"registry": component_registry}}]
    project: dict[str, Any] = {"name": "demo", "components": [component]}
    if registries is not None:
        project["services"] = [{"name": _SVC, "config": {"registries": registries}}]
    return project


class TestDeGrendel:
    def test_de_laatste_registry_weghalen_wordt_geweigerd(self) -> None:
        fouten = validate_registry_references(_project([]))
        assert len(fouten) == 1
        assert "code-overheid" in fouten[0]
        assert "component 'web'" in fouten[0]

    def test_de_dienst_uitzetten_wordt_net_zo_goed_geweigerd(self) -> None:
        """Geen dienstvermelding op projectniveau betekent geen registries.

        Gemeten door de SAVE-ROUTE heen, want die is de reden dat deze grendel bestaat:
        de dienstensectie draait haar ``post_merge`` voor ``validate_project`` kijkt, en
        die gooide de componentvermelding weg voordat de grendel hem kon zien. Dan is de
        toets los groen terwijl de route stilletjes opruimt -- precies wat het plan
        verbiedt.
        """
        project = _project([])
        del project["services"]
        _strip_removed_services_from_components(project, {})
        assert project["components"][0]["services"], "de vermelding mag niet stil verdwijnen"
        assert len(validate_registry_references(project)) == 1

    def test_met_de_registry_erin_is_er_niets_aan_de_hand(self) -> None:
        assert validate_registry_references(_project([_REGISTRY])) == []

    def test_een_component_zonder_keuze_houdt_niets_tegen(self) -> None:
        """Publiek is de afwezigheid van een keuze, en die blokkeert nooit."""
        assert validate_registry_references(_project([], component_registry=None)) == []

    def test_de_laatste_registry_weghalen_overleeft_de_save_route_ook(self) -> None:
        """De dienst staat er nog, alleen de registries zijn weg: de strip laat de
        vermelding dan sowieso staan, en de grendel weigert."""
        project = _project([])
        _strip_removed_services_from_components(project, {})
        assert len(validate_registry_references(project)) == 1

    def test_de_override_op_een_deployment_telt_mee(self) -> None:
        """Daar is ``services`` een dict, en een verwijzing die daar achterblijft is net
        zo onzichtbaar."""
        project = _project([], component_registry=None)
        project["deployments"] = [
            {
                "name": "productie",
                "components": [
                    {"reference": "web", "services": {_SVC: {"config": {"registry": "weg"}}}},
                ],
            }
        ]
        fouten = validate_registry_references(project)
        assert len(fouten) == 1
        assert "deployment 'productie', component 'web'" in fouten[0]

    def test_de_melding_noemt_elke_plek_die_de_registry_nog_gebruikt(self) -> None:
        project = _project([])
        project["components"].append(
            {"name": "api", "services": [{"name": _SVC, "config": {"registry": "code-overheid"}}]}
        )
        fouten = validate_registry_references(project)
        assert len(fouten) == 1
        assert "component 'web'" in fouten[0]
        assert "component 'api'" in fouten[0]

    def test_niet_automatisch_opruimen(self) -> None:
        """De toets LEEST alleen: stilletjes de verwijzing weghalen zou veranderen waar een
        image vandaan komt zonder dat iemand ernaar keek."""
        project = _project([])
        voor = str(project)
        validate_registry_references(project)
        assert str(project) == voor


def test_de_grendel_hangt_aan_de_dienst() -> None:
    """Anders draait hij niet op de schrijfroutes die langs ``validate_project`` gaan."""
    fouten = get_service(ServiceType.IMAGE_REGISTRIES).validate_project(_project([]))
    assert any("code-overheid" in fout for fout in fouten)
