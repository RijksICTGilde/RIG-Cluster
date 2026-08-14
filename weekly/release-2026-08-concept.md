#### ZAD release, augustus 2026 (concept)

> Concept. De datum en de definitieve bestandsnaam volgen zodra de release wordt uitgerold.

Dit is de grootste release tot nu toe. Waar de vorige over betrouwbaarheid en snelheid ging, gaat deze over **wat je zelf kunt**: vijf compleet nieuwe diensten, een database die meer aankan, en voor het eerst een volwaardige API waarmee je alles wat het portaal kan ook vanaf de commandoregel doet. Daarnaast is de wizard flink volwassener geworden, met uitleg bij elke keuze en een samenvatting die laat zien wat er straks echt gebeurt.

##### Vijf nieuwe diensten

- **Slaapstand voor preview-omgevingen.** PR- en preview-deployments die niemand bekijkt gaan na een deadline vanzelf in slaapstand en geven dan geen geheugen en CPU meer uit. Bezoekt iemand de URL, dan wordt de omgeving automatisch weer gewekt, met een "applicatie wordt gestart"-pagina zolang dat duurt. Voor projecten met veel openstaande PR's scheelt dat direct in het beslag op het cluster.
- **Cross-domain toegang.** Je legt nu zelf in je projectbestand vast welke andere projecten, deployments of componenten jouw pods mogen bereiken, en waar je zelf heen mag. Geen ticket meer nodig voor verkeer tussen projecten: je beschrijft het, en het wordt als netwerkbeleid uitgerold.
- **Uitnodigingen.** Een projectbeheerder nodigt mensen uit voor de eigen Keycloak-omgeving via een deelbare link, rechtstreeks vanuit het portaal. Daar is geen toegang tot de projectenrepository meer voor nodig.
- **Bijlagen.** Koppel een bestand aan een component, bijvoorbeeld een certificaat, keystore of CA-bundle. Het bestand wordt versleuteld in je project bewaard en bij het uitrollen als bestand in de pod gezet of als omgevingsvariabele meegegeven. Bijlagen kunnen ook weer weg, en als er nog iets naar verwijst krijg je dat eerst te zien in plaats van een gebroken verwijzing achteraf.
- **Health check.** Je bepaalt zelf hoe Kubernetes controleert of je component gezond is: het protocol, de poort en het pad. Applicaties die geen standaard HTTP-antwoord geven hoefden daar voorheen omheen te werken.

##### Je database kan meer

- **Meerdere schema's binnen dezelfde database.** Naast het standaardschema kun je er zelf bij definiëren, project-breed, elk met een eigen omgevingsvariabele. Handig als een rapportagetool of een tweede component zijn eigen ruimte moet hebben zonder een tweede database.
- **Gedeeld of een eigen cluster.** Je kiest nu waar je database staat: op de gedeelde instantie, of een eigen PostgreSQL-cluster voor je project.
- **Weghalen is niet meteen weggooien.** Een schema dat je uit de lijst haalt blijft met zijn data bestaan; het wordt alleen niet meer aangeboden. Zo kost een vergissing je geen gegevens.

##### Alles kan nu ook zonder het portaal

Dit is de tweede grote lijn van deze release. Wie liever scriptt dan klikt, of een eigen tooling bouwt, kan nu de hele weg via de API:

- **Een project aanmaken en je projecten opvragen met je rijksaccount.** Geen aparte sleutel nodig om te beginnen: je logt in met SSO en krijgt terug welke projecten je hebt, met hun omschrijving en sleutel.
- **Diensten opvragen en laten uitleggen.** De API vertelt zelf welke diensten er zijn, wat ze doen, op welk niveau je ze toepast en welke omgevingsvariabelen ze opleveren. Iemand die het portaal nooit heeft gezien, of een script, kan daarmee zelfstandig uitvinden hoe het systeem werkt.
- **Opslaan zonder verwerken.** In de opbouwfase wil je tien dingen achter elkaar toevoegen en daarna één keer uitrollen, niet tien keer. Dat kan nu: elk endpoint dat normaal verwerkt, accepteert dat je het even laat wachten.
- **Configuratie per niveau.** Instellingen van een dienst zijn via de API te zetten op project-, deployment- of componentniveau, precies zoals in het portaal.

##### De wizard is volwassener geworden

- **Uitleg bij elke dienst.** Bij elke keuze staat nu een toelichting: wat het is, wanneer je het gebruikt en hoe je het invult. Diezelfde uitleg is ook via de API op te vragen, dus er is één verhaal en niet twee.
- **Een samenvatting die klopt.** Voor je indient zie je wat er werkelijk wordt vastgelegd, in plaats van een benadering daarvan.
- **Fouten komen aan het begin, niet aan het eind.** Een project dat niet aan de eisen voldoet wordt nu vroeg tegengehouden, met een melding die zegt wat er mis is, in plaats van halverwege het uitrollen om te vallen.
- **Eén manier van bevestigen.** Alle ingrijpende acties, ook verwijderen, vragen op dezelfde manier om bevestiging en vertellen wat de gevolgen zijn.
- **Zien wat een taak doet.** Een lopende taak toont zijn stappen en voortgang, dus je hoeft niet te raden of er nog iets gebeurt.
- **Zelf je startcommando meegeven.** Een component kan een eigen startcommando krijgen, als één regel, inclusief argumenten tussen aanhalingstekens.

##### Een nieuw portaal

Dit stond vorige keer nog bij "voor de toekomst" en is er nu: het portaal is opnieuw opgebouwd op de rijkshuisstijl-componenten. Dat is niet alleen een ander uiterlijk. De projectpagina was één lange pagina waarop je moest zoeken; die is nu opgedeeld in tabbladen die elk over één ding gaan.

- **Overzicht, Team, Componenten, Services, Services info, Deployments, Metrics, Backups en Taken.** Elk tabblad heeft zijn eigen adres, dus je kunt er rechtstreeks naartoe en een link naar iemand anders sturen.
- **Je omgevingsvariabelen staan bij het component waar ze bij horen.** Voorheen stond er een verwijzing naar een lijst op een ander tabblad. Bij een deployment zie je alleen nog de overschrijvingen, en dus niet meer dezelfde variabele twee keer.
- **Je aliassen zijn eindelijk terug te lezen.** Je kon ze wel invullen, maar nergens zien wat erin stond. Ze staan nu in de kaart van het component, met de verwijzing erbij, zodat te zien is waar een waarde vandaan komt.
- **Wat een dienst je aanreikt staat bij elkaar.** Het Keycloak-adres, de beheerdersnaam, het wachtwoord en de gedeelde code, de uitnodigingslinks en je bijlagen staan op het tabblad Services info, in dezelfde vorm als de rest van je configuratie. Wachtwoorden en codes zijn afgeschermd, met een knop om ze te tonen en te kopiëren.
- **Acties staan waar je ze zoekt.** Elk tabblad heeft een eigen Acties-kaart met de knoppen die bij dat onderwerp horen, in plaats van verspreid over de pagina.
- **Het dashboard toont per project wat het gebruikt**, geheugen boven CPU en gesorteerd op geheugen, met de projectnaam als link naar dat project. Zo zie je in een oogopslag wie tegen zijn limiet aanloopt, ook als dat in absolute zin een klein project is.
- **Een bijlage is te vervangen zonder je koppelingen kwijt te raken.** Een verlopen certificaat verving je door hem weg te gooien en opnieuw te uploaden, en dan verwees geen enkel component er meer naar. Nu overschrijf je de inhoud en blijft alles gekoppeld.
- **Een slapend project is gezond.** Het wordt niet meer als probleem geteld, maar wel apart benoemd, zodat je niet denkt dat er iets stuk is.
- **Bij een taak staat wie hem uitvoerde.** De ingelogde gebruiker, of "API" als het via een sleutel ging.

##### Veiliger

- **De gedeelde registry heeft een eigenaar per tag.** Een image die je publiceert draagt de naam van je project, zodat een ander project er niet overheen kan schrijven. Dit kwam uit een technische toetsing tegen de BIO en NORA en is de zwaarste bevinding daaruit.
- **De uitleg bij een dienst is een pagina op zich.** Open je de link rechtstreeks, dan krijg je een leesbare pagina in plaats van tekst zonder opmaak.
- **Wachtwoorden staan niet meer in de procestabel.** Bij het bijwerken van een geheim ging de waarde via de commandoregel naar Kubernetes, en die is voor iedereen op die machine leesbaar. Dat gaat nu via een bestand dat alleen het proces zelf mag lezen.

##### Sneller uitrollen

- **Een uitrol wacht niet meer op de klok.** Er zaten vaste wachttijden in het uitrolpad uit de tijd dat ArgoCD zijn status traag bijwerkte. Die zijn weg: de laatste stap van een projectaanmaak duurde dertien seconden en is nu minder dan een tiende seconde. Het portaal kijkt bovendien elke twee seconden of een stap klaar is in plaats van elke vijf of tien, dus je ziet eerder dat er iets gebeurd is.
- **Geen excuus meer dat nergens op slaat.** Bij het aanmaken van een project stond een melding dat het door een bug in ArgoCD een paar minuten kon duren. Die bug is verholpen en de melding is weg. Wat blijft staan is de zin die er wel toe doet: duurt het lang, dan betekent een time-out niet dat het aanmaken is mislukt.

##### Terugzetten van een backup

- **Een restore laat je project werkend achter.** Terugzetten in je eigen database wisselde het databasewachtwoord, terwijl het opgeslagen wachtwoord in je omgeving bleef staan. Het gevolg was een project dat het niet meer deed en waarop ook geen enkele wijziging meer lukte. Dat wachtwoord wordt nu niet meer gewisseld; het is er niet voor nodig.
- **En het antwoord vertelt de waarheid.** Een restore die zelf slaagde maar waarvan het bijwerken daarna misging, meldde toch "gelukt". Nu krijg je in dat geval "gedeeltelijk", met erbij of dat bijwerken wel of niet is voltooid.

##### Duidelijker status, minder ruis

- **Uitgeschakeld is niet ongezond.** Een deployment die je bewust hebt uitgezet wordt niet langer als een probleem gerapporteerd.
- **Eén toestand, één badge.** De status die je in het portaal ziet komt uit één bron, zodat verschillende plekken niet meer verschillende dingen beweren.
- **Fouten uit ArgoCD komen door.** Gaat het renderen van je manifesten mis, dan zie je die melding nu zelf, in plaats van een algemeen "mislukt".
- **Een falende health-controle heet geen crash.** Een component dat draait maar de controle niet doorstaat werd gemeld als een applicatie die omvalt, en dan zoek je op de verkeerde plek. Nu staat er wat er werkelijk aan de hand is.
- **Een herverwerking noemt alleen webadressen die bestaan.** Je kreeg een URL terug die vervolgens 404 gaf.
- **Automatisch afgestemde resources.** Geheugen en CPU worden op basis van werkelijk gebruik bijgesteld, zodat je niet handmatig hoeft te schatten. Wil je dat niet, dan zet je het per project uit.

##### Onder de motorkap

Niet zichtbaar, wel merkbaar: elke dienst zit nu volledig in zijn eigen map, met zijn configuratie, formulieren, uitleg en validatie bij elkaar. Daardoor kan er sneller een dienst bij, en raken bestaande diensten minder snel van slag door werk aan een andere. Verder is er een schemaversie per dienst, zodat oude projectbestanden vanzelf meegaan naar de nieuwe vorm zonder dat je iets hoeft te doen.

##### Voor de toekomst

De eerstvolgende stap is verdere uitbreiding van de commandoregel-tooling nu de API-kant er ligt. Daarnaast blijft staan: CI/CD voor ZAD zelf, en de vraag hoe ZAD op langere termijn meer kan leunen op bestaande producten en minder op eigen maatwerk.
