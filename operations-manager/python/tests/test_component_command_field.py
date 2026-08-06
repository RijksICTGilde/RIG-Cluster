"""The component's start command is settable, optional, and absent when empty.

`command` already existed in the schema (on both the component and the
deployment-component) and the manifest already rendered it, but there was no editable, no
form field and no API field: the only way to set it was hand-editing the project file,
while the schema suggested it was supported.

It is a sharp tool. Kubernetes replaces the image's ENTRYPOINT with this, so a value here
silently discards the image's own start-up logic, and a command the image does not carry
gives a pod that never starts with an error that points nowhere -- our own test images hit
`exec: "sh": executable file not found in $PATH`. Hence the warning in the help text and
the insistence on it staying out of the file when empty.
"""

from __future__ import annotations

from typing import Any

from opi.forms.editables.fields.components import COMPONENT_COMMAND_EDITABLE, COMPONENT_COMMAND_ITEM_EDITABLE
from opi.forms.visualizers.fields.components import COMPONENT_COMMAND, COMPONENTS_SEQUENCE
from opi.utils.yaml_util import dump_yaml_to_string, load_yaml_from_string


def _paths(visualizer: Any) -> list[str]:
    return [child.editable.yaml_path for child in visualizer.children or []]


def test_the_field_is_offered_on_the_component() -> None:
    assert any("command" in path for path in _paths(COMPONENTS_SEQUENCE))


def test_it_is_not_required() -> None:
    """Most components should never touch this."""
    assert COMPONENT_COMMAND_EDITABLE.required is False
    assert COMPONENT_COMMAND_EDITABLE.min_items == 0


def test_empty_is_removed_rather_than_written_as_an_empty_list() -> None:
    """The schema demands ``minItems: 1``, so an empty list is not merely ugly but
    invalid, and it would also override the image's entrypoint with nothing."""
    assert COMPONENT_COMMAND_EDITABLE.remove_when_none is True


def test_the_help_text_warns_about_replacing_the_entrypoint() -> None:
    """The danger is not that a command can be wrong, it is that a correct-looking one
    discards what the image brought along. Say that, not just "be careful"."""
    help_text = (COMPONENT_COMMAND.help_text or "").lower()

    assert "leeg" in help_text, "it must say that leaving it empty is the normal case"
    assert "vervangt" in help_text, "it must say the image's own start command is replaced"


def test_the_help_text_carries_no_angle_brackets() -> None:
    """Help text becomes a ROOS attribute, and ROOS re-emits attribute values, so anything
    needing escaping is escaped twice and the reader sees the entities."""
    assert "<" not in (COMPONENT_COMMAND.help_text or "")
    assert ">" not in (COMPONENT_COMMAND.help_text or "")


def test_each_argument_is_its_own_entry() -> None:
    """A command is a list in Kubernetes; one text field with spaces in it would quietly
    turn `sh -c "x y"` into a single argument."""
    assert COMPONENT_COMMAND_EDITABLE.children
    assert COMPONENT_COMMAND_EDITABLE.children[0].yaml_path.endswith("command[*]")


def test_een_argument_mag_een_heel_shellscript_zijn() -> None:
    """De reden dat dit veld bestaat: een gecombineerd commando.

    Openproject start als ``["/bin/sh", "-c", "<script>"]``. Spaties, aanhalingstekens en
    regeleindes horen dus in een argument thuis; een validator die die weigert maakt het
    veld nutteloos voor precies het geval waarvoor het is gebouwd.
    """
    validator = COMPONENT_COMMAND_ITEM_EDITABLE.validator
    assert validator is not None, "het argumentveld hoort een validator te hebben"

    for argument in ["/bin/sh", "-c", "bundle exec rails s -b 0.0.0.0", 'echo "hoi" && ls', "regel1\nregel2"]:
        assert validator.validate(argument) == [], f"{argument!r} hoort toegestaan te zijn"


def test_een_leeg_argument_wordt_geweigerd() -> None:
    """Kubernetes geeft een leeg argument gewoon door, en dat verschuift alles erna."""
    validator = COMPONENT_COMMAND_ITEM_EDITABLE.validator
    assert validator is not None
    assert validator.validate("   ") != []
    assert validator.validate("a\x00b") != [], "een stuurteken hoort niet in een commando"


def test_het_schema_weigert_wat_de_validator_weigert() -> None:
    """Het formulier is niet de enige weg naar binnen; de API en een handmatige bewerking
    komen langs het schema. Die twee horen hetzelfde te vinden."""
    from opi.core.project_schema import validate_declared_project_schema

    def keurt_goed(command: list[str]) -> bool:
        project = {
            "schema-version": 2.6,
            "name": "demo",
            "components": [{"name": "web", "image": "x:1", "command": command}],
        }
        try:
            validate_declared_project_schema(project)
            return True
        except Exception:
            return False

    assert keurt_goed(["/bin/sh", "-c", "bundle exec rails s"]), "het openproject-patroon hoort te mogen"
    assert not keurt_goed(["/bin/sh", ""]), "een leeg argument hoort ook via het schema te sneuvelen"
    assert not keurt_goed([]), "een leeg commando hoort weggelaten te worden, niet leeg opgeslagen"


def test_een_commando_kan_het_document_niet_openbreken() -> None:
    """De zorg bij een vrij tekstveld: een waarde die de YAML eromheen herschrijft.

    De canonieke schrijver zet een meerregelige waarde neer als literal block, dus wat
    eruit komt is één string en geen extra sleutels. Deze test bewaakt dat, want het is
    een eigenschap van de schrijver en niet van dit veld: als de schrijver verandert,
    verandert het risico hier mee.
    """
    aanval = ["/bin/sh", "-c", 'echo hoi\ncommand: ["rm", "-rf", "/"]\n']
    geschreven = dump_yaml_to_string({"components": [{"name": "web", "command": aanval}]})

    teruggelezen = load_yaml_from_string(geschreven)
    assert teruggelezen is not None
    assert teruggelezen["components"][0]["command"] == aanval, "de waarde hoort ongewijzigd terug te komen"


def test_an_empty_command_writes_no_key_at_all() -> None:
    """Leeg laten is de normale keuze, en die moet opslaanbaar zijn.

    Het schema kent ``minItems: 1`` op ``command``, dus een lege lijst is ongeldig. Een
    reeks schreef die toch, omdat ``remove_when_none`` alleen op losse velden werd
    toegepast: elk project met een component werd daardoor afgekeurd zodra het veld
    onaangeroerd bleef.
    """
    import asyncio

    from opi.forms.editables.processor import EditableFormProcessor
    from opi.forms.visualizers.fields.components import COMPONENTS_SEQUENCE

    submitted = {"components": [{"name": "web", "image": "nginx:1.25", "command": []}]}
    resultaat, _errors = asyncio.run(
        EditableFormProcessor().process_json_submission(submitted, [COMPONENTS_SEQUENCE], {}, edit_mode=False)
    )
    component = resultaat["components"][0]
    assert "command" not in component, f"een leeg commando hoort geen sleutel te schrijven, kreeg {component!r}"


def test_a_filled_command_is_still_written() -> None:
    """De keerzijde: het weglaten mag niet zo ver gaan dat een echt commando sneuvelt."""
    import asyncio

    from opi.forms.editables.processor import EditableFormProcessor
    from opi.forms.visualizers.fields.components import COMPONENTS_SEQUENCE

    submitted = {"components": [{"name": "web", "image": "nginx:1.25", "command": ["/bin/sh", "-c", "echo hoi"]}]}
    resultaat, _errors = asyncio.run(
        EditableFormProcessor().process_json_submission(submitted, [COMPONENTS_SEQUENCE], {}, edit_mode=False)
    )
    assert resultaat["components"][0]["command"] == ["/bin/sh", "-c", "echo hoi"]


def test_the_field_is_actually_laid_out_on_the_components_step() -> None:
    """Een editable bestaan is niet genoeg: de componentenstap somt in zijn ``layout``
    expliciet op welke velden getoond worden. Wie er een toevoegt en die opsomming
    vergeet, levert een veld op dat nergens te zien is en waar niets over klaagt.

    Precies dat gebeurde bij het startcommando: het stond in ``COMPONENTS_SEQUENCE``,
    kwam door elke definitie-test heen, en werd in de wizard nooit gerenderd.
    """
    from opi.forms.visualizers.wizard_sections import COMPONENTS_SECTION

    def _field_names(items: object) -> set[str]:
        namen: set[str] = set()
        for item in items if isinstance(items, list) else []:
            if isinstance(item, str):
                namen.add(item)
                continue
            naam = getattr(item, "field_name", None)
            if naam:
                namen.add(naam)
            for attribuut in ("children", "child_layout"):
                namen |= _field_names(getattr(item, attribuut, None))
        return namen

    assert "command" in _field_names(COMPONENTS_SECTION.layout), (
        "het startcommando staat niet in de layout van de componentenstap, dus het wordt niet getoond"
    )
