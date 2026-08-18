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
een, dan wijst de opgeslagen URL naar een adres dat niet meer bestaat, terwijl de gegevens
om hem opnieuw uit te rekenen gewoon in het projectbestand staan.

Dat is vandaag al zichtbaar: de keuzelijst kent een regel `... (niet meer afleidbaar)` voor
precies deze situatie. Die regel bestaat omdat opgeslagen URL's uit de pas lopen.

## Wat we willen

Sla de keuze op en reken de URL uit op het moment van renderen, zodat de knop een
adreswijziging vanzelf volgt.

## De vorm: een samengestelde waarde, met converters

Het nieuwe veld houdt de keuze vast als een enkele string:

    component:deployment:/pad

`:` is veilig als scheidingsteken. Deployment- en componentnamen zijn DNS-1123-labels en
kunnen zelf geen dubbele punt bevatten, en het pad staat achteraan: splits van links met
een maximum van twee, dan blijft een pad met een dubbele punt erin heel. Splitsen op `/`
zou wel misgaan, want een pad bestaat uit schuine strepen.

Het pad is alleen nodig als de component er meer dan een publiceert; dat is dezelfde regel
als het label in de keuzelijst (commit 790480bf). Publiceert de component er een, dan is
`component:deployment` genoeg en blijft het pad leeg.

**Gebruik de bestaande converters, daar zijn ze voor.** `opi/forms/converters.py` kent het
`Converter`-protocol met `to_form` en `from_form`. Een `InviteTargetConverter` zet de
opgeslagen string om naar de waarde die de keuzelijst toont en terug. Kijk naar
`KeyValueConverter` en `YAMLConverter` als voorbeeld van een converter die meer doet dan
een tekst doorgeven.

**Let op de reikwijdte, en dit is het punt waar het meer wordt dan een schemaversie.**
`to_form`/`from_form` gaan over het FORMULIER. De publieke uitnodigingspagina is geen
formulier; die rendert in `invite_routes.py` en heeft dus een eigen resolver nodig die de
bestemming naar een URL omzet. Dat zijn twee verschillende plekken met dezelfde bron
(`public_urls_for_project`) en het is de bedoeling dat ze die bron delen en niet allebei
hun eigen versie krijgen.

## Optioneel, en bestaande uitnodigingen blijven met rust gelaten

Het nieuwe veld is optioneel en `application-url` blijft geldig. We herschrijven bestaande
projectbestanden NIET.

Waarom niet: een uitnodiging is een lopende afspraak met iemand die de link al heeft. Een
migratie die de bestemming omzet, verandert stilletjes waar die persoon uitkomt, en bij een
foute match komt hij ergens anders uit dan de bedoeling was. Bovendien is niet elke URL
afleidbaar: een uitnodiging mag naar een adres buiten dit project wijzen, en dat moet zo
blijven kunnen.

De twee vormen bestaan dus naast elkaar, met een vaste voorrang: is het nieuwe veld gevuld,
dan wint dat; anders de URL; anders geen knop.

Wat er daardoor meer bij komt kijken dan een nieuwe schemaversie:

- **Twee lezers, niet een.** Elke plek die de bestemming leest moet beide vormen aankunnen. Inventariseer ze eerst; `invite_routes.py:960` is er een, de detailsectie en de API-uitvoer mogelijk ook.
- **Precies een van de twee.** Het configmodel moet een entry met allebei de velden weigeren, anders is niet te zien welke telt.
- **De API en de CLI.** Beide vormen moeten in te vullen zijn en de uitleg (`opi/services/catalog/invite/help.md`) moet zeggen wanneer je welke kiest.
- **De keuzelijst.** Die schrijft voortaan de bestemming, maar een bestaande niet-afleidbare URL moet kiesbaar blijven, anders laat opslaan hem vallen.
- **De schemaversie.** `config_schema_version` gaat van 1.0 naar 1.1 voor het nieuwe veld, maar `migrate_config` verandert GEEN gegevens: alle diensten staan nu op 1.0, dit wordt de eerste ophoging, en het is prettig als de eerste er een is die niets herschrijft.

## Wat je moet meten voordat je begint

1. **Hoeveel uitnodigingen hebben vandaag een `application-url`, en hoeveel daarvan zijn nog afleidbaar?** Draai `public_urls_for_project` over de projecten in de store en vergelijk. Dat zegt hoeveel er baat bij hebben, en of de niet-afleidbare gevallen echt bestaan.
2. **Waar wordt de bestemming allemaal gelezen?** Zoek op `application_url` en `application-url` door `opi/`, inclusief templates. De lijst hierboven is een begin, geen garantie.
3. **Kan een `Editable` met converter dit dragen?** Lees `INVITE_APPLICATION_URL_EDITABLE` en bevestig dat validator en converter samen doen wat je nodig hebt, of dat er een tweede `Editable` bij moet.

## Wat er moet gebeuren

1. **Het veld toevoegen** aan `InviteEntry`, optioneel, naast `application-url`.
   Verify: het model weigert een entry met allebei; een entry met geen van beide blijft geldig, want een uitnodiging zonder knop mag.

2. **De converter schrijven** met een eigen test op de randgevallen: geen pad, een pad met een dubbele punt erin, een lege waarde, en rommel die niet te splitsen valt.
   Verify: heen en terug door `to_form`/`from_form` levert dezelfde waarde op.

3. **De resolver schrijven** die een bestemming naar een URL omzet via `public_urls_for_project`, gedeeld door het formulier en de publieke pagina.
   Verify: oplosbaar, niet meer oplosbaar (component weg, publish-on-web uit, deployment verdwenen), en de voorrangsregel bestemming-boven-URL.

4. **De publieke pagina laten oplossen bij het renderen** in `invite_routes.py`. Valt de bestemming niet op te lossen, toon dan GEEN knop in plaats van een kapotte link. Dat is dezelfde keuze die het formulier al maakt: geen knop is beter dan een knop die verkeerd wijst.
   Verify: een test per geval op de HTTP-laag, zodat de template meedoet.

5. **De keuzelijst laten schrijven wat hij toont.** De provider levert al deployment, component en pad per optie.
   Verify: `tests/test_invite_bestemming_paden.py` blijft groen en krijgt er een geval bij waarin de opgeslagen waarde de bestemming is, niet de URL.

6. **De uitleg bijwerken** in `help.md` en een featuredoc, met beide vormen en wanneer je welke gebruikt.

## Wat NIET in deze taak zit

- Bestaande projectbestanden herschrijven. Uitdrukkelijk niet, zie hierboven.
- `application-url` verwijderen of afkeuren.
- Andere diensten ophogen naar 1.1.
- De keuzelijst verbouwen; die is net aangepast (790480bf) en toont het pad waar dat nodig is.

## Klaar als

- Een uitnodiging met een bestemming wijst na een wijziging van het subdomein nog steeds naar het juiste adres, aangetoond met een test die het projectbestand wijzigt en de knop opnieuw rendert.
- Een bestaande uitnodiging met alleen een `application-url` werkt onveranderd, afleidbaar of niet, en is door deze taak niet aangeraakt.
- De volledige unit-suite is groen, plus `uv run ruff check .` en `uv run pyright`.
