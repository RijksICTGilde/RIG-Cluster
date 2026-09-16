# ZAD-pitch, Digigilde kick-off

*Raamwerk op basis van je eigen ingesproken versie. Steekwoorden om op te bouwen, steekzinnen om letterlijk te landen. Vijf minuten, gesproken, geen slides.*

> ## De elevator pitch
> **"ZAD is de control plane voor deployment. Het vult het gat tussen applicatieontwikkeling en platform."**
>
> Dat zei je zelf, aan het eind. Het is de kortste en beste samenvatting die er ligt. Zet hem aan het **begin** én aan het eind: dan weet de zaal meteen waar je heen gaat, en onthouden ze het.
>
> ### En de opvatting eronder
> Je zegt wat je nodig hebt in plaats van hoe het moet. Je krijgt wat je nodig hebt in plaats van wat je vraagt. En wat jij niet gebruikt, is er voor iemand anders.
>
> Dat is het gedachtegoed, en het is het meest onderscheidende aan het project. **Maar noem het pas in het slot.** Een filosofie moet je verdienen, niet aankondigen.

**De rode draad:** it works on my machine → maar het ergens neerzetten is een ander vak → en dat doen we allemaal apart, steeds opnieuw → en wat je krijgt is beschikbaar maar niet bruikbaar → dus: klik, klik, klaar → plus alles wat eromheen hoort → hier komt het vandaan → hier draait het → en dit is wie we zijn.

---

## 1 · Wie ben ik, en wat is dit &nbsp;·&nbsp; 0:00 tot 0:30

- Robbert Uittenbroek, project **ZAD**: Zelfservice Applicatie Deployment

> **"ZAD is de control plane voor deployment. De laag die regelt wat jouw applicatie nodig heeft."**

- Kubernetes en de tools eromheen, dát is het platform. ZAD zit daarboven
- jij praat niet met Kubernetes, jij praat met een knopje

> **"ZAD is de control plane voor deployment. Het vult het gat tussen applicatieontwikkeling en platform."**

- *pauze. Laat dat hangen, de rest van de vijf minuten is uitleg*
- **niet zeggen "ik denk dat het een platform is".** Twijfel in de eerste zin hoort een zaal
- *achter de hand, alleen als een engineer doorvraagt:* het is een **control plane voor wat een applicatie nodig heeft**. Wat Kubernetes met containers doet, doet ZAD met diensten: jij zegt wat je wilt, en er is iets dat blijft duwen tot het er is

## 2 · Waarom bestaat het &nbsp;·&nbsp; 0:30 tot 0:55

> **"Eigenlijk omdat ik het een goed idee vond."**

- klinkt raar, maar er was geen klantvraag
- **geen externe klant die zei "wij zitten met dit probleem"**
- ik zat met het probleem, en volgens mij velen met mij

## 3 · Het probleem &nbsp;·&nbsp; 0:55 tot 1:35

> **"It works on my machine."**

- je schrijft je code, je zorgt dat het in een Docker-image zit, en dat wil je ergens neerzetten zodat een ander erbij kan
- concreet: *"dit is mijn applicatie, die staat op een publieke repo, en die wil ik op `www.robbert.nl` zetten zodat anderen erbij kunnen"*

> **"Het schrijven van een applicatie en het neerzetten van een applicatie zijn hele verschillende disciplines."**

- *pauze na "disciplines"*

## 4 · De hobbels &nbsp;·&nbsp; 1:35 tot 2:10

- je moet een platform aanvragen. Tegenwoordig een Kubernetes-platform

> **"Als je weet wat dat is, leuk. Als je niet weet wat het is: nou ja, dat heb je nodig."**

- dan moet je het inrichten zodat je applicatie er terecht kan
- en nog eens inrichten zodat je het kunt bijwerken
- **er komen heel veel stappen kijken bij deployment**

> **"Deployment is eigenlijk een vakgebied apart."**

- *optioneel, 12 sec, werkt bij een gemengde zaal:* vroeger was er een duidelijke grens, jij leverde een war-bestand en beheer zette het in de JBoss. Die grens verdween toen alles code werd, maar er is nooit een nieuwe afgesproken

## 5 · En we doen het allemaal apart &nbsp;·&nbsp; 2:10 tot 2:40

- bij heel veel teams gaat dit op dezelfde manier: **copy-paste**, door mensen die er een beetje verstand van hebben, of heel veel, mag ook
- de platformmensen bouwen de deployments, services, ingresses, network policies, databases, storage

> **"Maar eigenlijk zijn het steeds herhalingen van zetten."**

## 6 · Beschikbaar is niet bruikbaar &nbsp;·&nbsp; 2:40 tot 3:05

> **"En alles wat je doet is beschikbaar, maar niet bruikbaar."**

- je zet een database neer, en dan moet je die database nog inrichten
- je richt bucket storage in, S3, en dan moet je dat ook weer doen

> **"Je wil heel veel, en je wil eigenlijk niet alles zelf doen. Althans niet steeds weer opnieuw."**

- *dit is je sterkste eigen vondst. Rustig uitspreken, dit is het scharnier van het hele verhaal*

## 7 · Wat ZAD dan doet &nbsp;·&nbsp; 3:05 tot 3:35

- als ontwikkelaar of team ga je via een **UI of een CLI** naar het platform
- je zegt: ik wil een project, daarin wil ik deze applicatie neerzetten, op dit domein

> **"Klik, klaar."**

- dat is de basisstap, en het is te simpel om het daarbij te laten

## 8 · En alles daaromheen &nbsp;·&nbsp; 3:35 tot 4:15

- ik wil ook een database. En storage. En een IDP, een Keycloak, zodat mensen kunnen inloggen, single sign-on
- ik wil een mailserver. Ik wil de **VLAM**, de interne overheids-LLM

> **"Ik wil heel veel dingen hebben, en ook dat is onderdeel van je deployment. Je applicatie heeft dingen nodig."**

> **"Klik, klik. Dan heb ik het, en dan werkt het."**

- **zodat ik als ontwikkelaar bezig kan zijn met applicatieontwikkeling, en geholpen word bij deployment**
- *pauze na "en dan werkt het"*

## 9 · Waar het vandaan komt &nbsp;·&nbsp; 4:15 tot 4:40

- gestart vanuit **ODI** zelf
- valt een beetje onder de vlag van **Wies** en onder **MOZa**
- Wies is een eigen ontwikkeld product dat ergens neergezet moest worden, en de algoritme-toolkit ook. **Dat is de aanleiding geweest: het moest ergens landen**

## 10 · Waar het draait &nbsp;·&nbsp; 4:40 tot 5:00

- we landen met z'n allen vooralsnog op **ODC-Noord**

> **"Maar het mooie is dat je op een ander cluster, een cluster to be, Fundament, ook gewoon klik-klik je applicatie neerzet."**

- **let op: zeg "to be".** Fundament is ontworpen, niet gebouwd. `cluster_config.py` kent vandaag alleen `local`, `sandboxed-local` en `odcn-production`
- de reden dat het kan: wat je opschrijft is geen Kubernetes, het is **een verklaring van wat je nodig hebt**

## 11 · Wie zit erin &nbsp;·&nbsp; 5:00 tot 5:20

> **"Wie zit erin? Ik. Product owner, architect, bouwer, tester, beheerder, deployer."**

- en de rollen die er nog niet zijn: ontwerper, technisch schrijver, security officer, servicemanager, support
- **achtenveertig projecten, honderdtweeëndertig omgevingen, één persoon**

> **"Dat is tegelijk het bewijs en het probleem. Het bewijs dat je dit gat met software kunt vullen in plaats van met mensen. En het probleem dat er precies één iemand is die weet hoe het werkt."**

- *pauze na "werkt". Dit is het moment waarop je slot echt wordt*
- zachtere variant als je de risicomelding niet wilt: **"Op dit moment ben ik het team"**, en dan direct door naar wat dat bewijst

## 12 · Slot &nbsp;·&nbsp; 5:20 tot 5:50

> **"ZAD is de control plane voor deployment. Het vult het gat tussen applicatieontwikkeling en platform."**

- terug bij je openingszin, cirkel rond

> **"En eigenlijk is ZAD vooral een opvatting over hoe dit zou moeten werken. Je zegt wat je nodig hebt in plaats van hoe het moet. Je krijgt wat je nodig hebt in plaats van wat je vraagt. En wat jij niet gebruikt, is er voor iemand anders."**

- *dit is de zin die mensen navertellen. Rustig, en pauzeer erna*
- **de filosofie hoort hier en niet vooraan.** Vooraan klinkt het vaag, hier heb je hem verdiend

> **"Mijn vraag aan jullie is: help me dit uitleggen."**

- past bij de opdracht om samen tot een elevator pitch te komen
- **niet zeggen: "bedankt voor het kijken".** Dat is een video-afsluiter, geen zaal-afsluiter

---

## Wat je nog niet zei, en wat je ermee kunt

Je ingesproken versie zit al op ongeveer 5:50. Deze vier zitten er dus **niet** in, en dat is een keuze, geen omissie. Neem er hooguit één mee, en schrap er dan iets voor.

| Wat | Waarom je het zou willen | Kost |
|---|---|---|
| **"Je weet vooraf niet wat je nodig hebt."** Je denkt database, wordt er geen. Je begint zonder inloggen, na twee maanden moet het toch. | Verklaart waarom "morgen een vinkje erbij" ertoe doet, en waarom vooraf kiezen niet werkt | 25 sec |
| **Een pull request is een omgeving.** Eigen URL, eigen database, en die database is niet leeg: je kiest van welke omgeving hij een kloon is | Het meest concrete dat je hebt voor ontwikkelaars, en het enige dat echt indruk maakt | 30 sec |
| **48 projecten, 132 omgevingen, 46 previews** | Bewijst dat het draait en niet een plan is | 10 sec |
| **"Wat jij niet gebruikt, is er voor iemand anders. Het licht hoeft niet altijd aan."** | De enige plek waar delen en zuinigheid binnenkomen | 20 sec |

**Mijn advies:** neem de tweede (een PR is een omgeving) en de derde (de aantallen). Samen 40 seconden, en ze doen samen het meeste werk: het draait echt, en het doet iets dat je zelf niet makkelijk bouwt. De rest bewaar je voor de flip-over.

## Dictatie-opschoning

Wat de spraakherkenning ervan maakte, en wat het moet zijn:

| Gehoord | Bedoeld |
|---|---|
| ZAT / ZAP | **ZAD** |
| ODU | **ODI** |
| WIS / Wyss | **Wies** |
| MOZA | **MOZa** |
| flam / FLAM | **VLAM** |
| increases | **ingresses** |
| "door een captie" | **"door een knopje"** |
| "de algoritme mensen, ToeKids" | de algoritme-toolkit (klopt dat?) |

## Voorbereiden voor de flip-over, niet voor nu

1. **"Kan mijn project hierop?"** Ja, en het begint met één bestand.
2. **"Wat als ik iets nodig heb dat er niet is?"** Dan bouwen we een dienst, of je krijgt je eigen instantie. Twee projecten doen dat.
3. **"Dit kan VPA toch ook?"** Alleen het meten, en zelfs dat half: 84 procent van onze containers zit onder de ondergrens van 250 Mi die VPA hanteert. VPA alleen zou ons ruim twee keer zo duur maken. En VPA provisioneert niets, kent geen delen, en weet niet dat niemand kijkt.
4. **"Draait het ook ergens anders?"** Vandaag ODC-Noord. Fundament is ontworpen, niet gebouwd.
5. **"Is het dan niet gewoon een applicatie?"** Een platform is een laag: onder mij zit Kubernetes, boven mij jouw applicatie. Dat ik zelf ook dingen nodig heb maakt me geen applicatie, dat maakt me een laag. En het stuurt niet alleen aan, het draait ook zelf een paar gedeelde diensten waar iedereen op deelt.
