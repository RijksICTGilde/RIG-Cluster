"""Een uitnodiging zonder realm-rol, in een project dat de toegang beperkt.

Uit de praktijk: ``projects/vr3ed-r0l.yaml`` droeg een invite zonder ``realm-roles``. Het
schema stond dat toe en terecht -- zo'n uitnodiging levert een kaal account op en dat is
een geldige keuze. Maar keycloak had in datzelfde project ``restrict-access`` aanstaan, en
dan laat het realm alleen rolhouders binnen. De uitnodiging gaf dus geen toegang, en
niemand kwam daar achter tot iemand de link probeerde.

Dat is een VOORWAARDELIJKE afhankelijkheid tussen twee diensten, en de twee mechanismen die
er al waren passen geen van beide:

* ``ServiceDefinition.requires`` is onvoorwaardelijk en BLOKKEERT (de authorization-wall
  gebruikt het zo). Hier zou blokkeren onjuist zijn: zonder ``restrict-access`` is een
  uitnodiging zonder rol volkomen in orde.
* Een enforcer op een ``FormSection`` hangt aan een sectie, terwijl deze uitspraak over het
  hele project gaat -- en invite heeft geen enforcer, dus hij zou nooit gedraaid worden.

``ConfigAdvice`` is de derde vorm, en met opzet GEEN derde kanaal: hij komt naar buiten via
de veldwaarschuwing (``field_warnings``) en via ``warnings`` op de schrijfactie, precies de
twee uitgangen die voor het certificaatgeval gebouwd zijn.
"""

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from opi.forms.editables.processor import EditableFormProcessor
from opi.manager.project_manager import ProjectManager
from opi.services.catalog.base import ConfigLayer
from opi.services.registry import get_service
from opi.services.services import ConfigAdvice, ServiceAdapter, collect_config_advice
from opi.services.services_enums import ServiceType

ROLE_PATH = "services/invite/config/active[0]/realm-roles"
RESTRICT_PATH = "services/keycloak/config/restrict-access"


def _project(*, restrict: bool, roles: Any) -> dict[str, Any]:
    """Een project met keycloak en een uitnodiging, waarvan de twee helften instelbaar zijn."""
    keycloak_config: dict[str, Any] = {"template": "sso-support"}
    if restrict is not None:
        keycloak_config["restrict-access"] = {"enabled": restrict, "realm-role": "allowed-user"}
    invite: dict[str, Any] = {"key": "Xk3pQ7rL2mNvB8dTfW1aYz"}
    if roles is not None:
        invite["realm-roles"] = roles
    return {
        "schema-version": 2,
        "name": "vr3ed-r0l",
        "services": [
            {"name": "keycloak", "config": keycloak_config},
            {"name": "invite", "config": {"active": [invite]}},
        ],
    }


class TestTheAdviceItself:
    """Wat ``collect_config_advice`` van een project vindt."""

    def test_a_roleless_invite_is_reported_when_access_is_restricted(self) -> None:
        """Het geval uit de praktijk."""
        notices = collect_config_advice(_project(restrict=True, roles=None))

        assert [n.field_path for n in notices] == [ROLE_PATH]
        assert "geen toegang" in notices[0].message

    def test_an_empty_role_list_counts_as_unfilled(self) -> None:
        """``realm-roles: []`` is hetzelfde als geen rol; alleen None controleren zou dit
        geval laten lopen, en het is precies wat een leeggemaakt formulierveld achterlaat."""
        assert [n.field_path for n in collect_config_advice(_project(restrict=True, roles=[]))] == [ROLE_PATH]

    def test_a_roleless_invite_is_fine_without_restrict_access(self) -> None:
        """De negatieve kant, en de reden dat dit geen ``requires`` is: zonder
        toegangsbeperking deelt deze uitnodiging gewoon kale accounts uit."""
        assert collect_config_advice(_project(restrict=False, roles=None)) == []

    def test_a_missing_condition_says_nothing(self) -> None:
        """Geen ``restrict-access``-blok is geen aanwijzing dat er iets mis is."""
        assert collect_config_advice(_project(restrict=None, roles=None)) == []

    def test_a_filled_role_says_nothing(self) -> None:
        assert collect_config_advice(_project(restrict=True, roles=["allowed-user"])) == []

    def test_every_entry_of_the_list_is_judged_on_its_own(self) -> None:
        """``[*]`` wordt per element opgelost, zodat de melding de uitnodiging noemt waar
        hij over gaat. Een uitspraak over 'de invite-config' zou bij twee uitnodigingen
        niet te herleiden zijn tot de een die de rol mist."""
        project = _project(restrict=True, roles=["allowed-user"])
        project["services"][1]["config"]["active"].append({"key": "tweede"})

        assert [n.field_path for n in collect_config_advice(project)] == [
            "services/invite/config/active[1]/realm-roles"
        ]

    def test_a_project_without_the_invite_service_says_nothing(self) -> None:
        """Geen apart geval in de code: het ``expects``-pad wijst dan nergens heen."""
        project = _project(restrict=True, roles=None)
        project["services"] = [project["services"][0]]

        assert collect_config_advice(project) == []


class TestTheDeclarationDrivesIt:
    """Generieke code kent geen dienstnamen; alles komt uit wat de dienst declareert."""

    def test_invite_declares_the_advice(self) -> None:
        definition = get_service(ServiceType.INVITE).definition
        advice = definition.config_advice

        assert len(advice) == 1
        assert advice[0].when == "services/keycloak/config/restrict-access/enabled"
        assert advice[0].expects == "services/invite/config/active[*]/realm-roles"

    def test_removing_the_declaration_removes_the_warning(self) -> None:
        """Het omkeerbewijs: haal de declaratie weg en de generieke code zwijgt. Zo staat
        vast dat de melding uit de dienst komt en niet uit een verstopte if."""
        definition = get_service(ServiceType.INVITE).definition
        with patch.object(definition, "config_advice", []):
            assert collect_config_advice(_project(restrict=True, roles=None)) == []

    def test_a_declaration_on_any_service_is_evaluated(self) -> None:
        """Niets in de evaluator is invite-specifiek: een advies op een andere dienst,
        over een ander veld, wordt op dezelfde manier gelezen."""
        keycloak = get_service(ServiceType.KEYCLOAK).definition
        advice = ConfigAdvice(
            when="services/invite/config/active[*]/key",
            expects="services/keycloak/config/restrict-access/realm-role",
            message="verzonnen advies",
        )
        project = _project(restrict=True, roles=["allowed-user"])
        del project["services"][0]["config"]["restrict-access"]["realm-role"]

        with patch.object(keycloak, "config_advice", [advice]):
            notices = collect_config_advice(project)

        assert [(n.field_path, n.message) for n in notices] == [(f"{RESTRICT_PATH}/realm-role", "verzonnen advies")]

    def test_the_definitions_carry_no_advice_by_default(self) -> None:
        """Een dienst die niets declareert krijgt geen lege-lijst-onderhoud."""
        declaring = [
            service_type
            for service_type, definition in ServiceAdapter.SERVICE_DEFINITIONS.items()
            if definition.config_advice
        ]
        assert declaring == [ServiceType.INVITE]


class TestTheFormWarnsAtTheField:
    """Uitgang 1: dezelfde ``field_warnings`` die het certificaatgeval gebruikt."""

    async def test_the_processor_warns_at_the_role_field(self) -> None:
        processor = EditableFormProcessor()

        await processor.process_json_submission({}, [], _project(restrict=True, roles=None))

        assert processor.field_warnings[ROLE_PATH] == [
            "Keycloak beperkt de toegang tot houders van een rol; een uitnodiging zonder "
            "realm-rol geeft dus geen toegang."
        ]

    async def test_the_processor_is_silent_without_restrict_access(self) -> None:
        """De negatieve kant op de formulierweg."""
        processor = EditableFormProcessor()

        await processor.process_json_submission({}, [], _project(restrict=False, roles=None))

        assert processor.field_warnings == {}

    async def test_the_wizards_virtual_root_is_read_too(self) -> None:
        """In de wizard staat de dienstconfiguratie onder ``_services-config`` en houdt
        ``services`` alleen de gekozen namen vast. Zou de evaluator alleen het echte pad
        lezen, dan viel de waarschuwing precies weg op het scherm waar hij hoort."""
        state = _project(restrict=True, roles=None)
        state["_services-config"] = state.pop("services")
        state["services"] = ["keycloak", "invite"]
        processor = EditableFormProcessor()

        await processor.process_json_submission({}, [], state)

        assert ROLE_PATH in processor.field_warnings

    async def test_the_same_advice_is_not_repeated(self) -> None:
        """De processor draait per stap en per hertekening; twee keer dezelfde zin onder
        een veld leest als twee problemen."""
        processor = EditableFormProcessor()
        project = _project(restrict=True, roles=None)

        await processor.process_json_submission({}, [], project)
        await processor.process_json_submission({}, [], project)

        assert len(processor.field_warnings[ROLE_PATH]) == 1


class TestTheApiSaysItOnTheWrite:
    """Uitgang 2: ``warnings`` op de schrijfactie, zoals bij het certificaat."""

    def test_the_warning_rides_on_a_config_write(self) -> None:
        warnings = ProjectManager._config_advice_warnings(_project(restrict=True, roles=None))

        assert len(warnings) == 1
        assert warnings[0].startswith(f"{ROLE_PATH}: ")
        assert "geen toegang" in warnings[0]

    def test_no_warning_without_the_condition(self) -> None:
        assert ProjectManager._config_advice_warnings(_project(restrict=False, roles=None)) == []

    def test_the_result_model_carries_the_field(self) -> None:
        """Zonder een veld op het resultaatmodel komt de lijst nooit bij de client aan."""
        from opi.api.task_models import ConfigureServiceResult

        result = ConfigureServiceResult(status="success", warnings=["a: b"])
        assert result.warnings == ["a: b"]


class TestTheWarningIsRendered:
    """Een waarschuwing die nergens getekend wordt is even onzichtbaar als geen waarschuwing."""

    def test_the_sequence_widget_renders_its_warnings(self) -> None:
        """De bridge zet ``field.warnings`` op elk veld, maar ``render_warnings`` stond
        alleen in het tekstveld -- het certificaatgeval had niets anders nodig. Het advies
        van invite wijst de LIJST aan (``realm-roles``), en dat is een sequence, dus zonder
        deze regel kwam de melding wel in ``field_warnings`` en werd hij nergens getekend.

        Op de bron en niet op gerenderde HTML: een widgettemplate draait alleen binnen de
        LOTC-omgeving met zijn voorbewerker, en die halve applicatie optuigen zou meer
        vastleggen dan de regel die hier bewaakt wordt."""
        source = (Path(__file__).parent.parent / "opi/templates_lotc/widgets/sequence.html.j2").read_text()

        assert "render_warnings(field.warnings)" in source


@pytest.mark.parametrize("layer", [ConfigLayer.PROJECT])
def test_the_advice_paths_are_real_editable_paths(layer: ConfigLayer) -> None:
    """``expects`` moet een veld aanwijzen dat de dienst ook echt bewerkbaar maakt, anders
    landt de waarschuwing op een pad dat het formulier niet opzoekt en ziet niemand hem."""
    invite = get_service(ServiceType.INVITE)
    paths = {editable.yaml_path for editable in invite.config_editables(layer)}

    assert invite.definition.config_advice[0].expects in paths
