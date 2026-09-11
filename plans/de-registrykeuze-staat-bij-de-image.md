# De registrykeuze staat bij de image

**Status**: plan, nog niets gebouwd. Vervolg op RC-177.
**Datum**: 2026-09-11
**Context**: `features/image-registries.md`, `plans/private-images-uit-een-eigen-registry.md`, `instructions/services.md`

## Wat er nu niet goed voelt

Een afnemer die een private image wil draaien moet vandaag drie dingen doen op drie plekken: de dienst aanvinken op projectniveau, de registry configureren, en daarna de dienst nóg een keer aanvinken bij het component om er een registry te kunnen kiezen. Technisch klopt dat, want de koppeling is componentconfig van een dienst en zo werkt elke componentgebonden dienst. Maar voor deze ene vraag is het te veel: je hebt een image ingetypt en je wilt zeggen waar hij vandaan komt.

De volgorde moet omgekeerd: de keuze staat er altijd, bij het image-veld, en het aanvinken volgt uit wat je kiest.

## Wat we bouwen

1. **Projectniveau blijft zoals het is.** Dienst aanvinken, configureren in een eigen blok, net als attachments. Daar verandert niets aan.
2. **Zodra het project minstens één private registry heeft, staat er bij elk component met een image een keuzeveld "Registry"**, zonder dat de dienst bij dat component is aangevinkt. Heeft het project er geen, dan is dat veld er ook niet: de componentvorm blijft dan precies zoals hij nu is, en dat geldt vandaag voor 47 van de 49 projecten.
3. **De standaardwaarde is "Publieke registry, geen token nodig"**, en die schrijft niets weg. Geen dienstvermelding, geen configblok, geen sleutel. Afwezig betekent publiek, precies zoals nu.
4. **Kies je wel een registry, dan materialiseert dat de dienstvermelding** op dat component. De keuze is de selectie.
5. **De eerste registry voeg je altijd toe in het dienstblok**, want daar komt het veld vandaan. Een "nieuwe registry toevoegen" in dezelfde keuzelijst kan later nog, voor de tweede en verder, maar dat is een gemak en geen onderdeel van dit plan.

## De nieuwe haak: een plek in plaats van een aanhangsel

Vandaag hangt `_service_component_layouts()` de layoutknopen van elke dienst ACHTER de handgeschreven velden van de componentvorm. Voor metrics-scraper klopt dat: een eigen fieldset onderaan. Voor deze keuze niet, want hij hoort naast het veld waar hij over gaat.

De componentvorm heeft dat veld handgeschreven staan:

```python
Fieldset(legend="Identificatie", children=["name", "image", "command"])
```

Voorstel: één benoemde plek in die fieldset, direct na `image`, die diensten kunnen vullen. Het formulier bepaalt dus WAAR de plek zit, de dienst bepaalt WAT erin komt. Dat is dezelfde rolverdeling als nu, alleen met een tweede bestemming naast "onderaan".

Twee vormen zijn denkbaar en de keuze is niet vrijblijvend:

- **Een slot in de layout** (voorkeur): de fieldset noemt de plek, een dienst declareert bij zijn layoutknopen in welk slot ze horen, en zonder opgave blijft het gedrag zoals het is. Voorspelbaar, want de volgorde in het formulier blijft van het formulier.
- **Een anker op de knoop** (`after="image"`): de dienst zegt waar hij landt. Flexibeler, maar dan bepaalt een dienst de volgorde van een formulier dat hij niet bezit, en twee diensten die hetzelfde anker kiezen vechten.

### Waar de zichtbaarheid vandaan komt

Het veld verschijnt wanneer zijn eigen keuzelijst iets te kiezen heeft, dus wanneer de options-provider minstens één registry uit de dienstconfig van dit project teruggeeft. Geen aparte voorwaarde ernaast die uit de pas kan lopen met de lijst: één bron, en de zichtbaarheid volgt eruit.

## De val die we hier bewust opzoeken

`instructions/services.md` waarschuwt expliciet: zaai geen configdefaults op iets dat de dienst niet gekozen heeft, want het pad-filter materialiseert de dienst als bijwerking en dan wordt een default stilletjes een selectie. Dat is precies wat we hier WEL willen, maar alleen in één richting:

- een niet-publieke waarde materialiseert de dienstvermelding
- de standaardwaarde schrijft niets en haalt een bestaande vermelding juist weg
- een leeg formulier dat niemand heeft aangeraakt verandert niets

Dat verschil moet in de converter zitten en niet in het formulier, anders geldt het alleen voor de UI en niet voor de API. Leg het vast met een test per richting.

## Moet de upstream altijd ingevuld worden

Nee, maar hij is wel altijd nodig, en dat is niet hetzelfde.

Op een cluster dat rechtstreeks kan pullen is een token in principe genoeg: de image draagt de host al, en het dockerconfigjson-secret heeft alleen die host als sleutel nodig. Op ODCN niet: daar wordt een proxy-organisatie aangemaakt voor precies één upstream-namespace, en die moet je kennen voordat er een image is.

Daarom niet het veld weghalen, maar de vraag wegnemen: voeg je een registry toe vanaf het image-veld, dan vullen wij de upstream in uit die image (host plus eerste padsegment) en laat je hem zien met de mogelijkheid hem bij te stellen. Wie het projectblok gebruikt zonder image in beeld vult hem zelf in.

## Hoort hierbij

Twee dingen uit hetzelfde gesprek die dezelfde belofte dragen, "wij fixen het onder water":

- **Een geplakte browser-URL wordt een upstream.** `https://code.overheid.nl/robbert.uittenbroek/-/packages` wordt `code.overheid.nl/robbert.uittenbroek`. Er ligt al een halve versie: `_normalize_upstream()` in `schema_migration.py`, privé en alleen voor de migratie. Publiek maken in het dienstpakket, uitbreiden met de bekende vormen (Forgejo packages, GitHub org packages, Docker Hub, GitLab container registry, een volledige image-referentie), en door de migratie laten hergebruiken.
- **De naam mag een vrij label zijn.** Nu moet de afnemer een DNS-label typen en krijgt hij anders een melding over kleine letters en streepjes. Het project doet dit al goed met `name` plus `display-name`, en `opi/utils/project_names.py` heeft de generator inclusief het uniek maken. Dezelfde vorm een niveau lager: label is vrije tekst, slug wordt afgeleid, en de slug is bevroren zodra hij bestaat, want hij is de verwijzing vanaf componenten en hij zit in de naam van het dockerconfigjson-secret.

Allebei horen ze op de plek waar het formulier en de API samenkomen, dus in het configmodel en niet in de converter van de editable.

## Fasering

1. **De haak.** Het slot in de componentvorm, met de bestaande diensten ongewijzigd (geen slot opgegeven blijft onderaan). Verifieer: metrics-scraper staat nog waar hij stond, en een testdienst kan in het slot landen.
2. **Het veld.** Altijd zichtbaar bij een component met een image, standaard publiek, met de materialisatie in de converter en een test per richting.
3. **De invoerhulp.** Upstream uit een geplakte URL, upstream voorgevuld vanaf de image, en het vrije label met afgeleide slug.

## Open

- Slot of anker. Ik neig naar het slot, zie hierboven.
- Geldt hetzelfde veld ook op een deployment-component, waar de override zit? Waarschijnlijk ja, met dezelfde standaard.
