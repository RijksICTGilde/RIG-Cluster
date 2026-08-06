# De wizard levert geen ongeldig projectbestand op

Status: plan, 6 augustus 2026. Niet gebouwd. Aanleiding: een schemafout bij het opslaan laat de gebruiker met lege handen achter. Er wordt niets bewaard, de foutmelding komt uit de git-stap, en terug naar de wizard kan niet meer.

Wat de gebruiker zag:

```
Verwerking mislukt: Failed Git operations: Projectbestand 'nep-wfo' is afgekeurd:
het voldoet niet aan het projectschema (versie 2.6).
Veld 'components/0/command': [] should be non-empty
```

Dat is drie keer mis in één regel: het had niet mogen gebeuren, het werd te laat opgemerkt, en wat er staat is ontwikkelaarstaal.

## Wat er nu gebeurt, gemeten

**De wizard valideert het projectbestand nergens tegen het schema.** In `submit_wizard` bestaat `final_data` als compleet projectbestand, wordt het geprund, gaan de PRE_SAVE-haken eroverheen, en gaat het daarna regelrecht naar de opslag. De enige schemacontrole zit in `project_store` (`validate_project_schema`), diep in de git-stap.

**De create-flow gooit de wizardsessie weg vóórdat het werk gedaan is.** In `router_wizard.py` staat `clear_wizard_state(request)` boven `create_async_task(...)`. Op het moment dat de taak stukloopt is er dus niets meer om naar terug te keren. Dat is precies waarom "terug in de wizard" niet lukt; het is geen ontbrekende knop maar weggegooide toestand.

De bewerk-flow doet het andersom: `save_and_commit_project` wordt eerst afgewacht en pas daarna wordt de sessie geleegd. Daar overleeft een mislukte opslag dus wel. De twee paden verschillen zonder dat iets dat verschil verantwoordt.

## Wat dit plan wil bereiken

1. Een schemafout bereikt de opslag niet meer, omdat de wizard hem eerder ziet.
2. Gebeurt het toch, dan sta je terug in de wizard met je gegevens, niet op een doodlopende pagina.
3. Wat er staat is te begrijpen zonder het schema te kennen.

## Voorstel

1. **Valideer `final_data` waar het compleet is**, in `submit_wizard`, na de PRE_SAVE-haken en na het samenvoegen van gestagede bijlagen, vlak voordat het naar de opslag gaat. Dat is het laatste punt waarop de wizard nog iets kan doen en het eerste waarop het bestand echt af is. Valideer op dezelfde manier als de opslag dat doet, dus na migratie, anders keurt de wizard iets goed dat de opslag alsnog afwijst.

2. **Ruim de wizardsessie pas op als het werk gelukt is.** In de create-flow verhuist `clear_wizard_state` naar ná het aanmaken van de taak, en de sessie blijft staan tot de taak slaagt. Dat is de kern van "ik kan niet meer terug": de toestand was er niet meer.

3. **Vertaal een schemafout naar het veld waar hij vandaan komt.** Een pad als `components/0/command` wijst een editable aan; die weet bij welke stap hij hoort. Lukt die vertaling niet, toon de fout dan bovenaan de laatste stap met de ruwe tekst erbij, in plaats van hem te laten verdwijnen. Niet kunnen plaatsen is geen reden om niets te tonen.

4. **Behandel het als een bug wanneer dit gebeurt.** Als de wizard een ongeldig bestand oplevert, is er een veld zonder validatie of een validatie die niet overeenkomt met het schema. De melding hoort dus ook gelogd te worden met het veldpad, zodat het gat te vinden is in plaats van alleen bij de gebruiker te landen.

5. **Toets de bekende gaten meteen.** Het startcommando was er een: leeg gelaten schreef het `command: []` terwijl het schema `minItems: 1` eist. Dat is gerepareerd (`dd3eb9ed`), maar het patroon is algemeen: elk optioneel veld dat een lijst of een blok schrijft kan hetzelfde doen. Loop de editables langs die een lijst schrijven en controleer of leeg ook echt niets schrijft.

## Volgorde

1. De sessie later opruimen. Dat is de kleinste stap en lost meteen het ergste op: je raakt je invoer niet meer kwijt. Verifiëren: een opslag laten mislukken en terugkunnen.
2. De validatie in `submit_wizard`, met een test die een bekend-ongeldig project door de wizard duwt en aantoont dat de fout in de wizard verschijnt en niet in de git-stap.
3. De vertaling van veldpad naar stap en veld, met de terugval erbij.
4. De inventarisatie van lijst-schrijvende editables, met per geval een test op leeg.

## Waar op te letten

**Valideren op het juiste moment is de hele truc.** Te vroeg en je keurt iets af dat de create-flow daarna nog aanvult (gestagede bijlagen, de samengestelde deployment). Te laat en je bent alweer in de git-stap. Er is precies één punt waarop het bestand compleet is en de wizard nog bestaat; dat punt is het doel van stap 2 en het verdient een opmerking in de code, want het is niet vanzelfsprekend.

**Twee paden, één gedrag.** Create en edit doen dit nu verschillend en dat verschil is niet verantwoord. Kies er een en laat beide dat volgen, anders is de volgende die hier komt weer een halve dag bezig met uitzoeken welk pad hij te pakken heeft.

**De sessie langer bewaren betekent hem ook opruimen.** Een wizardsessie die blijft staan tot iets slaagt, blijft ook staan als de gebruiker gewoon weggaat. Kijk hoe die sessies vandaag verlopen en zorg dat dit plan geen bestanden laat rondslingeren.

**Een nette foutmelding is niet het doel, hem voorkomen wel.** De vertaling uit stap 3 is een vangnet. Als hij vaak zichtbaar wordt, is dat een signaal dat er validatie ontbreekt, niet dat het vangnet beter moet.
