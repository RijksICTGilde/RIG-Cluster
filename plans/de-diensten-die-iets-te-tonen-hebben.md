# De diensten die iets te tonen hebben, krijgen hun eigen plek

Status: plan, 13 augustus 2026. Sommige diensten leveren een eigen infopaneel op de projectpagina: Keycloak toont zijn realm, admin-console, gebruikersnaam, wachtwoord en de gedeelde OTP. Die panelen vallen nu weg tussen de rest van de detailpagina, terwijl je er juist naartoe gaat als je iets nodig hebt.

## Wat er is, gemeten

Drie diensten leveren een eigen detailsectie: **attachments**, **invite** en **keycloak** (`section-detail.html.j2` in hun eigen map, verzameld via `detail_page_sections`). De rest levert niets op projectniveau.

Het tabblad **Services** toont nu de dienstkaarten: welke diensten aan staan, met hun beschrijving en bindingslabel. Dat is *beheer*: aanzetten, configureren, weghalen.

De drie panelen hierboven zijn iets anders. Dat is wat je nodig hebt om de dienst te **gebruiken**: een adres om naartoe te gaan, een wachtwoord om mee in te loggen, een code om in te vullen.

## De vraag die de taak moet beantwoorden

**Hoe heten die twee tabbladen?** De gebruiker heeft "Services beheren" en "Services gebruik" geopperd en zegt er zelf bij dat de naam niet goed is. Wij vinden dat ook: "gebruik" leest als verbruik of kosten, en dat is het niet.

Bedenk een naam die zegt wat er staat. Denk aan de richting: het ene tabblad gaat over *wat er aan staat*, het andere over *hoe je erbij komt*. Woorden die in die richting liggen: toegang, aansluiten, gegevens, sleutels, verbinden. Kies er een die klopt voor alle drie de diensten en niet alleen voor Keycloak, want attachments en invite zitten er ook in.

**Meet eerst wat die drie panelen werkelijk tonen** voordat je een naam kiest. Een naam die alleen op Keycloak past, gaat scheef zodra de volgende dienst zich aanmeldt.

Kom je er niet uit, lever dan twee of drie kandidaten met de afweging erbij in plaats van er een te kiezen; dit is een naam die blijft staan.

## Wat er verder moet gebeuren

* Een tweede tabblad naast Services, met dezelfde structuur als de andere (eigen adres `/projects/<naam>/<tab>`, beide vormen letterlijk geregistreerd).
* De drie panelen verhuizen daarheen en **verdwijnen van de detailpagina**; niet kopiëren.
* Blijft het tabblad leeg voor een project dat geen van de drie diensten heeft, laat het dan weg uit de tabbalk in plaats van een lege pagina te tonen. Dat is de vraag die je bij een generiek mechanisme hoort te stellen: hoort dit tabblad er ook te zijn als er niets in staat?
* Volgt de plek uit de dienst zelf, of noemt het sjabloon ze met naam? Dezelfde afweging als bij RC-100 (backups). Kies bewust en zeg waarom; drie diensten is genoeg om het generiek te willen doen.

## De toets

- er staat een gekozen naam met de reden, of twee tot drie kandidaten met de afweging;
- de drie panelen staan op het nieuwe tabblad en niet meer op de detailpagina;
- een project zonder een van die drie diensten krijgt geen leeg tabblad;
- het tabblad heeft een eigen adres, en de oude weg doet wat er besloten is.

## Waar op te letten

**Kijk naar het scherm.** `scripts/kijk_sandbox.py /projects/<naam>/<tab>`; een tabblad met drie panelen eronder is iets anders dan drie panelen die ergens tussen staan, en dat zie je alleen op beeld.

**Niet de panelen zelf verbouwen.** Dit gaat over waar ze staan.
