# De CLI loopt vast op zes bevindingen

Status: plan, 11 augustus 2026. Aanleiding: het zad-cli-project speelde op 10 augustus een volledig draaiboek af tegen de sandbox-API en kwam niet tot een draaiende applicatie. Zes bevindingen, waarvan één alles blokkeert.

Veel ging wel goed: projecten en componenten aanmaken, dienstconfiguratie per laag, databaseschema's, bijlagen inclusief twee koppelvormen naar hetzelfde bestand, env-vars schrijven, en `pending-rollout` dat netjes 22 wachtende wijzigingen teruggaf. Het gaat hieronder dus over de resterende gaten, niet over een kapotte API.

## Bevinding 1 is de blokkade, en de oorzaak is al gevonden

`:upsert-deployment` faalt met `Error upserting deployment 'productie': 'deployments'`, achter de validatie, op elk project. Die aangehaalde `'deployments'` is een `KeyError`, en hij staat in `opi/manager/project_manager.py:7098`:

```python
project_data["deployments"].append(new_deployment)
```

Directe toegang tot een sleutel die niet hoeft te bestaan. **Een project dat via de API is aangemaakt heeft geen `deployments`**: `POST /api/v2/projects` schrijft bewust een project zonder deployments, want er is nog niets uit te rollen. De eerste deployment die je er daarna aan toevoegt loopt dus tegen die regel aan.

Dat is dezelfde wortel als de fout die gisteren is gerepareerd in de wizard: daar kreeg de eerste deployment geen cluster en geen repository, omdat die van een bestaande deployment werden gekopieerd en er geen bestaande was. Dit is dezelfde aanname op een andere plek, en dat maakt het waarschijnlijk dat er meer van zijn.

**Dus niet alleen deze regel repareren.** Zoek elk pad dat op "er is al een deployment" leunt en toets het op een project dat er nul heeft. De API kan zulke projecten sinds RC-51 maken; alles wat daarna komt moet ermee omgaan.

## Bevinding 2 hangt er waarschijnlijk aan vast

`:refresh` faalt op "Diensten en manifesten bijwerken", ook op een vers project. Meet dit **opnieuw nadat bevinding 1 gerepareerd is**: een project zonder deployments is precies de toestand die `process_project` als mislukking behandelt, en dat is eerder bij het aanmaken via de API afgevangen met `rollout=false`.

Blijft hij dan staan, dan is de melding zelf de tweede bevinding: *"check logs for details"* verwijst naar logs waar een projectgebruiker niet bij kan. Wat een aanroeper zelf kan oplossen, hoort in het antwoord te staan.

## Bevinding 3: env-vars en aliassen zijn niet te lezen

Schrijven kan, lezen niet. De negen `…/values/…`-paden hebben alleen `post`, `patch` en `delete`, en de generieke configlezer geeft een lege lijst terwijl er variabelen staan. De enige plek waar ze opduiken is `env_var_names` in de componentenlijst: genoeg om te zien *dat* ze er zijn, niet om een waarde te controleren.

Dat blokkeert `zad env list` en `zad alias list`. En let op: de `explanation` van de dienst verwijst zelf naar het `values`-pad als de plek waar dit hoort, dus de documentatie belooft iets dat er niet is.

Een `GET` op die paden, met dezelfde vorm als de `POST` erop.

## Bevinding 4: aliaswaarden komen gemaskeerd terug

`{"POSTGRES_HOST": "***"}`. Een alias is een verwijzing naar een platformvariabele (`$DATABASE_SERVER_HOST`), geen geheim: de waarde ís de koppeling. Gemaskeerd zie je dát er een alias is, niet waarheen hij wijst, en dat is precies wat je wilt controleren.

De maskering is op zichzelf goed en moet blijven staan voor wat wél geheim is. Wat hier misgaat is dat één regel twee soorten waarden over één kam scheert. Zoek waar dat onderscheid hoort en maak het daar, niet met een uitzondering op de naam.

## Bevinding 5: een alias naar een niet-bestaande variabele wordt geslikt

`{"KAPOT": "$BESTAAT_ECHT_NIET"}` wordt geaccepteerd, terwijl de beschrijving van de dienst zegt: *"Een onbekende verwijzing is hier een harde fout, anders dan bij een eigen omgevingsvariabele."* Het gedrag en de belofte lopen uiteen, en een typefout valt nu pas op als de container draait.

Voor eigen env-vars is doorlaten juist goed en dat staat ook zo beschreven: een dollarteken in een wachtwoord is geen typefout. Het onderscheid bestaat dus al op papier en moet alleen nog in de code.

## Bevinding 6: `DELETE` van een niet-bestaande deployment meldt succes

Idempotent verwijderen is een verdedigbare keuze, maar dan zichtbaar. Nu is "hij is weg" niet te onderscheiden van "hij was er niet", en in een script leest dat als bevestiging. Laat het antwoord zeggen wat er werkelijk is gebeurd.

## Waar dit getoetst moet worden

**Op de server-sandbox, met de CLI zelf.** Het draaiboek dat deze bevindingen opleverde ligt klaar bij `zad-cli`, branch `v1`, in onze eigen git. Gebruik dat in plaats van een eigen reeks `curl`-aanroepen te verzinnen: dan meet je wat een echte client doet, en zie je meteen of een reparatie het draaiboek verder brengt.

**De keten is aantoonbaar zodra bevinding 1 weg is.** De testimage `e2e-allservices` doet bij het opstarten een echte schrijf-lees-ronde tegen elke gebonden dienst en meldt per dienst OK of FAIL op `/status`. Dat is de eigenlijke opdracht: niet "de API geeft 202" maar "de applicatie draait en zegt zelf dat elke dienst werkt".

## Waar op te letten

**Twee dingen zijn geen bevinding.** De sandbox was vlak voor de run herbouwd en gaf tot 21:05 nog 502; bevinding 1 en 2 zijn daarna opnieuw gemeten en staan overeind, maar meet ze nog een keer op een rustige sandbox voordat je conclusies trekt. En de CLI weigerde zelf een dienst zonder configuratie aan te zetten terwijl de API een lege body accepteert; dat repareert het CLI-project zelf.

**Repareer niet de melding als je de oorzaak kunt repareren.** Bevinding 2 en 6 zijn allebei "het antwoord klopt niet met wat er gebeurde". De verleiding is de tekst aan te passen; de vraag is eerst of het gedrag klopt.

**Elke reparatie hoort een toets te krijgen die op een LEEG project draait.** Dat is de rode draad onder bevinding 1, en het is de toestand die de API sinds RC-51 zelf kan maken. Een test op een project met een deployment erin had geen van deze fouten gevonden.
