# Een bestemming die meebeweegt: sla de component-keuze op, niet de uitgerekende URL

## Wat er nu gebeurt

De uitnodigingsdienst bewaart de bestemming van de succesknop als een kale URL.

- `InviteEntry.application_url`, alias `application-url` (`opi/services/catalog/invite/config_model.py:91`)
- gekozen via `INVITE_APPLICATION_URL_EDITABLE` (`opi/services/catalog/invite/editables.py:121`)
- de keuzelijst komt van `InviteApplicationUrlOptionsProvider` (`opi/forms/visualizers/providers.py`), die de publieke adressen afleidt met `public_urls_for_project` (`opi/services/catalog/publish_on_web/urls.py:126`)
- gelezen op `opi/api/invite_routes.py:960` en getoond in `bg/invite-success.html.j2` en `invite-success.html.j2`, allebei achter `{% if application_url %}`

De keuze WORDT dus al in termen van deployment en component gemaakt. Alleen slaan we
daarna het antwoord op in plaats van de vraag. Een hostname is afgeleid uit het
domeinformaat, het subdomein en het cluster, en al die drie kunnen wijzigen. Verandert er
een, dan wijst de opgeslagen URL naar een adres dat niet meer bestaat, terwijl de
gegevens om hem opnieuw uit te rekenen gewoon in het projectbestand staan.

Dat is vandaag al zichtbaar: de keuzelijst kent een regel `... (niet meer afleidbaar)`
voor precies deze situatie. Die regel bestaat omdat opgeslagen URL's uit de pas lopen.

## Wat we willen

Sla de keuze op (welke deployment, welke component, welk pad) en reken de URL uit op het
moment van renderen. Dan volgt de knop een adreswijziging vanzelf.

## Wat je moet meten voordat je iets kiest

1. **Kan een `Editable` een genest object schrijven, of alleen een enkele waarde op een
   `yaml_path`?** Dit bepaalt de vorm hieronder en is de enige echte ontwerpvraag.
2. **Hoeveel projecten hebben vandaag een `application-url`, en hoeveel daarvan zijn nog
   afleidbaar?** Draai `public_urls_for_project` over de projecten in de store en vergelijk.
   Dat getal bepaalt of de migratie een formaliteit is of echt werk.
3. **Is de dienst-configmigratie ooit gedraaid?** Alle diensten staan op `v1.0`; dit wordt
   de eerste ophoging. Lees `config_schema_version` en `migrate_config` in
   `opi/services/catalog/base.py` (rond regel 590) en bevestig dat convert-then-validate
   werkt zoals de docstring belooft, met een test die een v1.0-config door de nieuwe versie
   haalt.

## De vorm

Voeg aan `InviteEntry` een bestemming toe die de keuze vasthoudt:

```yaml
application-target:
  deployment: production
  component: frontend
  path: /api        # alleen nodig als de component meer dan een pad publiceert
```

`path` is optioneel en volgt dezelfde regel als het label in de keuzelijst (commit
790480bf): een component mag meerdere paden publiceren en dat zijn evenzoveel adressen, dus
alleen daar is een pad nodig om ze uit elkaar te houden. Publiceert de component er een,
dan is deployment plus component genoeg.

**Waarom een object en geen samengestelde string.** Een `production/frontend//api` moet
je splitsen, en een pad bevat zelf schuine strepen. Dat is precies het soort eigen
recordformaat dat eerder misgelezen werd door meerdere lezers (zie
`features/service-orphan-reconciliation.md` en de servicerecord-bugs). Blijkt uit meting 1
dat een `Editable` geen object kan schrijven, kies dan drie losse velden onder
`application-target` boven een gecodeerde string, en schrijf op waarom.

**`application-url` blijft bestaan en blijft geldig.** Niet elke bestemming is afleidbaar:
een uitnodiging mag naar een adres buiten dit project wijzen. Het veld wordt dus niet
verwijderd, het is niet langer de enige vorm.

## Wat er moet gebeuren

1. **`InviteEntry` uitbreiden** met `application-target` (optioneel), naast het bestaande
   `application-url`. Precies een van de twee mag gevuld zijn.
   Verify: het configmodel weigert een entry met allebei, en accepteert een entry met geen van beide (een uitnodiging zonder knop is geldig).

2. **De dienstversie ophogen** naar `1.1` en `migrate_config` schrijven: v1.0 -> v1.1
   probeert elke `application-url` te matchen tegen de afgeleide adressen. Match je hem,
   schrijf dan `application-target` en laat de URL vallen. Match je hem niet, laat de URL
   staan: dat is een extern of verouderd adres en stilletjes weggooien is erger dan bewaren.
   Verify: een test met beide gevallen, en een test die bewijst dat de migratie vooruit-only is en tweemaal draaien niets verandert.

3. **De keuzelijst laten schrijven wat hij toont.** De provider levert al deployment,
   component en pad per optie; de waarde van een optie wordt de bestemming in plaats van de
   URL. De regel `(niet meer afleidbaar)` blijft, want een bestaande URL moet kiesbaar
   blijven zodat opslaan hem niet laat vallen.
   Verify: `tests/test_invite_bestemming_paden.py` blijft groen en krijgt er een geval bij dat de opgeslagen waarde de bestemming is, niet de URL.

4. **Uitrekenen bij het renderen.** Een resolver die een `application-target` omzet naar een
   URL via dezelfde `public_urls_for_project`, gebruikt op `invite_routes.py:960`. Valt de
   bestemming niet meer op te lossen (component weg, publish-on-web uit, deployment
   verdwenen), toon dan GEEN knop in plaats van een kapotte link. Dat is dezelfde keuze die
   het formulier al maakt: geen knop is beter dan een knop die ergens verkeerd heen wijst.
   Verify: een test per geval (oplosbaar, niet meer oplosbaar, alleen een kale URL, niets ingevuld) op de HTTP-laag, zodat de template meedoet.

5. **De uitleg bijwerken** in `opi/services/catalog/invite/help.md` en het featuredoc, zodat
   iemand die dit via de API of de CLI invult weet dat er twee vormen zijn en wanneer je welke gebruikt.

## Wat NIET in deze taak zit

- De API-vorm veranderen voor bestaande clients: `application-url` blijft aanvaard.
- Andere diensten ophogen naar 1.1. Dit is de eerste bump; hou hem klein en goed gedocumenteerd, dan is de volgende makkelijker.
- De keuzelijst zelf verbouwen. Die is net aangepast (790480bf) en toont het pad waar dat nodig is.

## Klaar als

- Een uitnodiging waarvan het subdomein daarna wijzigt, wijst nog steeds naar het juiste adres, aangetoond met een test die het projectbestand wijzigt en de knop opnieuw rendert.
- Bestaande projectbestanden met een `application-url` blijven werken, zowel de afleidbare als de niet-afleidbare.
- De volledige unit-suite is groen, plus `uv run ruff check .` en `uv run pyright`.
