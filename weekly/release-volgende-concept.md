#### ZAD release, nog te schrijven

Concept. Hernoem dit bestand naar `release-<datum>.md` zodra de releasedatum bekend is.

Dit is alles wat er sinds de release van 2 september bij is gekomen: 40 opgeleverde brokken werk. De vorige uitgebrachte notitie is `release-2026-09-02.md`, en dat is ook de versie die productie vandaag draait.

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

##### Een eigen domein aanvragen

Gerepareerd. Een eigen domein met het kale domein op een component was niet aan te vragen: de weigering had geen veld om in te landen, dus er kwam geen melding in beeld, en het vinkje om het domein aan te vragen zat achter die weigering. Een eigen domein, een subdomein daarop en een kaal domein mogen nu alle drie in het projectbestand staan voordat de goedkeuring rond is, en de aanvraag komt tot stand.

Verder op domeinen:

- Gereserveerde namen als `admin` en `www` gelden alleen op zones die ZAD zelf bedient, dus niet meer op een eigen domein.
- Het domeinenblok is niet meer genoemd naar een instelling die niet meer bestaat.

##### Services op een component

Gerepareerd. De keuzelijst met services op een component toonde bijna alles wat het project aanhad. Een service zegt nu zelf of hij per component aan te vinken is, en de lijst toont alleen die.

##### Uitnodigingen en accounts

- Een account dat via een uitnodiging wordt aangemaakt, krijgt de bevestigingsmail meteen bij het aanmaken in plaats van bij de eerste keer inloggen, en bevestigt zijn adres voordat inloggen werkt. Dit geldt niet voor SSO Rijk.
- Een uitnodiging slaat op welk component de bestemming is, niet de uitgerekende URL. Verandert die URL later, dan blijft de uitnodiging naar de goede plek wijzen.

##### Klonen, backups en herstellen

- Een run die afbreekt na een geslaagde kloon maakt die kloon bij de volgende run af, in plaats van een tweede database ernaast te zetten.
- Een gekloonde deployment erft de webadresvorm van zijn bron. Eerder kreeg hij de componenten wel mee maar die vorm niet, en viel hij terug op een andere standaard dan de bron.
- Het bronschema van een externe bron blijft staan in plaats van weggegooid te worden.
- Een losse PVC-restore krijgt het cluster waarop hij draait doorgegeven.
- Backup- en restorepods draaien op het serviceaccount van het project.

##### Sites achter een helmfile

Gerepareerd. Sites die via een helmfile uitrollen waren onbereikbaar: hun DNS-record wees over de zonegrens, en dat overleeft de DNSSEC-validatie niet. Bezoekers kregen een SERVFAIL. Dit raakte onder andere de documentatiesites.

##### Een project verwijderen

Gerepareerd, en dit had brede gevolgen. Een verwijdering kon achter een lopende uitrol vast komen te zitten die op gezondheid wachtte, terwijl die gezondheid nooit zou komen. Eén vastgelopen verwijdering hield daarmee de uitrol van alle projecten op het cluster tegen.

De noodgreep die daarvoor was ingebouwd maakte het erger: de applicatie verdween, maar haar resources bleven staan zonder dat er ooit een verwijderverzoek voor was geweest. Eén meting vond 350 achtergebleven resources, waaronder 140 secrets, opgebouwd over drie weken. De verwijderroute beëindigt de lopende uitrol nu eerst, en verwijdert de resources zelf voordat er geforceerd wordt. Er is gereedschap bij dat opspoort wat er nog ligt.

Daarnaast: een projectaanmaak die slaagt wordt niet meer als mislukt weggeschreven. De uitrol van het projectniveau kon na de workload komen die erop wacht, waardoor de aanmaaktaak in zijn tijdslimiet liep terwijl het project een halve minuut later draaide.

##### VLAM

- De doorlus wijst naar overheid-i, met de bundel die daarbij hoort. De service biedt die CA-bundel aan om te downloaden bij het serviceblok.
- Het model kies je uit een lijst in plaats van de naam over te typen.

##### Foutmeldingen

- Een fout noemt wat je eraan kunt doen in plaats van wat er technisch stukging.
- Een serverfout komt aan als pagina of als envelop, niet meer als kale JSON. Die envelopvorm staat nu ook in het OpenAPI-document.
- Een melding herhaalt de afgekeurde waarde niet meer. Wat iemand invult kan van elders geplakt zijn, en dan zet een foutmelding die waarde op het scherm van de persoon die hem invoerde. Dit gold onder andere voor de bestemming van een uitnodiging en voor een geweigerde instelling.
- Technische details lekken niet meer naar buiten. Op meerdere plekken kwam de onderliggende uitzondering mee in het antwoord.

##### Sneller

- Een deploy na een codewijziging verstuurt 8,6 MB in plaats van 92,2 MB. De image is zo ingedeeld dat een wijziging in de applicatiecode alleen de bovenste laag ongeldig maakt. Het image werd daarbij ook kleiner, van 877 naar 801 MB.
- Versleuteling loopt via een efficiëntere weg.
- Het bepalen van de gezondheid van een deployment is verbeterd.

##### Beveiliging en beschikbaarheid

- De platformsleutel en het GitHub-token zijn vervangen. Terugkerend onderhoud volgens BIO2 8.24: de oude sleutel opent niets meer en het oude token geeft nergens toegang. Voor gebruikers verandert er niets.
- MinIO komt uit een eigen build. De leverancier heeft zijn images achter een abonnement gezet en het project gearchiveerd, waardoor de opslag niet meer opstartte. Zowel de opslag als het bijbehorende gereedschap komt nu uit ons eigen register, dus een volgende stap van de leverancier raakt ons niet.
- Een projectpad kan de repository niet meer verlaten.

##### Uitgelogd raken na een deploy

Gerepareerd. Bij een applicatie achter de authorization wall raakte iedereen uitgelogd zodra er een deploy was geweest. Het cookie-secret van oauth2-proxy kreeg bij elke uitrol een nieuwe waarde, waardoor alle lopende sessies ongeldig werden. Dat secret houdt nu zijn waarde.

##### Verder opgelost

- Het inlogscherm toont de template uit het projectbestand, en de rolcontrole stuurt door in plaats van te blijven hangen.
- Een schemamigratie zet de stempel op de nieuwste versie, ook als er onderweg niets te doen was.

##### Feedback

Mocht er iets niet goed of lekker werken, of word je juist ergens heel blij van, laat het vooral even weten.

##### Notitie: issues die bij deze release dicht mogen

- **#153** SOPS-secret lifecycle na component-prune. Gemerged als RC-202.
- **#56** Generational failover creates zombie databases. Gemerged als RC-203. Let op bij het sluiten: bestaande `_vN`-databases zijn niet opgeruimd, dat is een losse actie.
- **#167** Een kloon erft de vorm van het webadres. Gemerged als RC-217.
- **#179** Een eigen domein aanvragen loopt vast op het kale domein. Gemerged als RC-216.
- **#184** App-delete kan eeuwig hangen. Gemerged als RC-226, issue al gesloten.
