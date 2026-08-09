# Een agent leert ZAD kennen via de API

Status: plan, 9 augustus 2026. Niet gebouwd. Aanleiding: iemand die de ZAD-UI nooit heeft gezien, een CLI of een agent, moet via API-aanroepen kunnen achterhalen welke diensten er zijn, wat ze doen, hoe je ze toepast op project-, deployment- en componentniveau, en welke omgevingsvariabelen ze opleveren.

## Wat er nu is, gemeten

**De projectkant is af.** Beide routes bestaan en lopen allebei op een SSO-token, niet op een projectsleutel:

```
POST /api/v2/projects     aanmaken (RC-51)
GET  /api/v2/projects     opsommen, met naam, omschrijving, rol en sleutel (RC-57)
```

**En er is al een dienstenlijst.** `GET /api/v2/services` bestaat, is projectonafhankelijk en vraagt geen enkele vorm van authenticatie:

| veld | wat het geeft |
|---|---|
| `name` | de identifier zoals hij in de padnamen terugkomt |
| `description` | een regel Nederlands uit de servicedefinitie |
| `configurable` | of er ergens config op kan |
| `targets` | op welke lagen (`project`, `component`, `deployment`, `deployment-component`) |
| `value_targets` | waar losse sleutel/waarde-paren kunnen |
| `config_schema_version` | de versie van het configschema |

Dit deel van de vraag is dus al beantwoord. **De opdracht is aanvullen, niet bouwen.**

**Wat de diensten zelf al declareren.** Gemeten over de 21 geregistreerde diensten:

| | |
|---|---|
| diensten in de registry | 21 |
| met een `help_template` | 21 van 21, allemaal aanwezig, samen 858 regels |
| met een `config_model` (dus een JSON-schema) | 17 |
| met gedeclareerde `variables` | 11 |
| zichtbaar in de dienstenkiezer | 14 (de rest is `hidden` of `SYSTEM`) |

`ServiceDefinition` draagt bovendien `binding`, `kind` (`USER` of `SYSTEM`), `hidden`, `requires`, `cleanup_strategy`, `backup_label` en `secret_class`. `Service` draagt daarnaast `form_exempt_layers`: de lagen waar een dienst bewust config accepteert zonder formulier, mét de reden erbij.

`VariableDefinition` heeft precies wat gevraagd wordt: `name`, `description`, `source` (`direct` of `secret`), `aliases` en `secret_key`.

**Conclusie van de meting: bijna alles staat er al, alleen niet in de API.** Een describe is daarmee grotendeels een projectie van bestaande declaraties, geen tweede documentatiesysteem. Dat is ook de norm waaraan dit plan zich moet houden.

## De vier gaten

1. **Er is geen describe per dienst.** Geen `GET /api/v2/services/{name}`.
2. **De lijst zwijgt over de aard van een dienst.** `kind`, `binding`, `hidden` en `requires` staan er niet in, dus een client kan niet zien wat hij zelf mag kiezen, wat het platform altijd draait, en wat een andere dienst nodig heeft.
3. **De omgevingsvariabelen staan nergens in de API.** Elf diensten declareren ze, met omschrijving en aliassen, en niets van dat alles is opvraagbaar. Dit is het gat dat expliciet gevraagd is.
4. **De uitleg bestaat, maar alleen als UI-opmaak.** De 21 hulpteksten zijn geschreven in ROOS-componenten (`<c-heading>`, `<c-p>`, `<c-ul>`) voor een popup in het portaal.

## De beslissing die vooraf gemaakt moet worden: waar woont de uitleg

Dit is de enige echte keuze in dit plan, en hij bepaalt de omvang. De prozateksten zijn er, ze zijn goed, en ze staan in componentopmaak.

| | wat het kost | wat het oplevert |
|---|---|---|
| **A. De hulptemplate renderen en als HTML teruggeven** | vrijwel niets | een agent krijgt HTML met `utrecht-*`-klassen en zinnen die naar de UI wijzen ("klik op") |
| **B. Server-side de opmaak strippen naar platte tekst** | klein | leesbaar, maar lossy en de koppen verliezen hun structuur |
| **C. De proza naar markdown als enige bron, de UI rendert die** | 21 bestanden omzetten, en de popup in het portaal aanpassen | één bron, geschikt voor zowel mens als agent, koppen blijven structuur |
| **D. Naast elke `help.html.j2` een `help.md` zetten** | klein | twee bronnen die uit elkaar gaan lopen |

**Voorstel: C, maar gefaseerd.** D valt af, dat is drift inbouwen. A en B leveren iets dat werkt maar dat niemand wil onderhouden. C is de enige die de vraag echt beantwoordt, want de vraag is dat een agent het systeem *begrijpt*, niet dat hij er HTML van krijgt.

Gefaseerd betekent: de describe-route landt eerst met alles wat uit declaraties komt (dat is het grootste deel en het is meteen bruikbaar), en het prozaveld komt in een tweede stap als de omzetting naar markdown gedaan is. Wie de volgorde omdraait, bouwt de route om een veld heen dat nog niet bestaat.

Als C te groot blijkt bij de uitvoering, is B een verdedigbare tussenstap, mits opgeschreven dat het tijdelijk is. A niet.

## Wat een describe moet teruggeven

`GET /api/v2/services/{name}`, met per dienst:

1. **Wat het is.** Naam, omschrijving, `kind`, `binding`, `hidden`, en de lange uitleg (zie de beslissing hierboven).
2. **Waar je het toepast.** Per laag uit `targets`: dat de laag ondersteund wordt, en de vorm die de config daar in het projectbestand aanneemt. Inclusief de lagen uit `form_exempt_layers` met hun reden, want "hier kan het wel via de API maar bewust niet via een formulier" is precies wat een API-client moet weten.
3. **Hoe je het instelt.** Het JSON-schema van `config_model`, plus de versie. Dat schema zit vandaag al in de OpenAPI-beschrijving van de PUT-route; hier hoort de verwijzing ernaartoe, niet een tweede kopie.
4. **Wat het oplevert aan omgevingsvariabelen.** Per variabele: naam, omschrijving, of hij uit een secret komt of direct gezet wordt, en zijn aliassen. Een dienst zonder variabelen geeft een lege lijst, niet een ontbrekend veld.
5. **Wat het nodig heeft.** `requires`, in de padvorm die er al staat, zodat een client de afhankelijkheid kan volgen.
6. **Wat er gebeurt als het weggaat.** `cleanup_strategy` en `backup_label`, want dat is het verschil tussen "weg is weg" en "staat nog in de uitgestelde verwijdering".

En de lijst (`GET /api/v2/services`) wordt aangevuld met `kind`, `binding`, `hidden` en `requires`, zodat de lijst al genoeg is om te kiezen en de describe alleen nodig is om toe te passen.

## De databaseschema's, als eigen deelbron

Apart gevraagd en het hoort hier: schema's toevoegen, verwijderen en opsommen via de API.

**Wat er nu is, gemeten.** Schema's bestaan sinds RC-17 in het projectbestand, op de projectlaag van de databasedienst:

```yaml
services:
  - postgresql-database:
      config:
        schemas:
          - postfix: rapportage
            description: Waar de rapportagetool bij mag
            marked-for-deletion: false
```

De volledige naam wordt `{project}_{deployment}_{postfix}`, per deployment, en de verbindingsgegevens komen als `DATABASE_SCHEMA_{POSTFIX}` beschikbaar. Het formulier staat er maximaal twintig toe. De postfix moet een veilige identifier zijn (`^[a-z][a-z0-9_]*$`), en uniciteit, de 63-tekengrens van de volledige naam en botsingen tussen variabelenamen worden bij het opslaan gecontroleerd, niet in het model, omdat daar de project- en deploymentnaam voor nodig zijn.

**Er is geen eigen API voor.** Opsommen kan indirect via `GET /projects/{p}/services/{name}/config`, en toevoegen of verwijderen kan vandaag alleen door de hele dienstconfig met een PUT te vervangen.

**En daar zit het risico.** RC-17 heeft bewust gekozen dat een schema verdwijnen uit de lijst niet betekent dat het weg mag: de data blijft staan, de dienst stopt alleen met beheren en met het aanbieden van de variabele. Maar dat markeren is een veld dat de gebruiker aanvinkt, geen automatisch gevolg van het weghalen van een regel. Een client die de config in zijn geheel terugschrijft met één schema minder, laat dat schema dus gewoon uit het bestand vallen, en dan is de bedoelde veiligheid weg precies op de plek waar een agent hem het hardst nodig heeft: die kent de bedoeling niet, hij kent alleen het schema van het verzoek.

**Wat het moet worden.** Een eigen deelbron, zodat een regel toevoegen of weghalen niet betekent dat je de rest van de config moet meesturen:

```
GET    /api/v2/projects/{p}/services/postgresql-database/schemas
POST   /api/v2/projects/{p}/services/postgresql-database/schemas
DELETE /api/v2/projects/{p}/services/postgresql-database/schemas/{postfix}
```

Met deze regels:

1. **De lijst geeft ook de afgeleide feiten, en begint bij het standaardschema.** Elke database krijgt sowieso een standaardschema, en dat is het schema waar de meeste gebruikers het over hebben. Het staat **niet in het projectbestand**: het wordt afgeleid als `{project}_{deployment}` en aangeboden als `DATABASE_SCHEMA` (alias `APP_DATABASE_SCHEMA`). Een lijst die alleen de `schemas:` uit het projectbestand teruggeeft, laat dus juist het belangrijkste weg. Het hoort er als eerste regel in te staan, herkenbaar als standaard en niet verwijderbaar.

   Per regel: de postfix (leeg voor de standaard), de volledige schemanaam per deployment, de variabelenaam, de omschrijving en of hij gemarkeerd is.

   **Reken de naam uit, vertel de formule niet na.** De twee soorten gedragen zich verschillend bij de 63-tekengrens van PostgreSQL: het standaardschema wordt stil afgekapt (`generate_database_schema`), een extra schema faalt juist hard (`generate_extra_database_schema` gooit een `ValueError`). Wie de naam zelf samenstelt uit project en deployment krijgt bij lange namen dus een schemanaam die niet bestaat. Gebruik de bestaande functies in `opi/utils/naming.py`; dat is precies waarom deze lijst een eigen route verdient in plaats van een verwijzing naar de config.
2. **Verwijderen markeert, en gooit niet weg.** Dat is het bestaande gedrag, nu afgedwongen aan de API-kant in plaats van overgelaten aan de goede bedoelingen van de aanroeper. Het antwoord hoort te zeggen dat de data blijft staan.
3. **Werkelijk laten vallen is een tweede, expliciete handeling.** Dezelfde vorm als bij het verwijderen van een bijlage (RC-52): standaard het veilige gedrag, en het onomkeerbare alleen met een vlag die zegt dat je weet wat je weggooit. Naam en reden vastleggen zoals daar.
4. **Toevoegen is een echte actie, niet een omweg via de config.** `POST` met een postfix en een omschrijving, en de route rekent zelf uit wat de volledige naam en de variabelenaam worden en geeft die terug. Een aanroeper die een schema toevoegt hoort meteen te weten hoe hij er in zijn applicatie bij komt, zonder een tweede aanroep en zonder de naamgevingsregels te kennen.
5. **De controles bij het opslaan blijven waar ze zijn.** Uniciteit, de 63-tekengrens en botsende variabelenamen worden al bij het opslaan afgedwongen; deze routes horen die fouten door te geven als een nette 422, niet ze over te doen.

### De naamgeving zelf, want die is nu niet dicht

Zodra een API schema's laat toevoegen, komt de postfix niet meer alleen uit een formulier maar ook uit een script dat de regels niet kent. Gemeten staat het er nu zo voor:

| controle | waar | dekt |
|---|---|---|
| vorm `^[a-z][a-z0-9_]*$` | `SchemaPostfixValidator` + het configmodel | tekens, niet lengte |
| **lengte van de postfix zelf** | **nergens** | |
| uniciteit binnen de lijst | `UniqueSchemaEnforcer`, bij opslaan | |
| botsing met bestaande variabelenamen | `UniqueSchemaEnforcer` | |
| volledige naam onder 63 tekens | `UniqueSchemaEnforcer` | alleen tegen de deployments die er **nu** zijn |

Drie gaten, oplopend in ernst:

1. **De postfix heeft geen eigen maximum.** De invite-sleutel vlak ernaast heeft er wel een (3 tot 64); deze niet. Een aanroeper kan dus een postfix van 200 tekens insturen en krijgt pas bij de samengestelde controle een klacht die over iets anders lijkt te gaan.

2. **De samengestelde controle kijkt alleen naar het heden.** Een postfix die vandaag past, past niet meer zodra er een deployment met een langere naam bijkomt. Dan faalt het ofwel bij het aanmaken van die deployment, met een foutmelding die naar een schemaveld wijst, ofwel helemaal niet, en dan gooit `generate_extra_database_schema` bij het uitrollen een `ValueError`. Dit is dezelfde klasse als het al bekende probleem met samengestelde namen (`project_composed_name_length`), nu aan de databasekant.

3. **Het standaardschema kapt stil af.** `_truncate_if_needed` doet een kale `name[:63]`, zonder hash. Twee lange deploymentnamen in hetzelfde project kunnen dus tot hetzelfde standaardschema afkappen en ongemerkt in elkaars data zitten. RC-17 heeft dat voor extra schema's bewust voorkomen door hard te falen; de standaardweg is daarbij nooit meegenomen.

**Voorstel.**

- **Geef de postfix een eigen maximum**, en zet het op één plek waar het model, de formuliervalidator en de API hem allemaal uit lezen. Wees eerlijk over wat dat oplost: een vast maximum haalt de samengestelde controle **niet** weg, want hoeveel ruimte er is hangt af van de project- en deploymentnaam. Het zorgt er alleen voor dat de veelvoorkomende fout vroeg en begrijpelijk faalt in plaats van diep in de naamgeving.
- **Normaliseer niet stil, weiger.** De vorm is al streng genoeg dat normaliseren neerkomt op iets anders opslaan dan er gevraagd is. Een aanroeper die `Rapportage` instuurt hoort een 422 te krijgen met de reden, niet een schema dat `rapportage` heet zonder dat hij het weet.
- **Draai de samengestelde controle ook als er een deployment bijkomt**, niet alleen als de schemalijst wijzigt. Dat is het echte gat: nu kan een geldige toestand ongeldig worden zonder dat iemand het merkt.
- **Het stille afkappen van het standaardschema is een eigen bevinding**, geen onderdeel van dit plan. Noteer hem, repareer hem hier niet: een naamgevingsregel veranderen raakt bestaande databases, en dat hoort een eigen taak met een eigen migratievraag te zijn.

## Voorstel

1. **`GET /api/v2/services/{name}`**, typed, met dezelfde registry als bron als de lijst. Een onbekende naam geeft 404 met de geldige namen erbij.
2. **De lijst aanvullen** met `kind`, `binding`, `hidden` en `requires`. Toevoegen aan een bestaand antwoordmodel, dus geen breuk.
3. **De variabelen als eigen deel van het antwoord**, uit `VariableDefinition`. Geen nieuwe bron, geen handwerk.
4. **De prozaomzetting naar markdown**, als tweede stap, met de UI-popup op dezelfde bron.
5. **De schemaroutes** als eigen deelbron, met markeren als standaardgedrag bij verwijderen.
6. **Een dekkingstoets zoals de catalogus die al kent.** `tests/test_service_config_layers.py` houdt elke dienst aan zijn declaraties; dit verdient hetzelfde: elke `ServiceType` levert een volledige describe op, en een nieuw toegevoegde dienst zonder uitleg of zonder variabelenlijst laat de toets vallen. Zonder die toets is dit over een half jaar half gevuld.

## Volgorde

1. De lijst aanvullen met `kind`, `binding`, `hidden`, `requires`. Klein, meteen bruikbaar, en het maakt de vorm van de describe duidelijker.
2. De describe-route met alles wat uit declaraties komt, inclusief de variabelen en de lagen. Verifieerbaar: voor elke van de 21 diensten een antwoord zonder lege verplichte velden.
3. De dekkingstoets, zodat stap 2 niet stilletjes kan verwateren.
4. De schemaroutes. Verifieerbaar: een schema toevoegen en weer weghalen laat het projectbestand geldig achter, en het weggehaalde schema staat gemarkeerd in plaats van verdwenen.
5. De proza naar markdown, en de UI op diezelfde bron.

## Waar op te letten

**Dit mag geen tweede documentatiesysteem worden.** Alles wat de describe teruggeeft hoort af te leiden te zijn uit wat de dienst al declareert. Zodra er prozavelden bijkomen die alleen voor de API bestaan, gaan die uit de pas lopen met het gedrag, en dan is een verkeerd antwoord erger dan geen antwoord. Kan iets niet worden afgeleid, dan is dat een teken dat de declaratie dat feit mist, en hoort het dáár bij.

**De authenticatievraag hoort bewust beantwoord.** De bestaande lijst is publiek en projectonafhankelijk. De describe geeft geen projectgegevens en geen geheimen, maar wel de namen van omgevingsvariabelen en de interne opbouw van het platform. Consistentie pleit voor publiek; wie dat niet wil, moet ook de bestaande lijst afschermen. Kies één van beide en schrijf de reden op.

**De taal is Nederlands.** Alle omschrijvingen en hulpteksten zijn dat, en dat blijft zo; een API die zichzelf half in het Engels uitlegt is verwarrender dan een die consequent Nederlands is. Vermeld het wel in de OpenAPI-beschrijving.

**`aliases` en `user-env-vars` zijn zelf ook diensten.** De variabelen die een gebruiker zelf zet en de aliassen die hij eraan hangt lopen via die twee diensten. Een agent die alleen naar `variables` van de andere diensten kijkt, ziet dus maar de helft van wat er uiteindelijk in een container staat. De describe van die twee hoort dat expliciet te zeggen.

**De OpenAPI-beschrijving is de andere helft van dit antwoord.** RC-45 heeft die verstaanbaar gemaakt; een agent begint daar en niet bij dit endpoint. Deze routes horen daar dus goed in te landen, met voorbeelden, en niet alleen als schema.
