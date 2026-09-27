#### ZAD release, nog te schrijven

> **Concept, nog niet uit te brengen.** Hernoem dit bestand naar `release-<datum>.md` zodra de datum bekend is, en haal dit blok en de notitie onderaan weg. De inhoud dekt alles sinds de release van 2 september, want dat is de versie die productie vandaag draait.

##### Eigen container registries

Nieuw. Een project kan images draaien uit een eigen private registry. Op projectniveau vul je de registry, een gebruikersnaam en een token in; per component kies je welke registry bij die image hoort. Wat er onder water gebeurt verschilt per cluster, maar het projectbestand is overal hetzelfde.

Wat daarbij hoort:

- De registrykeuze staat in de componentvorm, op de plek waar je de image invult.
- De upstream volgt uit de images die het project al heeft, dus je zoekt hem niet zelf op. Een geplakte URL en een vrij label worden omgezet naar de juiste schrijfwijze.
- Het token wordt getoetst voordat het wordt opgeslagen, en staat als wachtwoordveld in het formulier. Een onleesbaar token wordt niet gewist.
- De ingestelde registries staan als blok op de projectpagina. Zonder keuze geldt de eigen registry alsnog; die optie heet "Automatisch".
- Elk project krijgt een eigen serviceaccount, dus het ene project kan niet bij de registry van het andere.
- Een image wordt alleen uitgeschakeld als de registry bevestigt dat hij er niet is, niet meer op een onduidelijk antwoord.

##### Speelruimte per service

Nieuw. Een service legt per instelbaar veld vast wat de grenzen zijn: de ondergrens, de bovengrens en op welke laag het veld gezet mag worden. Een waarde daarbuiten wordt geweigerd met een melding die de grenzen noemt, bijvoorbeeld `'connection-limit' moet tussen 1 en 500 liggen; je gaf 501`.

Het eerste veld dat hierop draait is de connectielimiet van de databaserollen, en die is daarmee instelbaar geworden.

##### Een eigen domein gebruiken

Een eigen domein met het 'kale domein' (mijndomein.nl) op een component was niet aan te vragen: de weigering had geen veld om in te landen. Dit is opgelost.

Verder passen we de 'black-list' voor subdomeinen als `admin` en `www` alleen toe op zones die ZAD zelf bedient, dus niet meer op een 'eigen' domein.

##### Services op een component

De keuzelijst met services op een component toont nu alleen services die op componentniveau gelden.

##### Uitnodigingen en accounts

- Een account dat via een uitnodiging wordt aangemaakt, krijgt de bevestigingsmail meteen bij het aanmaken in plaats van bij de eerste keer inloggen, en bevestigt zijn adres voordat inloggen werkt. Dit geldt niet voor SSO Rijk.
- Een uitnodiging verwijst nu naar het component/depoyment dat de bestemming is, niet de uitgerekende URL. Verandert een URL later, dan blijft de uitnodiging naar de goede plek wijzen.

##### Klonen, backups en herstellen

- Een run die afbreekt na een geslaagde kloon maakt die kloon bij de volgende run af, in plaats van een tweede database ernaast te zetten.
- Een gekloonde deployment erft de webadresvorm van zijn bron. Eerder kreeg hij de componenten wel mee maar die vorm niet, en viel hij terug op een andere standaard dan de bron.
- Het bronschema van een externe bron blijft staan in plaats van weggegooid te worden.
- Een losse PVC-restore krijgt het cluster waarop hij draait doorgegeven.
- Backup- en restorepods draaien op het serviceaccount van het project.

##### Een project verwijderen

Een verwijdering kon achter een lopende uitrol vast komen te zitten en een deadlock veroorzaken. Eén vastgelopen verwijdering hield daarmee de uitrol van alle projecten op het cluster tegen.

##### VLAM

- De proxy wijst nu naar vlam.overheid-i.nl, met het certificaat dat daarbij hoort. De service biedt die CA-bundel aan om te downloaden bij het serviceblok.

##### Sneller

- Versleuteling loopt via een efficiëntere weg.
- Het bepalen van de gezondheid van een deployment is verbeterd.

##### Beveiliging en beschikbaarheid

- MinIO komt uit een eigen build. De leverancier heeft zijn images achter een abonnement gezet en het project gearchiveerd, waardoor de opslag niet meer opstartte. Zowel de opslag als het bijbehorende gereedschap komt nu uit ons eigen register, dus een volgende stap van de leverancier raakt ons niet.

##### Authorization wall

Gerepareerd. Bij een applicatie achter de authorization wall raakte iedereen uitgelogd zodra er een deploy was geweest. Het cookie-secret van oauth2-proxy kreeg bij elke uitrol een nieuwe waarde, waardoor alle lopende sessies ongeldig werden. Dat secret houdt nu zijn waarde.

##### Verder opgelost

- Het inlogscherm van keycloak volgt nu het template uit het projectbestand

##### Feedback

Mocht er iets niet goed of lekker werken, of word je juist ergens heel blij van, laat het vooral even weten.

##### Notitie: issues die bij deze release dicht mogen

- **#153** SOPS-secret lifecycle na component-prune.
- **#56** Generational failover creates zombie databases. Let op bij het sluiten: bestaande `_vN`-databases zijn niet opgeruimd, dat is een losse actie.
- **#167** Een kloon erft de vorm van het webadres.
- **#179** Een eigen domein aanvragen loopt vast op het kale domein.
- **#184** App-delete kan eeuwig hangen. Al gesloten.
