# Waar zit de meerwaarde, en het bestelbusje

*Kritische toets: wat kun je ook zonder ZAD, en wat niet? Plus de analogie uitgewerkt. Cijfers per 31 augustus 2026.*

## Eerst de kritiek serieus nemen: "dit kan VPA toch ook?"

Voor het **tunen van resources** is dat een terechte vraag, en het antwoord is verrassender dan verwacht.

De upstream VPA-recommender heeft een ondergrens: hij adviseert nooit onder de 250 Mi (`podMinMemoryMb`). Dat is prima op een vloot van middelgrote JVM-services. Op onze vloot is het rampzalig:

> **84 procent van onze containers gebruikt minder dan 250 Mi.** 179 van de 213. 42 ervan zitten onder de 25 Mi.

Wat dat betekent als je VPA zijn gang zou laten gaan:

| Applicatieregime | Geheugen | Per maand |
|---|---:|---:|
| **ZAD nu** (gemeten, met slaapstand) | **24,4 GiB** | **610 euro** |
| VPA overal, zoals hij het zou doen | 57,8 GiB | 1.445 euro |
| Geen tuner, 512 Mi overal | 109,4 GiB | 2.735 euro |

**VPA zou deze vloot ruim twee keer zo duur maken als ZAD.** Niet omdat VPA slecht is, maar omdat hij ontworpen is voor een ander soort applicaties dan wij draaien. Wij draaien redirects van 2 Mi, statische sites van 25 Mi en sidecars van 13 Mi. Voor VPA is dat allemaal "250 Mi".

Dat is precies waarom ZAD de VPA-aanbeveling **wel gebruikt maar niet gelooft**: ligt de target exact op de vloer, dan is dat geen meting maar een ondergrens, en valt ZAD terug op de werkelijke Prometheus-meting.

### En het hele plaatje

| | Geheugen | Per maand | Factor |
|---|---:|---:|---:|
| **Met ZAD** | **34,0 GiB** | **849 euro** | **1,0×** |
| Zonder ZAD, maar met VPA overal | 88,1 GiB | 2.201 euro | 2,6× |
| Zonder ZAD, zonder VPA | 185,3 GiB | 4.633 euro | 5,4× |

**VPA dicht ongeveer de helft van het gat.** Dat is eerlijk gezegd meer dan niets, en het is goed dat we dat weten. Maar het kost je wel een clusterbrede operator die iemand moet draaien, en je bent er dan nog steeds niet, want:

- VPA **provisioneert niets**. Geen database, geen realm, geen bucket, geen certificaat.
- VPA kent **geen delen**. Hij maakt jouw eigen Postgres kleiner; hij zorgt niet dat 66 databases op één instantie passen.
- VPA weet niet dat **niemand kijkt**. Slaapstand bestaat niet in zijn wereldbeeld.
- VPA in `Auto`-modus **herstart je pods** om zijn advies toe te passen. In `Off`-modus moet iemand het advies lezen en handelen. Dat "iemand" is precies wat ZAD is.
- Het advies leeft **in het cluster**, niet in git. Verhuizen betekent opnieuw beginnen.

## Waar de meerwaarde dus níet zit

Dit hoort in het praatje, want een zaal ontwikkelaars ruikt een verkooppraatje meteen:

1. **Manifesten genereren is niet bijzonder meer.** Elke agent doet dat in een minuut, en een goed team met een template komt een heel eind.
2. **Voor één project is een platform overhead.** De vaste voet van 6,8 GiB betaalt zich pas terug vanaf een stuk of vijf projecten.
3. **Bij echt zware, unieke eisen helpt delen je niet.** Dan wil je je eigen instantie, en die krijg je ook (`scope: project`, twee projecten doen dat).
4. **Een uitstekend team haalt tachtig procent hiervan zelf.** Dat is waar.

## Waar de meerwaarde wél zit

1. **Toegang tot dingen die je niet zelf kúnt regelen.** Een DNS-record in een rijkszone. Een certificaat op `rijksapps.nl`. Een realm gekoppeld aan Rijkspas. Een image dat door de admission-rewrite komt. Dat is geen gemak, dat is toegang.
2. **Delen mogelijk maken.** Niet verplichten: mogelijk maken. Het loodgieterswerk eronder (aparte credentials, connectielimieten, backup per huurder, opruimen bij verwijderen) is de reden dat delen elders eindigt in "doe maar een eigen instantie".
3. **Het achtenveertigste team krijgt hetzelfde als het eerste.** Een platform is niet voor het beste team. Het is voor het gemiddelde team, en voor het team dat er over twee jaar bij komt.
4. **Compliance als bijproduct in plaats van als project.** Netwerkbeleid, geheimenbeheer, backups, wie-mag-wat: het staat er omdat je een applicatie hebt aangemeld, niet omdat je een audit had.
5. **De slaapstand.** Niemand anders doet dit, en het is het enige mechanisme dat opruimen overbodig maakt in plaats van aanmoedigt.
6. **Het is opschrijfbaar.** Eén leesbaar bestand met wat er draait, wie erbij mag en wie welk domein wanneer goedkeurde.

---

# Het bestelbusje: ik wil een kadootje versturen

Dat is het uitgangspunt. Niet "ik wil een bezorgnetwerk ontwerpen".

| In de analogie | Bij ZAD |
|---|---|
| **Het kadootje** | Jouw applicatie. Het enige wat van jou is en het enige waar jij verstand van hebt. |
| **Inpakken** | De manifesten: Deployment, Service, Ingress, Secret, RBAC, NetworkPolicy. Jij wilt niet leren inpakken, je wilt dat het ingepakt is. |
| **De juiste doos zoeken** | De resources. Geen doos van 2 GiB om een kaartje van 25 Mi te versturen. En groeit het kadootje, dan komt er vanzelf een grotere doos. |
| **Er wat extra's bij doen** | De diensten. Een database, inloggen met Rijkspas, opslag, mail, straks VLAM. Het lintje en het kaartje dat je zelf vergeten was. |
| **De juiste plek in de bus** | Delen en inplannen. Jij hoeft niet te weten waar in de bus je pakket ligt, alleen dat het past en dat er nog wat bij kan. |
| **De kortste route** | GitOps. Van commit tot draaiend, één weg, altijd dezelfde. |
| **De vrachtbrief** | Compliance. Wie mag erbij, welk domein is goedgekeurd en door wie, waar staat de data, is er een backup. |
| **De weegschaal** | VPA. Nuttig, en we gebruiken hem. Maar zijn kleinste streepje is 250 gram terwijl 84 procent van onze pakketjes lichter is. |

## De vier zinnen die het dragen

> **Een weegschaal is geen bezorgdienst.** VPA vertelt je hoe zwaar je pakket is. Hij pakt niets in, zoekt geen doos, rijdt niet en levert niet af.

> **Een koeriersdienst heeft één manier van werken, en dáárom is het goedkoop en betrouwbaar.** Zou elke afzender zijn eigen doos kiezen en zijn eigen route rijden, dan heb je geen koeriersdienst maar achtenveertig mensen met een auto. Dat is wat een golden path is.

> **De vrachtbrief is geen bureaucratie, het is waarom het pakket aankomt.** En waarom je achteraf kunt aantonen wat er gebeurd is. Bij ons is het projectbestand de vrachtbrief.

> **Jij hebt een kadootje. Wij hebben een busje.** Jij hoeft alleen te weten wat erin zit en waar het heen moet.

## Waar de analogie eerlijk moet zijn

- **Een koerier bezorgt en is klaar. ZAD blijft draaien.** Elke nacht opnieuw meten, bijstellen, slapen leggen, wakker maken. Het is geen bezorging maar een abonnement.
- **Wil je een vrachtwagen vol staal versturen, dan is een bestelbusje het verkeerde antwoord.** Dan wil je je eigen instantie, en die krijg je.
- **Voor één pakket per jaar huur je geen koerier.** Voor één applicatie is een platform overhead.

## Waarom "less is more" hier klopt

Het gaat niet om minder functionaliteit. Het gaat om minder beslissingen.

| | Met ZAD | Zonder ZAD |
|---|---:|---:|
| Beslissingen die jij moet nemen | wat je nodig hebt | wat je nodig hebt, plus hoe, waar, hoe groot, hoe vaak, met welk beleid |
| Dingen die iemand beheert | 7 | 67 |
| Databases | 66 op 1 instantie | 26 tot 66 instanties |
| Realms | 26 op 1 instantie | 26 instanties |
| CPU gereserveerd | 6,3 cores | tot 207 cores |

En het bewijs dat het werkt is niet de rekening, het is dit: **twaalf van de 48 projecten hebben genoeg aan een webadres, en de andere 36 nemen er gemiddeld drie voorzieningen bij zonder dat iemand daar een gesprek over heeft gevoerd.**
