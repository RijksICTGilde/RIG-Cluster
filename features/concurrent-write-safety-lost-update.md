# Gelijktijdig schrijven aan hetzelfde projectbestand

Twee schrijvers die tegelijk hetzelfde projectbestand raken, een bewerking in de UI en
een patch via de API, laten beide wijzigingen staan. Kan dat niet, dan faalt de save
zichtbaar met een `ConflictError` en een uitleg in de modal; stil overschrijven gebeurt
niet. Het projectbestand draagt naast de componenten ook de ledenlijst en de
toegangsconfiguratie, dus dit is een integriteitseis: BIO2 5.33 (bescherming van
registraties tegen onbedoelde wijziging) en 8.09 (beheerst wijzigen van configuratie).

## Hoe het werkt

Alle schrijfpaden gaan door de `ProjectStore` (`opi/services/project_store.py`):

- Eén proceswijde `asyncio.Lock` serialiseert elke write. Web-requests en de takenwerker
  draaien in hetzelfde proces ("combined mode", `server.py`), dus ze wachten op elkaar.
- `save(data, base=...)` is een compare-and-swap. `base` is de staat die de aanroeper las.
  Is het bestand sindsdien gewijzigd, dan voegt `_reconcile_with_concurrent_write` de twee
  samen met een driewegsmerge op de GEPARSEERDE structuur, valideert het resultaat, en
  weigert met `ConflictError` als dat niet lukt.
- `ProjectManager.get_contents()` onthoudt wat het las, en `save_and_commit_project()`
  geeft dat door als `base`. Een pad dat leest en later schrijft hoeft daar dus niets voor
  te doen.
- `mutate(change)` is de sterkere vorm: de wijziging is een functie, die onder de lock op
  verse staat opnieuw wordt uitgevoerd. Dat lost ook het geval dat een tekstmerge niet kan,
  twee schrijvers die elk een component toevoegen.

## Eén bewerking, één save

De fout die hier zat was geen ontbrekende `base` maar een dubbele save. Een
modal-bewerking sloeg het bestand op in het request (`_process_and_save_modal_edit`) en
startte daarna een `create_project`-taak die het opnieuw schreef, met twee verouderingen tegelijk:

- zijn data was het formulierresultaat van VOOR de eerste save, dus zonder wat de store
  daar eventueel in had gemergd;
- zijn `base` was de versie van het moment dat het formulier werd GELADEN, twee versies
  oud.

Gemeten in `tests/test_project_store_gelijktijdig_ui_en_api.py`: na een UI-verwijdering en
een gelijktijdige API-patch kon de store die tweede delta niet meer toepassen, want een
delta adresseert lijstitems op index en het component was al weg. De taak faalde dan met
"Dit project is ondertussen door iemand anders gewijzigd" over zijn eigen, al gecommitte
wijziging, en de uitrol stopte daarop. In de omgekeerde volgorde had dezelfde save de
patch van de ander juist teruggedraaid.

De oplossing is dat de taak het bestand niet opnieuw schrijft als de aanroeper dat al deed.
De payload zegt dat met `project_file_already_saved: True`, en de taak leest dan de
geldende staat uit de store in plaats van de formulierversie te publiceren. Dat geldt voor
de bewerkmodal (`router_detail_edit._start_deployment`) en voor een domeinbesluit
(`router_approvals`), de twee paden die eerst opslaan en dan uitrollen.

Twee andere routes vielen af:

- **De taak de versie NA de UI-save als basis geven.** Dan blijft de data het
  formulierresultaat van voor die save. Is er niets tussendoor gekomen, dan ziet de store
  `current == base` en publiceert hij die oudere data over een merge heen: precies de
  verloren wijziging die dit moet voorkomen.
- **Dit pad omzetten naar `mutate_and_commit_project`.** Dat is de goede richting voor de
  schrijfpaden die zelf opslaan, maar het raakt de dubbele save niet: de tweede schrijver
  blijft dan bestaan. Per pad een eigen taak.

`base_version` in de payload van `create_project` blijft bestaan voor een aanroeper die
een compleet bestand aanlevert zonder het zelf te hebben opgeslagen. Op dit moment stuurt
geen enkele aanroeper het; een aanroeper zonder token krijgt een WARNING in de log, zodat
dat gat zichtbaar blijft (`tests/test_task_save_base_version.py`).

## Wat de gebruiker ziet bij een echt conflict

Een `ConflictError` draagt zijn eigen uitleg ("Project X is gewijzigd sinds je begon met
bewerken ... Haal de laatste versie op en voer je wijziging opnieuw uit"). Die komt in de
modal terug via dezelfde weg als een validatiefout, de review met `global_errors`, en niet
als een kale 500. De uitleg gaat onbewerkt door: de aanhef die een validatiefout krijgt
("want het project is daarna niet geldig") is onwaar voor een conflict en zou er een tweede
oorzaak naast zetten. Vastgepind in `tests/test_modal_edit_conflict.py` voor de bewerkmodal
en in `tests/test_domain_approval_conflict.py` voor het domeinbesluit, dat deze fout sinds RC-317
op dezelfde manier opvangt.

## Grenzen

- **Delta's adresseren lijstitems op index.** Een verwijdering eerder in een lijst
  verschuift de posities erna en wordt als conflict gemeld, ook als de twee wijzigingen
  semantisch niets met elkaar te maken hebben. Dat faalt dicht: een fout, nooit een
  verloren wijziging. Items matchen op hun naamsleutel zou dit verfijnen.
- **Eén replica.** De lock serialiseert schrijvers in DIT proces. OPI draait vandaag één
  replica per cluster. Meerdere replica's zouden een Postgres advisory lock of een
  single-writer leader vragen; de interface verandert daar niet van.
- **Een schrijver buiten het proces** (een ander cluster, een mens die in de repo pusht)
  wordt optimistisch opgevangen: een non-fast-forward push betekent "HEAD is verschoven",
  dus de store reset naar de remote, leest opnieuw, past opnieuw toe en valideert opnieuw.

## Bewijs

| Test | Wat het aantoont |
|---|---|
| `tests/test_project_store_gelijktijdig_ui_en_api.py` | Een UI-bewerking en een gelijktijdige API-patch laten beide wijzigingen staan; met de oude dubbele save strandt de taak op zijn eigen wijziging |
| `tests/test_concurrent_save_conflict.py` | De compare-and-swap en de driewegsmerge van `save` |
| `tests/test_task_save_base_version.py` | De `base_version`-route in de taakhandler |
| `tests/test_modal_edit_conflict.py` | Een conflict komt als melding in de modal, niet als 500 |
| `tests/e2e/test_sandbox_reallife.py` | Dezelfde race op een echt cluster, door de echte UI en de echte API |
