# Typecontrole op aanroepen aanzetten

Status: plan, 6 augustus 2026. Niet gebouwd. Aanleiding: pyright draait hier met de controle op aanroepargumenten uit, dus getypeerde signatures geven documentatie en editorhulp maar geen enkele afdwinging.

## Wat er uit staat

`pyproject.toml` zet **negentien** pyright-regels op `false`. Twee daarvan raken de kern:

```toml
reportCallIssue = false      # klopt deze aanroep met deze functie
reportArgumentType = false   # past dit argument bij dit parameter-type
```

Dat kwam aan het licht toen een proef met `@overload` nul fouten gaf. Met standaardinstellingen geeft diezelfde proef er twee. De typehints in deze codebase worden dus geschreven en gelezen, maar niet gecontroleerd.

## Wat er gebeurt als ze aangaan

Gemeten op 6 augustus, met beide vlaggen tijdelijk aan (371 bestanden nagekeken):

```
182 fouten, verdeeld over 38 bestanden

  argumenttype           102
  geen overload           70
  ontbrekend argument      6
  te veel argumenten       4
```

Eén bestand draagt 42%: `api/router.py` heeft er 76. Daarna `forms/visualizers/flows.py` en `manager/project_manager.py` met 13, en dan snel aflopend.

## Niet alles is een fout, en dat is de kern van dit plan

Drie steekproeven op de tien "harde" gevallen leverden drie verschillende soorten op. Blind repareren zou dus schade doen.

**Vals alarm door een ongetypeerde afhankelijkheid.** Vier meldingen op `connectors/prometheus.py:59` beweren dat `PrometheusConnect` geen parameter `url` kent. De echte signatuur is `(url, headers, disable_ssl, retry, ...)`, dus de aanroep klopt gewoon. Het pakket is ongetypeerd (`# type: ignore[import-untyped]`) en pyright raadt ernaast. Hier is de oplossing een stub of een gerichte onderdrukking, niet de code.

**Een echt typegat waar de code werkt.** `forms/editables/processor.py:211` roept `validator.validate(value, context=...)` aan terwijl het protocol `validate(self, value)` declareert. Het werkt door een `try/except TypeError` eromheen: sommige validators nemen context, andere niet. Pyright heeft gelijk, en de oplossing is het protocol verbreden zodat het beschrijft wat er echt gebeurt, niet de aanroep aanpassen.

**Vermoedelijk echte fouten.** De overige gevallen van "ontbrekend" of "te veel argument" zijn geen typekwestie maar een aanroep die niet klopt. Dat zijn er hooguit een handvol, en die verdienen als eerste aandacht.

## Voorstel

1. **Triage vóór reparatie.** Deel de 182 in die drie bakken. Dat is de eerste en belangrijkste stap; zonder die indeling wordt dit een zoektocht waarbij echte fouten ondersneeuwen in ruis.
2. **De harde gevallen eerst**, want daar zit de kans op een echte bug. Per stuk beoordelen, niet per patroon.
3. **Ongetypeerde afhankelijkheden apart afhandelen.** Een stub of een gerichte `# type: ignore` met de reden erbij; geen algemene uitzondering die ook onze eigen fouten wegmoffelt.
4. **Typegaten dichten door het type te verbreden**, niet door de aanroep te versmallen. Het `validate`-protocol is daar het voorbeeld van: de code doet al iets dat het type niet beschrijft.
5. **Dan pas de vlaggen aan**, per stuk: eerst `reportCallIssue`, daarna `reportArgumentType`, zodat een terugval te herleiden is.
6. **De overige zeventien uitgezette regels inventariseren.** Niet aanzetten, wel opschrijven waarom ze uit staan, want nu weet niemand of dat een besluit was of een restant.

## Volgorde

1. Triage, met de uitkomst per bak vastgelegd in dit plan.
2. De harde gevallen, met per stuk een oordeel: echte fout, vals alarm, of te verbreden type.
3. `api/router.py`, want daar zit 42% en het is waarschijnlijk één of twee patronen die zich herhalen.
4. De rest in groepen per bestand.
5. De vlaggen aan, één voor één, met de suite groen na elke stap.

## Waar op te letten

**Dit is geen opruiming maar een zoektocht naar bugs.** Tien meldingen gaan over aanroepen die niet kloppen met hun functie. Die draaien vandaag gewoon mee. De opbrengst is niet een schoner scherm maar die tien.

**Onderdruk niet wat je niet begrijpt.** De verleiding bij 182 meldingen is een `# type: ignore` waar het rood is. Dat maakt het scherm schoon en de code slechter. Elke onderdrukking krijgt een reden erbij, en "pyright snapt dit pakket niet" is een geldige reden terwijl "geen tijd" dat niet is.

**Het aantal is een momentopname.** Er komt dagelijks code bij. Meet opnieuw voor je begint, en zet de vlaggen aan zodra een bak leeg is, anders loopt het terug tijdens het werk.

**De prijs van uit laten staan groeit.** Elke nieuwe typehint die niet gecontroleerd wordt, is een belofte die niemand nakomt. Dat is precies waarom het haaksysteem (`plans/een-haaksysteem-op-events.md`) op getypeerde payloads leunt: die keuze is alleen wat waard als de controle aanstaat.
