"""Het startcommando van een component: een regel tekst, opgeslagen als lijst.

Kubernetes wil ``command`` als losse argumenten, maar niemand typt een lijst. Het veld
toont dus een regel zoals je hem in een terminal schrijft, en de converter splitst hem.
Dat is de plek waar het fout kan gaan, dus daar zitten de meeste tests op.

Het is een scherp mes: een waarde vervangt de ENTRYPOINT van het image, dus de eigen
opstartlogica vervalt, en een commando dat het image niet kent geeft een pod die niet start
met een fout die nergens naar wijst (onze eigen testimages liepen op
``exec: "sh": executable file not found in $PATH``). Vandaar de waarschuwing in de
hulptekst, en de nadruk op leeg laten: dat is de normale keuze en moet gewoon werken.
"""

from __future__ import annotations

import asyncio

from opi.forms.editables.converters import CommandLineConverter
from opi.forms.editables.editable import WidgetType
from opi.forms.editables.fields.components import COMPONENT_COMMAND_EDITABLE
from opi.forms.editables.processor import EditableFormProcessor
from opi.forms.visualizers.fields.components import COMPONENT_COMMAND, COMPONENTS_SEQUENCE
from opi.utils.yaml_util import dump_yaml_to_string, load_yaml_from_string

CONVERTER = CommandLineConverter()


class TestDeRegelWordtEenLijst:
    def test_het_voorbeeld_uit_de_praktijk(self) -> None:
        """Het geval waarvoor dit veld bestaat: een gecombineerd commando."""
        regel = 'sh -c "/app/docker/prod/seeder && exec /app/docker/prod/web"'
        assert CONVERTER.write(regel) == [
            "sh",
            "-c",
            "/app/docker/prod/seeder && exec /app/docker/prod/web",
        ]

    def test_zonder_quotes_splitst_hij_op_witruimte(self) -> None:
        assert CONVERTER.write("python -m app") == ["python", "-m", "app"]

    def test_een_dubbele_quote_bereikt_de_container_niet(self) -> None:
        """Quotes zijn hier een scheidingsteken, geen inhoud."""
        assert CONVERTER.write('sh -c "echo hoi"') == ["sh", "-c", "echo hoi"]

    def test_verdubbelde_quotes_leveren_er_wel_een_op(self) -> None:
        """De ontsnappingsroute, zoals in een spreadsheet: "" is een letterlijke quote."""
        assert CONVERTER.write('sh -c "echo ""hoi"""') == ["sh", "-c", 'echo "hoi"']

    def test_leeg_levert_niets_op(self) -> None:
        """Zodat ``remove_when_none`` de sleutel weghaalt. Het schema eist minItems 1, dus
        een lege lijst opslaan zou het hele project afkeuren."""
        assert CONVERTER.write("") is None
        assert CONVERTER.write("   ") is None


class TestDeLijstWordtWeerEenRegel:
    """De terugweg, voor het bewerken van een bestaand project."""

    def test_een_argument_met_spaties_krijgt_zijn_quotes_terug(self) -> None:
        lijst = ["sh", "-c", "/app/seeder && exec /app/web"]
        assert CONVERTER.read(lijst) == 'sh -c "/app/seeder && exec /app/web"'

    def test_een_letterlijke_quote_wordt_weer_verdubbeld(self) -> None:
        assert CONVERTER.read(["echo", 'hoi "daar"']) == 'echo "hoi ""daar"""'

    def test_heen_en_terug_verandert_niets(self) -> None:
        """De eigenschap die telt bij bewerken: openen en opslaan zonder iets te typen mag
        het commando niet stilzwijgend veranderen."""
        for lijst in (
            ["sh", "-c", "/app/docker/prod/seeder && exec /app/docker/prod/web"],
            ["python", "-m", "app"],
            ["echo", 'met "quotes"'],
            ["/bin/sh"],
        ):
            assert CONVERTER.write(CONVERTER.read(lijst)) == lijst, f"heen en terug veranderde {lijst!r}"


class TestWatErGeweigerdWordt:
    def _fouten(self, waarde: str) -> list[str]:
        validator = COMPONENT_COMMAND_EDITABLE.validator
        assert validator is not None, "het veld hoort een validator te hebben"
        return validator.validate(waarde)

    def test_een_openstaande_quote(self) -> None:
        """Anders splitst de regel anders dan bedoeld, en dat merk je pas in een
        CrashLoopBackOff."""
        assert self._fouten('sh -c "echo hoi') != []

    def test_een_stuurteken(self) -> None:
        assert self._fouten("sh -c \x00echo") != []

    def test_een_normaal_commando_mag(self) -> None:
        assert self._fouten('sh -c "/app/seeder && exec /app/web"') == []

    def test_leeg_mag(self) -> None:
        """Leeg laten is de normale keuze."""
        assert self._fouten("") == []
        assert self._fouten("   ") == []


class TestHetVeldZelf:
    def test_het_is_een_tekstveld_en_geen_reeks(self) -> None:
        """Een lijst invoeren is niet wat iemand wil; hij typt een commandoregel."""
        assert COMPONENT_COMMAND.widget == WidgetType.TEXT
        assert not COMPONENT_COMMAND.children

    def test_de_hulptekst_waarschuwt_voor_de_entrypoint(self) -> None:
        """Het is een scherp mes: de eigen opstartlogica van het image vervalt."""
        tekst = (COMPONENT_COMMAND.help_text or "").lower()
        assert "vervangt" in tekst
        assert "leeg" in tekst

    def test_het_hoort_bij_het_component(self) -> None:
        assert COMPONENT_COMMAND in COMPONENTS_SEQUENCE.children

    def test_het_staat_bij_identificatie_in_de_layout(self) -> None:
        """Een editable bestaan is niet genoeg: de componentenstap somt in zijn ``layout``
        expliciet op welke velden getoond worden. Wie er een toevoegt en die opsomming
        vergeet, levert een veld op dat nergens te zien is en waar niets over klaagt.

        Precies dat gebeurde hier: het veld stond in ``COMPONENTS_SEQUENCE``, kwam door
        elke definitie-test heen, en werd in de wizard nooit gerenderd.
        """
        from opi.forms.visualizers.wizard_sections import COMPONENTS_SECTION

        def _identificatie(items: object) -> list[str] | None:
            for item in items if isinstance(items, list) else []:
                if getattr(item, "legend", None) == "Identificatie":
                    return [kind for kind in getattr(item, "children", []) if isinstance(kind, str)]
                for attribuut in ("children", "child_layout"):
                    gevonden = _identificatie(getattr(item, attribuut, None))
                    if gevonden is not None:
                        return gevonden
            return None

        children = _identificatie(COMPONENTS_SECTION.layout)
        assert children is not None, "het identificatieblok bestaat niet meer"
        assert "command" in children, f"het startcommando hoort bij het image te staan, kreeg {children}"


class TestOpslaan:
    def _verwerk(self, command: str) -> dict:
        submitted = {"components": [{"name": "web", "image": "nginx:1.25", "command": command}]}
        resultaat, _errors = asyncio.run(
            EditableFormProcessor().process_json_submission(submitted, [COMPONENTS_SEQUENCE], {}, edit_mode=False)
        )
        return resultaat["components"][0]

    def test_een_leeg_commando_schrijft_geen_sleutel(self) -> None:
        """Het schema kent ``minItems: 1``, dus een lege lijst is ongeldig. Toen die toch
        geschreven werd, was elk project met een component onopslaanbaar zodra het veld
        onaangeroerd bleef."""
        component = self._verwerk("")
        assert "command" not in component, f"leeg hoort geen sleutel te schrijven, kreeg {component!r}"

    def test_een_ingevuld_commando_wordt_als_lijst_opgeslagen(self) -> None:
        component = self._verwerk('sh -c "/app/seeder && exec /app/web"')
        assert component["command"] == ["sh", "-c", "/app/seeder && exec /app/web"]

    def test_het_schema_keurt_het_resultaat_goed(self) -> None:
        """De vorm die de wizard schrijft moet door het schema komen; dat is de tweede weg
        naar binnen en die controleert onafhankelijk."""
        from opi.core.project_schema import validate_declared_project_schema

        component = self._verwerk("sh -c /app/start")
        validate_declared_project_schema({"schema-version": 2.6, "name": "demo", "components": [component]})


def test_een_commando_kan_het_document_niet_openbreken() -> None:
    """De zorg bij een vrij tekstveld: een waarde die de YAML eromheen herschrijft.

    De canonieke schrijver zet een meerregelige waarde neer als literal block, dus wat
    teruggelezen wordt is een string en geen extra sleutels. Dat is een eigenschap van de
    schrijver en niet van dit veld: verandert de schrijver, dan verandert het risico hier
    mee, en daarom staat het als eigen test vast.
    """
    aanval = ["/bin/sh", "-c", 'echo hoi\ncommand: ["rm", "-rf", "/"]\n']
    geschreven = dump_yaml_to_string({"components": [{"name": "web", "command": aanval}]})

    teruggelezen = load_yaml_from_string(geschreven)
    assert teruggelezen is not None
    assert teruggelezen["components"][0]["command"] == aanval, "de waarde hoort ongewijzigd terug te komen"
