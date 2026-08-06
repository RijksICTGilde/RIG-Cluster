# Een schema per versie, zodat een migratie afgerond kan worden

Status: plan, 6 augustus 2026. Niet gebouwd. Aanleiding: het projectschema kan per definitie nooit afgerond worden, en dat blokkeert het opruimen van elke migratie.

## Waarom het schema alleen maar groeit

`project_v2.json` is één bestand voor alle 2.x-versies. En `opi/core/git_monitor.py` valideert een bestand **rechtstreeks uit git, vóór enige migratie**:

```python
# Security gate: validate against the project schema before any
validate_project_schema(content)
```

Dat is als poort verstandig, maar het heeft een gevolg dat niemand bedoeld heeft: zolang er één bestand op schijf staat dat nog een oude vorm draagt, moet het schema die vorm blijven accepteren. Een migratie kan dus wel geschreven worden, maar nooit afgesloten. Het schema groeit alleen.

Dat is geen theorie. Gemeten op 1 augustus, over 47 productiebestanden:

- **30** dragen nog de root-`domains:` van vóór v2.5
- **23** dragen nog het `config.keycloak`-restant van vóór v2.3
- **22** halen de rauwe schemavalidatie niet

Terwijl beide migraties allang bestaan. De reden is dat een bestand zichzelf pas herschrijft bij verwerking (`project_manager.py` slaat op zodra `was_migrated`), en veel projecten worden zelden verwerkt. Een project dat een half jaar met rust gelaten wordt, blijft een half jaar oud.

## De richting, en waarom die al vaststaat

Een bestand declareert zijn versie en wordt gevalideerd tegen het schema van díe versie. Een migratie levert een nieuwe schemaversie op, en de oude blijft staan voor bestanden die nog niet mee zijn.

Dat patroon bestaat al in deze codebase, alleen op serviceniveau: `<service>.v1.0.json` per service, gegenereerd uit het configmodel en drift-gelockt. Dit is hetzelfde idee, één niveau hoger.

`detect_schema_version` geeft de versie van een bestand al exact terug, dus die kant is er.

## Voorstel

1. **Schema per versie op schijf**, naast elkaar, met de huidige `project_v2.json` als de nieuwste. Bepaal daarbij hoe een versie ontstaat: met de hand geschreven of gegenereerd, en wat er gebeurt als iemand een migratie toevoegt zonder een schema. Dat laatste moet luid falen, niet stil doorlopen.
2. **De poort valideert tegen de gedeclareerde versie.** `git_monitor` blijft valideren vóór migratie, want dat is de veiligheidsreden, maar tegen het juiste schema.
3. **Pas dan kan het nieuwste schema opgeschoond.** De oude vormen verhuizen naar de oude schema's; wat nu als restant in `project_v2.json` staat kan eruit.
4. **En dan pas het echte probleem: bestanden die nooit verwerkt worden.** Zolang migreren alleen bij verwerking gebeurt, blijven die 30 bestanden staan. Dat is een aparte beslissing (een sweep die alles migreert, of accepteren dat oude versies blijven bestaan omdat hun schema er nog is) en het hoort in dit plan besloten te worden, niet stilzwijgend overgeslagen.

## Volgorde

1. Vastleggen wat er nu is: welke versies er in productie voorkomen, en welk bestand welke vorm draagt. De cijfers hierboven zijn van 1 augustus en moeten opnieuw gemeten worden.
2. De schema's splitsen, zonder dat er iets aan validatiegedrag verandert. Verifiëren: elk van de 47 bestanden valideert na de splitsing precies zoals ervoor, inclusief de 22 die nu falen.
3. De poort op de gedeclareerde versie zetten. Verifiëren: die 22 falen niet meer, want ze worden nu tegen hun eigen versie gehouden.
4. Het nieuwste schema opschonen, met een test die aantoont dat een oud bestand nog steeds valideert tegen zijn eigen versie.
5. Beslissen over de niet-verwerkte bestanden.

## Waar op te letten

**Dit is een beveiligingspoort.** `validate_project_schema` staat er om te voorkomen dat iets ongevalideerds uit git de verwerking in gaat. Een versie-declaratie komt uit het bestand zelf, dus uit dezelfde onvertrouwde bron: een onbekende of ontbrekende versie moet weigeren, niet terugvallen op "dan maar het nieuwste" of "dan maar het soepelste".

**Verifieer op gemigreerde data, niet op de rauwe.** Dit is eerder misgegaan: `dp-bn7` viel elke verwerking om op een schemagat, en de les was dat het proces in het geheugen migreert vóór het valideert. Draai bij elke controle hier eerst `migrate_to_latest()` en valideer daarna, anders meet je iets anders dan wat er in productie gebeurt.

**Een migratie die stil faalt is erger dan een die weigert.** De 22 bestanden die nu falen doen dat onzichtbaar: `git_monitor` weigert ze en dat wordt nergens gemeld. Dat gat hoort in dit werk opgelost te worden, want anders verplaatst het zich alleen.
