# De wizard levert geen ongeldig projectbestand op

De wizard controleert het afgeronde projectbestand tegen het projectschema voordat het
naar de opslag gaat, en houdt de wizardsessie vast tot het werk is overgedragen. Een
schemafout landt daardoor in de wizard, bij het veld waar hij vandaan komt, in plaats
van in de git-stap van een achtergrondtaak.

## Waarom

Een component zonder startcommando schreef `command: []`, en het schema eist daar
`minItems: 1`. Dat werd pas opgemerkt in de git-stap, en de create-flow gooide de
wizardsessie weg vóórdat die stap draaide. Wat de gebruiker overhield was een
doodlopende pagina met een ontwikkelaarsmelding en geen weg terug naar zijn invoer.

## Wat er nu gebeurt

**Één controle, op het moment dat het bestand af is.** `_validate_finished_project` in
`opi/web/router_wizard.py` draait `validate_project_schema` op precies wat de opslag te
zien krijgt:

- create: in `_start_project_creation`, nadat de generators liepen, de deployment is
  samengesteld, gestagede bijlagen zijn samengevoegd en de dienstverwijzingen zijn
  genormaliseerd — vlak voor het serialiseren naar YAML;
- edit: in `_save_existing_project`, ná het samenvoegen met het opgeslagen project (het
  formulier schrijft een deelverzameling, dus daarvoor bestaat het volledige bestand nog niet).

Er wordt niet eerst gemigreerd, omdat de schrijfweg dat ook niet doet: een gemigreerde
kopie valideren zou de wizard iets laten goedkeuren dat de opslag daarna afwijst.

**De fout wordt op het veld geplaatst.** `ProjectSchemaError` draagt nu `field_path`
(bijvoorbeeld `components/0/command`). `_locate_schema_error` vertaalt dat naar de
editable-notatie (`components[0]/command`) en zoekt de stap die dat veld bezit; de
wizard springt daarheen en toont de melding bij het veld. Lukt dat niet — een schending
op een blok, of op iets dat de wizard niet bewerkt — dan staat de melding bovenaan de
stap waar vandaan is ingediend. Niet kunnen plaatsen is geen reden om niets te tonen.

**Het wordt gelogd als bug.** Komt de wizard hier met een ongeldig bestand, dan
ontbreekt validatie op een veld. Er gaat een WARNING uit met het veldpad, want dat pad
is de plek waar dat gat zit. Het vangnet is niet het doel; het zichtbaar maken wel.

**De sessie gaat pas weg als het werk is overgedragen.** In `_start_project_creation`
staat `clear_wizard_state` nu ná `create_async_task`. Mislukt de indiening, dan staat de
gebruiker nog in de wizard met zijn gegevens. De edit-flow deed dit al zo (die wacht
`save_and_commit_project` af); beide paden volgen nu dezelfde regel. Sessiebestanden
blijven begrensd door de bestaande sweep in `opi/forms/wizard/session.py` (24 uur).

## Lege lijstvelden

`command: []` was één geval van een algemeen patroon: een optioneel veld dat een lijst
schrijft kan leeg een lege lijst neerzetten. `tests/test_empty_list_fields.py` loopt élk
lijst-schrijvend editable in élke flow langs en eist dat leeg níets schrijft daar waar
het schema een lege lijst verbiedt. "Lijst-schrijvend" wordt niet alleen aan de widget
afgelezen (SEQUENCE/MULTI_SELECT/CHECKBOX_GROUP/KEY_VALUE) maar ook aan de converter:
het startcommando is een tekstveld dat via zijn converter een lijst schrijft, en juist
dát geval was de aanleiding. Paden die de schemawandeling niet kan bereiken
(dienstconfig onder `services/<naam>/config/...`) staan expliciet in
`UNREACHABLE_BY_SCHEMA_WALK`, zodat een nieuw geval als testfout opvalt in plaats van
stilletjes buiten de dekking te vallen.

## Tests

- `tests/test_wizard_rejects_invalid_project.py` — de controle zelf, beide opslagpaden,
  de volgorde van het opruimen van de sessie, en het plaatsen van de melding.
- `tests/test_empty_list_fields.py` — de sweep over lijst-schrijvende velden.
- `tests/test_component_command_field.py` — het veld dat de aanleiding was.
