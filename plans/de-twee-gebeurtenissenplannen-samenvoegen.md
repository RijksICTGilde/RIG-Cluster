# De twee gebeurtenissenplannen samenvoegen

Er zijn twee onafhankelijk geschreven plannen over hetzelfde onderwerp. Ze staan allebei op `main` sinds commit `e98a7811`. Deze taak voegt ze samen tot één set van drie documenten en ruimt de dubbele op. Het resultaat is documentatie. Geen productiecode, geen migratie, geen schemawijziging.

| Herkomst | Bestanden | Omvang |
|---|---|---|
| RC-148, "meldingen" | `plans/meldingen-inventarisatie.md`, `plans/meldingen-oplossingsrichtingen.md`, `plans/meldingen-plan-van-aanpak.md` | 2025 regels |
| RC-149, "gebeurtenissen" | `features/futures/gebeurtenissen-inventarisatie.md`, `features/futures/gebeurtenissen-vastleggen-en-melden.md`, `features/futures/gebeurtenissen-plan-van-aanpak.md` | 625 regels |

## Waarom dit kan zonder iets weg te gooien

De twee aanbevelingen spreken elkaar niet tegen. RC-148 komt uit op "richting C in de vorm van D, met de gebeurtenistabel van B eronder": een onveranderlijke gebeurtenistabel plus een ontvangertabel per persoon, met kolomnamen die het NL GOV-profiel volgen. RC-149 komt uit op een eigen tabel in rig-db met soorten meteen in reverse-DNS-notatie zodat een CloudEvents-projectie later een hernoeming is, eerst een tijdlijn in de portal achter de bestaande autorisatie, en pas daarna push-kanalen. Dat is dezelfde onderlaag, dezelfde volgorde en dezelfde houding tegenover de standaard.

De twee sets zijn bovendien op verschillende dingen sterk. RC-148 heeft de eigenlijke catalogus van gebeurtenissen en de kanalen. RC-149 heeft de begripskwestie, de autorisatie, en de bewaartermijn met de AVG- en BIO-kant. Samenvoegen levert daarom een beter document op dan elk van beide, en niet een langer document.

## De ene echte tegenspraak, die beslecht moet worden

RC-148 zegt: volg het NL GOV-profiel **op het record**, dus op de kolomnamen, en laat de Abonneren-standaard en de notificatiedienst-API buiten de deur. RC-149 verwerpt CloudEvents juist **als intern opslagformaat**, met twee redenen: het profiel vraagt een OIN die nergens in deze repo staat, en het verbiedt persoonsgegevens in de context-attributen terwijl onze actor een e-mailadres is. RC-149 accepteert het alleen als projectie op de rand.

Beslecht dit zichtbaar in de tekst. Beide redeneringen zijn opgeschreven en beide zijn houdbaar; wat niet mag is dat er één stil wint en de andere verdwijnt. Schrijf op wat er wordt gekozen, waarom, en wat de verliezende redenering aandroeg dat overeind blijft.

## Het doel

Drie documenten in `features/futures/`, onder de bestaande namen, want daar staan ze al en *gebeurtenis* is het beslechte woord:

1. `features/futures/gebeurtenissen-inventarisatie.md`
2. `features/futures/gebeurtenissen-vastleggen-en-melden.md`
3. `features/futures/gebeurtenissen-plan-van-aanpak.md`

De drie `plans/meldingen-*.md` worden verwijderd in dezelfde commit waarin hun inhoud is opgenomen. `plans/meldingen-onderzoeksopdracht.md` blijft staan; dat is de opdracht en geen resultaat.

## Per document: wat de basis is, en wat er van de andere kant in moet

### Document 1, de inventarisatie

**Basis: `plans/meldingen-inventarisatie.md`.** Dat is de enige van de twee met een echte catalogus: negen domeingroepen (taken, aanvragen en goedkeuringen, gezondheid, automatisch ingrijpen, backups, leden en toegang, beheerdersgebeurtenissen, kortlopende workloads, wat er al een kanaal heeft), met per gebeurtenis een codeanker, het onderwerp, de belanghebbenden, de ernst en een standaardkanaal. Neem ook de leeswijzer over, de rollentabel die uit de code is afgeleid, en het onderscheid tussen "de toestand bestaat, de gebeurtenis niet" en "de toestand bestaat ook niet", want dat onderscheid draagt de kostenschatting van het hele plan.

**Wat uit RC-149 mee moet:**

- De indeling naar publiek (groep A: wat de gebruiker van een project wil weten; groep B: de platformbeheerder; groep C: een agent of script op de API). Voeg die toe als dwarsdoorsnede of als kolom, niet als een tweede lijst ernaast, want twee lijsten over dezelfde gebeurtenissen lopen gegarandeerd uit de pas.
- De sectie over toestand die wel wordt bepaald maar niet als gebeurtenis bestaat: gezondheid en afwijkingen worden per paginabezoek herberekend en nergens bewaard, waardoor "sinds wanneer is dit rood" onbeantwoordbaar is.
- De inventaris van wat er aan meld- en exportinfrastructuur ligt: OpenTelemetry volledig aanwezig maar uitgezet, Prometheus dat alleen procesinterne toestand van OPI exporteert, de Kubernetes-events in de `events`-kolom, ntfy, en de mailrelay.
- De rangschikking van de gebeurtenissen die vandaag verloren gaan en het duurst zijn om te missen.
- De sectie "Wat niet is geverifieerd".
- De vaststelling dat elke commit onder één vaste identiteit gaat (`GIT_COMMIT_AUTHOR_NAME` in `opi/connectors/git.py`), zodat de git-historie wel vertelt wat er veranderde en niet wie het deed.

### Document 2, vastleggen en melden

**Basis: `plans/meldingen-oplossingsrichtingen.md`.** Neem over: de vier richtingen A tot en met D, de vergelijkingstabel over de zes assen, de aanbeveling met de onderbouwing waarom richting B alleen afvalt (de drie argumenten, waarvan de eerste twee correctheidsbezwaren zijn), en de vijf uitgewerkte onderdelen daarna: waar de gebeurtenis ontstaat, betrouwbaar afleveren, ontdubbelen en samenvoegen, de verhouding tot het audittrail, en het datamodel.

**Wat uit RC-149 mee moet:**

- "De begripsbotsing, en het besluit" in zijn geheel. Drie betekenissen van het woord *event* in deze codebase, het besluit dat het derde een *gebeurtenis* heet, dat `ActionEvent`/`UIEvent` en de `events`-kolom ongemoeid blijven, en de prijs die daarbij hoort (naar buiten toe heet het bij de standaarden een event, dus de exporteur is een vertaalplek).
- "Wie mag welke gebeurtenis zien", inclusief de scherpste rand ervan: wat er in een gebeurtenis terechtkomt, de twee gemeten precedenten van geheimen die in een logregel kunnen belanden, de aanbeveling dat een gebeurtenis zijn eigen velden opbouwt en nooit een vrije `str(exception)` overneemt, en de aanbeveling van één redactiefunctie op de schrijfweg en niet op de leesweg.
- "Bewaartermijn en persoonsgegevens" in zijn geheel: waarom het Logboek Dataverwerkingen hier niet geldt en waarom dat een inhoudelijk antwoord is, waarom de BIO2 wel geldt, de twee termijnen (90 dagen voor de gebeurtenis met pseudonimisering in plaats van verwijdering, en langer voor beveiligingsgebeurtenissen als aparte beslissing), opruimen via het bestaande `cleanup_old_tasks`-patroon zonder nieuwe scheduler, en het argument dat een afmeldpad goedkoop hoort te zijn.
- De vier afgevallen richtingen die RC-148 niet heeft: alleen gestructureerd loggen naar Loki, OTLP als bron van waarheid, Kubernetes-events op de projectnamespace, en abonnementen in het projectbestand. Elk met de reden die er al bij staat.

**Eén extra opdracht op dit document.** RC-149 tekent bij zijn BIO2-uitspraken aan dat de skill `bio` in die sessie niet beschikbaar was, en dat de uitspraken over 8.15.01, 8.15.02 en 8.15.04 daarom niet onafhankelijk geverifieerd zijn. Die skill is nu wel beschikbaar. Controleer die drie tegen de skill `bio` en haal het voorbehoud weg, of scherp de tekst aan als de skill iets anders zegt.

### Document 3, het plan van aanpak

**Basis: `plans/meldingen-plan-van-aanpak.md`.** Neem over: de vier kanalen (UI, API, e-mail, Mattermost) elk uitgewerkt, de voorkeuren per persoon, de fasering en de openstaande beslissingen.

**Wat uit RC-149 mee moet:**

- "De kleinste eerste stap": de tabel plus precies één schrijfweg, namelijk de resource-tuner, met de onderbouwing waarom juist die (`$defs/resource-history-entry` in `opi/schemas/project_v2.json` heeft al een timestamp, een `source` uit een gesloten lijst, een deployment en een `reason`; er ontbreekt alleen een actor, en bij een scheduler is de actor de scheduler) en met de verifieerbare uitkomst die erbij staat.
- De volgorde van de kanalen met de onderbouwing: eerst de tijdlijn in de portal, daarna push in de volgorde webhook, Alertmanager, mail, omdat dat de volgorde is van "in ons beheer" naar "afhankelijk van een keten die aantoonbaar nog niet af is", en omdat een meldkanaal dat stil faalt erger is dan geen meldkanaal. Als de kanaalvolgorde van RC-148 hiervan afwijkt, beslecht dat expliciet in de tekst.
- De vermelding van de meetbasis, dus tegen welke commit er gemeten is.

## Randvoorwaarden

- Gooi niets van waarde weg. Staat een passage in beide sets, houd dan de preciezere. Spreken ze elkaar tegen, beslecht het dan zichtbaar in de tekst in plaats van er stil één te laten winnen.
- De onderlinge verwijzingen tussen de drie documenten kloppen na de samenvoeging. Zoek ook naar verwijzingen elders in de repo naar `plans/meldingen-*.md` en werk die bij.
- Nederlands, geen emoji, alinea's op één regel dus geen harde regelafbrekingen midden in een zin.
- **Geen em-streepjes.** De meldingendocumenten bevatten er nu nog; de gebeurtenissendocumenten zijn er al vrij van. Gebruik een dubbele punt na een kop of een vetgedrukt label, een komma in een tussenzin, en haakjes bij een echte terzijde. Controleer met een grep voordat je commit.
- Elk feit wijst een bestand aan. Wat niet geverifieerd is, staat er met die vermelding bij.
- Zelfbedachte namen voor tabellen, kolommen, eventtypes of endpoints blijven gemarkeerd als VOORSTEL.
- Geen productiecode, geen migratie, geen schemawijziging.

## Wanneer dit af is

1. De drie documenten staan in `features/futures/`, en de drie `plans/meldingen-*.md` zijn weg. `plans/meldingen-onderzoeksopdracht.md` staat er nog.
2. Elk van de RC-149-secties die hierboven bij naam is genoemd, is terug te vinden in het samengevoegde geheel. Een reviewer die op de kopregels zoekt, vindt ze.
3. De catalogus uit RC-148 is compleet overgenomen: alle negen domeingroepen staan er, met hun ankers.
4. De tegenspraak over CloudEvents als intern formaat is expliciet beslecht, met de reden erbij.
5. De drie BIO2-uitspraken zijn tegen de skill `bio` gecontroleerd, en het voorbehoud is weg of vervangen door wat de skill werkelijk zegt.
6. `grep -c "—" features/futures/gebeurtenissen-*.md` geeft nul voor alle drie.
7. Er is niets gewijzigd buiten `features/futures/` en `plans/`.
