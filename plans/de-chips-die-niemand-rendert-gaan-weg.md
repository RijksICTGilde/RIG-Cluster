# De chips die niemand rendert gaan weg

Gemeten tegen `forgejo/main` op `05314980a`.

## Wat er aan de hand is

`lotc_fixtures.services_overview()` rekent voor elke dienst een lijstje `chips` uit en zet dat in de rij die de dienstenpagina krijgt. Niemand rendert die sleutel.

Gemeten: een grep op `chips` over beide sjabloonbomen (`opi/templates_lotc/` en `opi/templates/`) levert geen enkele lezer op. `bg/_service-card.html.j2` heeft ze er bewust uit gehaald en leest `service.chips` niet; het commentaar bovenin dat bestand legt uit waarom een telling de vraag van de lezer niet beantwoordt.

Dit kwam boven in de review van RC-213, die de sleutel bewust buiten die PR hield omdat het een eigen opruiming is.

## Wat er precies weg moet

In `opi/web/lotc_fixtures.py`, binnen `services_overview()` (de functie begint op regel 536):

- de vier regels die `chips` opbouwen (regel 559 tot en met 565): de selectiechip, `gedeeld per deployment`, `N variabelen` en `vereist N`
- de sleutel `"chips": chips` in de rij (regel 577)

In `tests/test_service_config_location.py`:

- de klasse `TestDeChipsVanServicesOverview` (vanaf regel 211), die als enige `rij["chips"]` leest. De docstring zegt zelf al wat hij is: *"Een scherm hangt er vandaag niet aan."*
- de import van `services_overview` op regel 32, als die daarna nergens anders meer voor dient. Controleren, niet aannemen: `page_data` komt uit dezelfde import.

## Wat er NIET weg mag

**De rest van de rij.** `name`, `label`, `summary`, `icon`, `color`, `kind_label`, `kind_type` en `help` worden wel gerenderd, en `kind_label` wordt bovendien geteld voor de filterknoppen op de dienstenpagina (`lotc_fixtures.py:253` en verder). Alleen `chips` is dood.

**De gerenderde afleiding en zijn toets.** `selection_labels` levert dezelfde twee zinnen aan de dienstkaart op het projecttabblad, dát wordt wel gerenderd als `c-chip`, en `TestDeChipsOpDeProjectpagina` bewaakt het op de echte route. Die blijft zoals hij is.

## Waarom dit dekking oplevert in plaats van kost

`TestDeChipsVanServicesOverview` bestaat om te merken dat de twee afleidingen uit elkaar lopen. Dat is een echte zorg zolang er twee zijn. Haal je de dode tweede weg, dan is er nog één afleiding en kan er niets meer uit elkaar lopen. De toets verdwijnt dus samen met het risico dat hij bewaakte, niet ervoor in de plaats.

Blijft gedekt na deze wijziging: `selection_labels` als functie (de toetsen boven regel 150) en de gerenderde kaart (`TestDeChipsOpDeProjectpagina`), allebei op `selectable_per_component` en `shared_per_deployment`.

## De toets

1. `uv run pytest tests/test_service_config_location.py -x -q` groen, en de klasse die verdween is de enige die verdween.
2. `uv run ruff check .`, `uv run ruff format --check .` en `uv run pyright` schoon. Ruff is hier de toets die telt: een `chips`-variabele die wordt opgebouwd en nergens meer gebruikt, of een import die niemand meer nodig heeft, is precies wat die vangt.
3. Grep ter afsluiting: `chips` komt in `opi/web/lotc_fixtures.py` niet meer voor, en de dienstenpagina rendert nog steeds zijn kaarten met filterknoppen.

## Wat dit niet is

Geen wijziging aan de dienstkaart op het projecttabblad, geen wijziging aan `selection_labels`, en geen herstel van chips op de dienstenpagina. Als iemand die chips daar terug wil, is dat een ontwerpkeuze met een eigen plan; `bg/_service-card.html.j2` legt uit waarom ze er destijds uit gingen.
