#### ZAD release, nog te schrijven

Concept. Hernoem dit bestand naar `release-<datum>.md` zodra de releasedatum bekend is.

Dit is alles wat er sinds de release van 2 september bij is gekomen: 40 opgeleverde brokken werk. De vorige uitgebrachte notitie is `release-2026-09-02.md`, en dat is ook de versie die productie vandaag draait.

##### Eigen container registries

- **Je kunt images draaien uit je eigen private registry.** Je vult eenmalig op projectniveau de registry, een gebruikersnaam en een token in, en per component welke registry bij die image hoort. Wat daaronder gebeurt verschilt per cluster, en dat verschil merk je niet: je projectbestand is overal hetzelfde.
- **De registrykeuze staat bij de image**, op de plek in de componentvorm waar je de image invult.
- **De upstream volgt uit de images die je project al heeft**, dus je hoeft hem niet zelf op te zoeken. Een geplakte URL en een vrij label worden onder water rechtgetrokken.
- **Het token wordt getoetst voordat het wordt opgeslagen**, en het staat als wachtwoordveld in het formulier. Een onleesbaar token wordt niet stilletjes gewist.
- **De registries staan als blok op je projectpagina**, zodat je ziet wat er is ingesteld. Kies je niets, dan geldt je eigen registry alsnog, en de optie heet nu ook "Automatisch".
- **Elk project krijgt een eigen serviceaccount**, zodat het ene project niet bij de registry van het andere kan.
- **Een image wordt niet meer uitgeschakeld op een onduidelijk antwoord.** Dat gebeurt alleen nog als de registry bevestigt dat de image er niet is.

##### Je eigen domein

- **Een eigen domein op het kale domein aanvragen werkt.** Dit liep muurvast: de weigering had geen veld om in te landen, dus het scherm bleef staan en de knop leek kapot, en het vinkje om het domein aan te vragen zat achter die weigering. Er was geen uitgang. Nu mag je opslaan wat je WIL, en de aanvraag komt tot stand.
- **Gereserveerde namen gelden alleen op zones die ZAD zelf bedient.** Namen als `admin` en `www` worden op je eigen domein niet meer geweigerd.
- **Het domeinenblok heet niet meer naar een instelling die niet meer bestaat.**

##### Diensten instellen

- **Een dienst zegt zelf hoe ver je mag gaan.** Per instelbaar veld staat de ondergrens, de bovengrens en op welke laag je het mag zetten. Een waarde daarbuiten wordt geweigerd met een melding die zegt wat wél mag, bijvoorbeeld "moet tussen 1 en 500 liggen; je gaf 501".
- **De connectielimiet van je databaserollen is instelbaar.**
- **De keuzelijst met diensten op een component toont alleen wat daar ook echt per component te kiezen valt**, in plaats van bijna alles wat het project aanheeft.
- **De uitrolwacht leest wat er werkelijk is gereconcilieerd** per deployment, in plaats van af te gaan op een teller die daar niet over ging.

##### Uitnodigingen en accounts

- **Wie zichzelf via een uitnodiging toevoegt, bevestigt eerst zijn e-mailadres.** De bevestigingsmail komt meteen bij het aanmaken van het account, niet pas bij de eerste keer inloggen. Wie via SSO Rijk binnenkomt merkt er niets van.
- **De bestemming van een uitnodiging beweegt mee met je project.** Er wordt opgeslagen welk component de bestemming is in plaats van de uitgerekende URL, dus een uitnodiging blijft naar de goede plek wijzen als die URL later verandert.
- **Een afgekeurde bestemming komt niet meer terug in de foutmelding.**

##### Klonen, backups en herstellen

- **Een afgebroken kloon wordt afgemaakt in plaats van opnieuw begonnen.** Er komt geen tweede database naast de eerste te staan.
- **Een kloon erft het webadres van zijn bron.** Een gekloonde deployment kreeg de componenten van de bron mee maar niet diens webadresvorm, en viel dan terug op een andere standaard dan de bron had.
- **Het bronschema van een externe bron blijft staan** in plaats van weggegooid te worden.
- **Een losse PVC-restore weet op welk cluster hij draait.**
- **Backup- en restorepods draaien op het serviceaccount van je project.**

##### Sites die onbereikbaar waren

- **Sites die via een helmfile uitrollen zijn weer bereikbaar.** Hun DNS-record wees over de zonegrens, en dat overleeft de DNSSEC-validatie niet: bezoekers kregen een SERVFAIL en de site bestond voor hen simpelweg niet. Dat raakte onder andere de documentatiesites.

##### Verwijderen dat bleef hangen

- **Een project verwijderen blijft niet meer eeuwig hangen.** Een verwijdering kon achter een uitrol vast komen te zitten die op gezondheid wachtte, en die wachtte op iets wat nooit gezond zou worden. Eén vastgelopen verwijdering hield daarmee de uitrol van álle projecten op het cluster tegen.
- **En het laat geen rommel meer achter.** De noodgreep die dat moest oplossen maakte het erger: de applicatie verdween, maar haar resources bleven staan zonder dat er ooit een verwijderverzoek voor was geweest. Eén meting vond zo 350 achtergebleven resources, waaronder 140 secrets, opgebouwd over drie weken. Er is nu ook gereedschap dat opspoort wat er nog ligt.
- **Een projectaanmaak die slaagt wordt niet meer als mislukt weggeschreven.** De uitrol van het projectniveau kon na de workload komen die er op wacht, en dan liep de aanmaaktaak in zijn tijdslimiet terwijl het project een halve minuut later gewoon draaide.

##### VLAM

- **De doorlus wijst naar overheid-i**, met de bundel die daarbij hoort, en de dienst biedt die CA-bundel aan om te downloaden bij het dienstblok.
- **Het model kies je uit een lijst** in plaats van een voorgevulde naam te moeten overtypen.

##### Foutmeldingen

- **Een fout vertelt wat je eraan kunt doen**, in plaats van wat er technisch stukging.
- **Een serverfout komt aan als pagina of als nette envelop**, niet meer als kale JSON, en die vorm staat nu ook in het OpenAPI-document beschreven.
- **Technische details lekken niet meer in foutmeldingen.** Op meerdere plekken kwam de onderliggende uitzondering mee naar buiten, en een geweigerde waarde werd in de melding herhaald.

##### Sneller

- **Een deploy na een codewijziging verstuurt 8,6 MB in plaats van 92,2 MB.** De image is zo ingedeeld dat een wijziging in de applicatiecode alleen de bovenste laag ongeldig maakt. Het hele image werd daarbij ook kleiner, van 877 naar 801 MB.
- **Een secret dat niet wijzigt blijft staan.** Versleutelen levert elke keer andere bytes op, dus werd bij elke uitrol elk geheim opnieuw weggeschreven. Dat is voorbij, en een deploy logt daardoor ook niemand meer uit.
- **Geheimen ontsleutelen gebeurt in het proces zelf** in plaats van met een apart programma per veld. Dat is minder werk per verzoek, en de sleutel staat onderweg niet meer in een tijdelijk bestand op schijf.

##### Beveiliging en beschikbaarheid

- **De platformsleutel en het GitHub-token zijn vervangen.** Terugkerend onderhoud volgens BIO2 8.24: de oude sleutel opent niets meer en het oude token geeft nergens meer toegang. Hier is niets van te merken.
- **MinIO komt uit een eigen build.** De leverancier heeft zijn images achter een abonnement gezet en het project gearchiveerd, waardoor de opslagdienst niet meer opstartte. Zowel de opslag als het bijbehorende gereedschap komt nu uit ons eigen register, dus een volgende stap van de leverancier raakt ons niet.
- **Een projectpad kan de repository niet meer verlaten.**

##### Verder opgelost

- **Een leeg projectbestand werd als `null` teruggeschreven** in plaats van leeg te blijven.
- **Het inlogscherm toont de template uit je projectbestand**, en de rolcontrole stuurt door in plaats van te blijven hangen.
- **Een schemamigratie zet de stempel op de nieuwste versie**, ook als er onderweg niets te doen was.

##### Feedback

Mocht er iets niet goed of lekker werken, of word je juist ergens heel blij van, laat het vooral even weten.

##### Notitie: issues die bij deze release dicht mogen

- **#153** SOPS-secret lifecycle na component-prune. Gemerged als RC-202.
- **#56** Generational failover creates zombie databases. Gemerged als RC-203. Let op bij het sluiten: bestaande `_vN`-databases zijn niet opgeruimd, dat is een losse actie.
- **#167** Een kloon erft de vorm van het webadres. Gemerged als RC-217.
- **#179** Een eigen domein aanvragen loopt vast op het kale domein. Gemerged als RC-216.
- **#184** App-delete kan eeuwig hangen. Gemerged als RC-226, issue al gesloten.
