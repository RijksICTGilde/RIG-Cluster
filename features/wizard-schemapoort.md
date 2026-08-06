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

**De melding herhaalt de afgekeurde waarde niet, en staat nooit in het veld zelf.**
Twee redenen, allebei hard:

- *De waarde kan een geheim zijn.* De bewerk-flow valideert het bestand samengevoegd met
  het opgeslagen project, en jsonschema zet de afgekeurde waarde in zijn eigen melding.
  Dat zou `config/api-key`, `config/age-private-key` of `user-env-vars` naar de browser
  én naar de WARNING-log kunnen schrijven. `ProjectSchemaError` draagt daarom naast
  `field_path` een `reason`: een beschrijving die alleen uit de schemaregel is opgebouwd
  ("de waarde heeft niet de vorm die het schema voorschrijft"). Wat de gebruiker en het
  log te zien krijgen is veldpad + reden, gebouwd door `_validation_message_without_values`.
- *Wat in een veldmelding staat, wordt twee keer gerenderd.* Veldmeldingen komen in
  `step_html` terecht en `wizard_step.html.j2` haalt dat door `process_components`, dat de
  HTML nóg een keer als Jinja-template uitvoert. HTML-escapen helpt daar niet: `{{ }}`
  heeft geen bijzondere tekens nodig. Het veld krijgt daarom alleen een constante markering
  (`SCHEMA_FIELD_MARKER`); de tekst zelf staat in `global_errors`, dat binnen de template
  wordt gerenderd — één render, met autoescaping. Als extra grendel breekt
  `_defuse_template_syntax` Jinja-delimiters in álle veldmeldingen en -waarschuwingen die
  naar `_render_step_html` gaan; dat dekt ook de gewone formuliervalidators, die de
  ingevulde waarde wél in hun melding citeren ("Ongeldige waarde: ...").

**Het wordt gelogd als bug.** Komt de wizard hier met een ongeldig bestand, dan
ontbreekt validatie op een veld. Er gaat een WARNING uit met het veldpad en de reden
(niet de waarde), want dat pad is de plek waar dat gat zit. Het vangnet is niet het doel;
het zichtbaar maken wel.

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
  de volgorde van het opruimen van de sessie, het plaatsen van de melding, en dat de
  melding de waarde niet herhaalt en niet als template kan worden uitgevoerd.
- `tests/test_empty_list_fields.py` — de sweep over lijst-schrijvende velden.
- `tests/test_component_command_field.py` — het veld dat de aanleiding was.
