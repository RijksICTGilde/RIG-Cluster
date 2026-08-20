"""De inventaris van schrijvers naar het projectbestand, en een grendel op die lijst.

De vraag van RC-129 was niet "hoe repareren we dit schemagat" maar "waarom viel het pas op
toen iemand toevallig in de logs keek". Het antwoord: er was geen enkele test die legde wat
de CODE SCHRIJFT langs het SCHEMA, en niemand had een lijst van wie er eigenlijk schrijven.
Zo'n lijst in een markdownbestand veroudert stil; deze staat daarom in een test.

Wat hier bewaakt wordt is bewust smal: de VERZAMELING MODULES die naar een projectbestand
schrijft. Komt er een schrijver bij, dan valt deze test om met de vraag die twee keer niet
gesteld is -- past wat jij schrijft in het schema, en legt een test dat vast? De inhoud van
die controle staat in ``tests/test_schrijvers_tegen_het_schema.py``.

Wat een "schrijver" is, is hier een aanroep van een van de schrijfwegen van de store:
``ProjectManager.save_and_commit_project`` (de gebruikelijke) of ``get_project_store()``'s
eigen ``create``/``save``/``mutate``. De store zelf staat er niet in: die IS de schrijfweg.
De detectie loopt over de AST en niet over een grep, want een grep telt ook de naam in een
docstring of een commentaarregel mee -- en dan bewaakt de grendel iets anders dan hij zegt.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

OPI_DIR = Path(__file__).resolve().parent.parent / "opi"

#: De methodenamen die een projectbestand naar git schrijven.
_SCHRIJFWEGEN = frozenset({"save_and_commit_project", "create", "save", "mutate"})

#: ``create``/``save``/``mutate`` zijn te algemene namen om op zichzelf te tellen; ze tellen
#: alleen als de ontvanger de store is. ``save_and_commit_project`` is uniek genoeg.
_STORE_ONTVANGERS = ("project_store", "get_project_store()")


@dataclass(frozen=True)
class Schrijver:
    """Wat een module in het projectbestand schrijft, en wat dat resultaat controleert."""

    schrijft: str
    gedekt_door: str | None
    reden: str = ""


#: Elke module die naar een projectbestand schrijft. ``gedekt_door`` noemt de test die het
#: RESULTAAT van die schrijver langs de poorten van een save legt; is die None, dan zegt
#: ``reden`` waarom niet.
INVENTARIS: dict[str, Schrijver] = {
    # ---- programmatische schrijvers: draaien zonder gebruiker, met enforce_validation=False ----
    "opi/services/resource_tuning_service.py": Schrijver(
        "resources en resources/history op de deploymentcomponent (limits EN requests), plus compactie",
        "test_de_tuner_laat_een_geldig_projectbestand_achter",
    ),
    "opi/services/oom_watcher.py": Schrijver(
        "disabled/disabled-reason op de deploymentcomponent na image-pull-fouten",
        "test_het_uitzetten_van_een_deploymentcomponent_blijft_geldig",
    ),
    "opi/services/deployment_observation.py": Schrijver(
        "commit-punt voor de after-sync haken; de structuur komt van de tuner en de diensten",
        "test_de_tuner_laat_een_geldig_projectbestand_achter",
    ),
    "opi/api/resource_router.py": Schrijver(
        "disabled/disabled-reason op de deploymentcomponent bij het opschonen van kapotte componenten",
        "test_het_uitzetten_van_een_deploymentcomponent_blijft_geldig",
    ),
    "opi/core/backup_tasks.py": Schrijver(
        "generatienummers per dienst op de deployment, en een hele nieuwe deployment bij een kloon",
        "test_generatienummers_van_de_backuptaken_blijven_geldig",
    ),
    "opi/api/restore_router.py": Schrijver(
        "generatienummers en de kloonstatus na een restore",
        "test_de_kloonstatus_blijft_geldig",
    ),
    "opi/services/catalog/sleep_mode/flow.py": Schrijver(
        "deployments[].sleep: stand, deadline en het versleutelde wektoken",
        "test_de_hele_slaapcyclus_blijft_geldig",
    ),
    "opi/services/catalog/sleep_mode/scheduler.py": Schrijver(
        "dezelfde slaapstaat, maar vanuit de nachtelijke veger",
        "test_de_hele_slaapcyclus_blijft_geldig",
    ),
    # ---- door de gebruiker gestuurde schrijvers: enforce_validation=True ----
    "opi/manager/project_manager.py": Schrijver(
        "vrijwel elk deel: componenten, deployments, diensten en hun config, registries, bijlagen",
        "test_een_registry_op_een_bestaand_secret_blijft_geldig",
        "Deels: registries en bijlagen worden gedraaid; de andere methoden hebben elk hun eigen test "
        "in tests/test_component_*.py en tests/test_*_api.py, die het resultaat nog niet langs de poorten leggen.",
    ),
    "opi/manager/mail_manager.py": Schrijver(
        "services/send-email/config/accounts: per cluster de gebruikersnaam, het AGE-versleutelde "
        "wachtwoord, het afzender- en het bounce-adres; en het intrekken haalt datzelfde item weg",
        "test_een_mailaccount_blijft_geldig",
    ),
    "opi/manager/keycloak_manager.py": Schrijver(
        "services/keycloak/config/realms: host, realm, gebruikersnaam, versleuteld wachtwoord en totp_secret",
        None,
        "Het item wordt inline gebouwd in _configure_realm_from_template, midden in een methode die een "
        "levende Keycloak nodig heeft. De vorm raakt het JSON-schema niet (dienstconfig is daar vrij) maar "
        "wel het pydantic-model van de dienst; zie de notitie in het verslag.",
    ),
    "opi/manager/delete_project_manager.py": Schrijver(
        "haalt het realm-item weer uit services/keycloak/config/realms",
        None,
        "Zelfde blok als de keycloak-manager, en dezelfde reden.",
    ),
    "opi/web/router_wizard.py": Schrijver(
        "het hele bestand zoals het formulier het oplevert",
        "test_the_generated_file_passes_the_save_gates (tests/test_create_project_api.py)",
    ),
    "opi/web/router_detail_edit.py": Schrijver(
        "het deel van het bestand dat de bewerkte stroom beslaat",
        None,
        "De formulierstromen valideren hun eigen resultaat via tests/forms/; de schrijfweg zelf is enforce=True, "
        "dus een schemafout hier blokkeert de bewerking in plaats van stil te landen.",
    ),
    "opi/web/router.py": Schrijver(
        "de domeininstellingen van een deployment, en het terugdraaien daarvan",
        "test_elke_domeininstelling_blijft_geldig",
    ),
    "opi/web/router_approvals.py": Schrijver(
        "de goedkeuringsstaat van domeinen en subdomeinen onder publish-on-web",
        None,
        "De goedkeuringsblokken hebben hun eigen modeltests in tests/test_domain_approval*.py; de schrijfweg is "
        "enforce=True.",
    ),
    "opi/core/task_handlers_project.py": Schrijver(
        "het projectbestand zoals de aanmaaktaak het opbouwt",
        "test_the_generated_file_passes_the_save_gates (tests/test_create_project_api.py)",
    ),
    "opi/api/router.py": Schrijver(
        "het projectbestand zoals een zelfbedieningsaanvraag het aanlevert",
        "test_the_generated_file_passes_the_save_gates (tests/test_create_project_api.py)",
    ),
}


#: De naamprefixen waarmee ``ProjectFileHandler`` zijn muterende functies aanduidt.
_MUTATIEPREFIXEN = ("set_", "append_", "compact_", "reenable_", "remove_", "apply_")

#: De gedeelde structuurschrijvers van ``ProjectFileHandler``: functies die project_data
#: aanpassen. Ze staan los van de inventaris hierboven, want ze committen zelf niet -- de
#: aanroeper doet dat. Elke naam wijst de test die zijn resultaat langs de poorten legt.
HANDLER_MUTATOREN: dict[str, str] = {
    "set_component_disabled": "test_het_uitzetten_van_een_component_blijft_geldig",
    "set_component_resources": "test_de_wortelcomponent_bijstellen_blijft_geldig",
    "apply_user_resource_intent": "test_een_handmatig_gezette_waarde_blijft_geldig",
    "append_component_resource_history": "test_de_wortelcomponent_bijstellen_blijft_geldig",
    "set_deployment_component_disabled": "test_het_uitzetten_van_een_deploymentcomponent_blijft_geldig",
    "set_deployment_component_resources": "test_de_tuner_laat_een_geldig_projectbestand_achter",
    "append_deployment_component_resource_history": "test_de_tuner_laat_een_geldig_projectbestand_achter",
    "compact_resource_history": "test_de_tuner_comprimeert_de_historie_tot_iets_geldigs",
    "reenable_components_with_changed_image": "test_het_weer_aanzetten_na_een_nieuwe_image_blijft_geldig",
    "set_storage_generation": "test_generatienummers_van_de_backuptaken_blijven_geldig",
    "set_database_generation": "test_generatienummers_van_de_backuptaken_blijven_geldig",
    "set_bucket_generation": "test_generatienummers_van_de_backuptaken_blijven_geldig",
    "set_deployment_service_generation": "test_generatienummers_van_de_backuptaken_blijven_geldig",
    "set_clone_status": "test_de_kloonstatus_blijft_geldig",
    "remove_attachment_references": "test_het_verwijderen_van_verwijzingen_blijft_geldig",
    "remove_component_references": "test_het_verwijderen_van_verwijzingen_blijft_geldig",
}


def _handler_mutatoren() -> set[str]:
    """Elke functie in ``project_file_handler`` die project_data aanpast."""
    pad = OPI_DIR / "handlers" / "project_file_handler.py"
    boom = ast.parse(pad.read_text(encoding="utf-8"))
    return {
        knoop.name
        for knoop in ast.walk(boom)
        if isinstance(knoop, ast.FunctionDef | ast.AsyncFunctionDef)
        and knoop.name.startswith(_MUTATIEPREFIXEN)
        and "project_data" in [arg.arg for arg in knoop.args.args]
    }


def test_elke_gedeelde_structuurschrijver_wordt_ergens_gedraaid() -> None:
    """De zetters van ProjectFileHandler zijn wat de meeste schrijvers werkelijk gebruikt.

    De tuner riep ``append_deployment_component_resource_history`` aan met een item dat hij
    zelf samenstelde; de zetter zelf is generiek. Beide kanten moeten dus gedraaid worden --
    de kant van de aanroeper in de tunertest, en elke zetter hier.
    """
    gevonden = _handler_mutatoren()
    nieuw = sorted(gevonden - set(HANDLER_MUTATOREN))
    verdwenen = sorted(set(HANDLER_MUTATOREN) - gevonden)

    assert not nieuw, (
        f"Nieuwe structuurschrijver(s) in project_file_handler: {nieuw}. Draai ze in "
        f"tests/test_schrijvers_tegen_het_schema.py en zet de test hier in HANDLER_MUTATOREN."
    )
    assert not verdwenen, f"Deze structuurschrijvers bestaan niet meer; haal ze uit HANDLER_MUTATOREN: {verdwenen}"


def test_de_keycloak_schrijver_kan_deze_klasse_fout_niet_maken() -> None:
    """De reden waarom de keycloak-schrijver niet gedraaid wordt, moet waar blijven.

    Hij is niet te draaien zonder een levende Keycloak. Dat mag alleen zolang zijn vorm de
    poort niet kan raken: het JSON-schema laat de config van een dienst vrij, en het model
    van het realm-item staat extra velden toe. Verandert een van die twee, dan valt deze
    test om en moet de schrijver alsnog gedraaid worden.
    """
    import json

    from opi.services.catalog.keycloak.config_model import KeycloakRealm

    schema = json.loads((OPI_DIR / "schemas" / "project_v2.json").read_text(encoding="utf-8"))
    dienst_item = schema["$defs"]["service-entry"]
    object_tak = next(tak for tak in dienst_item["oneOf"] if tak.get("type") == "object")

    assert "properties" not in object_tak, "het schema is de config van een dienst gaan beschrijven"
    assert KeycloakRealm.model_config.get("extra") == "allow"


def _schrijvende_modules() -> set[str]:
    """Elke module onder opi/ die een van de schrijfwegen aanroept."""
    gevonden: set[str] = set()
    wortel = OPI_DIR.parent
    for pad in sorted(OPI_DIR.rglob("*.py")):
        boom = ast.parse(pad.read_text(encoding="utf-8"))
        for knoop in ast.walk(boom):
            if not isinstance(knoop, ast.Call) or not isinstance(knoop.func, ast.Attribute):
                continue
            naam = knoop.func.attr
            if naam not in _SCHRIJFWEGEN:
                continue
            ontvanger = ast.unparse(knoop.func.value)
            if naam != "save_and_commit_project" and not any(t in ontvanger for t in _STORE_ONTVANGERS):
                continue
            gevonden.add(str(pad.relative_to(wortel)))
    # De store is de schrijfweg zelf, niet een schrijver die er gebruik van maakt.
    gevonden.discard("opi/services/project_store.py")
    return gevonden


def test_de_inventaris_dekt_precies_de_schrijvers_die_er_zijn() -> None:
    """Een nieuwe schrijver moet een regel in de inventaris krijgen; een verdwenen schrijver eruit.

    Dit is de grendel. Hij zegt niets over of de nieuwe schrijver GOED is -- hij dwingt af dat
    iemand er bewust naar kijkt, en dat is precies wat twee keer niet gebeurd is.
    """
    gevonden = _schrijvende_modules()
    nieuw = sorted(gevonden - set(INVENTARIS))
    verdwenen = sorted(set(INVENTARIS) - gevonden)

    assert not nieuw, (
        f"Nieuwe schrijver(s) naar het projectbestand: {nieuw}. Zet ze in INVENTARIS met wat ze schrijven, "
        f"en leg dat resultaat langs de poorten in tests/test_schrijvers_tegen_het_schema.py."
    )
    assert not verdwenen, f"Deze schrijver(s) bestaan niet meer; haal ze uit INVENTARIS: {verdwenen}"


def test_elke_schrijver_zegt_of_hij_gedekt_is() -> None:
    """Een lege ``gedekt_door`` mag, maar dan staat er een reden bij."""
    zonder_reden = [pad for pad, s in INVENTARIS.items() if s.gedekt_door is None and not s.reden]

    assert not zonder_reden, f"Deze schrijvers zijn niet gedekt en zeggen niet waarom: {zonder_reden}"


def test_de_genoemde_tests_bestaan_echt() -> None:
    """Een verwijzing naar een test die hernoemd is, maakt de inventaris een verhaal in plaats van een meting."""
    hier = Path(__file__).parent
    bronnen = {
        pad.name: pad.read_text(encoding="utf-8")
        for pad in [hier / "test_schrijvers_tegen_het_schema.py", hier / "test_create_project_api.py"]
    }
    genoemd = [s.gedekt_door for s in INVENTARIS.values() if s.gedekt_door] + list(HANDLER_MUTATOREN.values())
    ontbreekt = []
    for verwijzing in genoemd:
        naam = verwijzing.split(" ")[0]
        if not any(f"def {naam}(" in bron for bron in bronnen.values()):
            ontbreekt.append(naam)

    assert not ontbreekt, f"De inventaris noemt tests die niet (meer) bestaan: {ontbreekt}"
