#### ZAD release, oktober 2026

Deze release gaat vooral over betrouwbaarheid. Uitrollen, verwijderen en wekken uit de slaapstand lopen niet meer vast op momenten waarop je zelf niets kunt doen. Een image die even niet op te halen is zet je component niet meer uit. De authorization wall faalt netjes en in het Nederlands. Daarnaast draait Keycloak voortaan uit een eigen image, is de mailrelay robuuster geworden, en zijn achter de schermen alle wachtwoorden van het platform in productie geroteerd.

##### Een image die niet opgehaald kan worden zet je component niet meer uit

**Dit verandert wat je op je deploymentkaart en in de ArgoCD-status ziet.**

Tot nu toe zette ZAD een component op nul replicas zodra het leek alsof zijn container image niet op te halen was. De deployment stond dan op `Healthy`, want nul replicas is voor ArgoCD een gezonde deployment.

Dat doen we niet meer. Een component dat zijn image niet kan ophalen blijft staan en Kubernetes blijft het proberen. Dat zie je op twee plekken:

- **Je deployment komt op `Degraded` te staan** in plaats van op `Healthy`. Dat is eerlijker, want er draait niets. Heb je een eigen alarm op de ArgoCD-status, dan gaat dat nu wel af.
- **Je deploymentkaart toont het component** met de image die niet lukt, wat de registry antwoordde en sinds wanneer. Er staat ook bij of het aan jouw image ligt (tag bestaat niet, geen toegang) of aan het platform (de gedeelde image-cache zit vol, de registry gaf geen antwoord).

Zodra de image er wel is, wordt hij vanzelf opgehaald. Je hoeft niets opnieuw uit te rollen.

**Waarom.** Op 30 september las ZAD een vol opslagquotum van de registry als "deze image bestaat niet" en zette daarmee ongeveer 40 componenten in zes projecten uit, terwijl elke image gewoon bestond. Een uitgeschakeld component kwam alleen terug met een nieuwe deploy, dus de storing duurde langer dan de oorzaak.

Uitschakelen bij een container die zijn geheugen opmaakt (OOMKilled) of die blijft crashen, blijft wel bestaan.

##### Authorization wall

**Een verlopen sessie sloopt je pagina niet meer stil.** Tot nu toe kreeg elk verzoek zonder sessie de inlogpagina terug, ook een stylesheet, een script of een `fetch()`. De pagina bleef dan zonder opmaak achter, met fouten in de console die niets over de oorzaak zeggen. Verzoeken onder `/api/` en verzoeken naar een statisch bestand krijgen nu een kale 401. Je applicatie kan zelf op `/oauth2/auth` toetsen of de sessie weg is (401 zonder sessie, 202 met). Staat je eigen XHR niet onder `/api/`, dan krijgt die nog wel de HTML-pagina. Laat het weten als dat je raakt.

**De sessie wordt vernieuwd voordat hij verloopt** in plaats van erna, en het cookie is niet langer geldig dan de Keycloak-sessie waarop het leunt.

**De foutpagina's van de wall zijn Nederlands** en zeggen wat er aan de hand is, met een code die je aan de beheerder kunt doorgeven. De meest voorkomende is een inlog die halverwege afbreekt omdat het e-mailadres in die omgeving nog niet bevestigd is. Maak je zelf vooraf een account aan voor iemand die via SSO Rijk binnenkomt, zet dan "Email verified" op aan.

**Wat nog niet is opgelost.** In sommige gevallen laadt een pagina nog steeds zonder opmaak, omdat voor een deel van de verzoeken geen sessiecookie meekomt. Waarom dat gebeurt weten we nog niet. Het faalt nu wel leesbaar. Loop je hier tegenaan, stuur ons een voorbeeld uit je eigen browser.

##### Uitrollen die vastlopen

- **Eén refresh trekt een vastgelopen uitrol los.** Herverwerken in het portaal, via de API of met `zadctl deployment refresh` breekt nu een lopende ArgoCD-sync af die blijft hangen. Je ziet dat als stap "Lopende sync afgebroken" in de voortgang.
- **Een sync die niet afrondt blokkeert de volgende niet meer.** Wachtte een sync op een onderdeel dat nooit gezond werd, dan kon je je kapotte configuratie niet meer repareren, want elke nieuwe sync stond erachter. Een sync breekt nu na tien minuten af en begint opnieuw op de nieuwste versie. De deploymentkaart en de API tonen een lopende of vastgelopen sync.
- **Minder mislukte taken door een korte hapering van ArgoCD.** ArgoCD is in productie af en toe een paar seconden onbereikbaar. Een taak die daar precies op viel, mislukte. ZAD probeert het nu tot 30 seconden opnieuw.
- **Een database op de standaardimage blijft niet meer eeuwig OutOfSync.** Bij die projecten liep een refresh na vier minuten vast. Na de uitrol is een refresh van zo'n project genoeg.

##### Verwijderen en opnieuw aanmaken

- **Een verwijderde deployment komt niet meer terug.** Liep er tijdens het verwijderen nog een verwerking van hetzelfde project, dan kon die de deployment terugzetten, en bleef hij gewoon draaien. Een deployment met een openstaande verwijdering wordt nu overgeslagen.
- **Verwijderen eindigt groen.** In ODCN eindigde een verwijdering vaak als "gedeeltelijk mislukt" en bleef er een map met manifests achter. ZAD wacht nu tot alles echt weg is en ruimt die map mee op.
- **Een trage verwijdering wordt niet meer afgebroken.** ZAD wachtte een vast aantal seconden en forceerde daarna, met een rode taak als gevolg. Nu wacht ZAD zolang er voortgang is. Opeenvolgende taken voor dezelfde deployment lopen netjes na elkaar.

##### Wekken uit de slaapstand

Wekken wacht nu tot je applicatie echt klaar is en rondt zichzelf daarna af. Eerder wachtte de eerste stap altijd vijf minuten op een status die nooit kwam, en meldde dan een onterechte fout. Komt je applicatie niet binnen tien minuten terug, dan faalt de taak met een duidelijke melding.

##### Automatisch bijstellen van geheugen

De automatische bijsteller verhoogde bij een tweede geheugenfout het al verhoogde getal opnieuw, waardoor een limiet in korte tijd veel te hoog werd. Dat gebeurt niet meer. Hij rolt ook niet meer elke nacht uit voor een verschil van een paar megabyte.

##### Uitnodigingen

De uitnodigingspagina is geschreven voor wie de uitnodiging krijgt: "Je bent uitgenodigd voor ...", met de keuze in gewone woorden ("Heb je een werkaccount?" of "Geen werkaccount?"). Is er maar één manier om in te loggen, dan valt de keuze weg. Een uitnodiging kan een eigen `title` krijgen, zodat de kop de naam van het product toont in plaats van de projectnaam. Zet dat veld pas in je projectbestand nadat deze release is uitgerold.

##### Uitloggen in je eigen applicatie

In de Keycloak-configuratie staat een nieuw optioneel veld `logout-path`, onder "Uitloggen". Daar vul je het pad in waarop je applicatie haar sessie beëindigt. Let op: binnen de federatie met SSO Rijk roept Keycloak dat pad bij uitloggen elders niet aan. Je applicatie moet zelf haar sessie wissen voordat ze doorstuurt naar het uitloggen. Hoe je dat doet staat in `instructions/uitloggen-in-een-applicatie.md`.

##### Keycloak start uit onze eigen image

Je merkt hier bij het inloggen niets van, en dat is het punt. Keycloak haalde bij elke start het inlogthema en onze SAML-mapper van github.com. Op 13 oktober liep die download bij een herstart in een timeout, en zag iedereen het standaard inlogscherm van Keycloak, op alle realms. Keycloak draait nu een image die wij zelf bouwen, met die uitbreidingen erin. Het inloggen hangt daardoor niet meer af van github.com, en Keycloak is na een herstart sneller terug.

##### Mailrelay

Op 13 oktober lag de mailrelay van het platform twee uur plat: hij had de gezondheidscontroles van Kubernetes zelf verbannen. Interne adressen staan nu op de lijst van toegestane adressen, en de relay komt in één keer goed op. Is de relay onbereikbaar, dan meldt ZAD dat als platformfout en wijst niet langer naar jouw project, en probeert het kort daarna opnieuw.

##### Verder

- De image-registries-service faalde in ODCN bij het aanmaken van een nieuwe registry. Opgelost.
- De uitleg bij MinIO zegt nu dat er één bucket en één set sleutels per deployment is, gedeeld door alle componenten.
- Het portaal draait op een nieuwere versie van de componentenbibliotheek (NLDD 0.8.92). Formulieren tonen weer onze eigen Nederlandse foutmeldingen in plaats van die van de browser, en een mislukte taak heeft weer een rode voortgangsbalk met percentage.

##### Achter de schermen

- Alle wachtwoorden van het platform in productie zijn geroteerd: databases, Redis, MinIO, mailrelay, Keycloak-mail, Prometheus en pgAdmin. Het rotatiescript controleert nu per onderdeel dat de oude waarde niet meer werkt.
- Een aantal Python-pakketten is bijgewerkt na een beveiligingsscan.
- Het beheerscript dat verweesde ArgoCD-applicaties opspoort, werkt nu ook in de ODCN-tenant.
- Het oude `local`-clustertype is verwijderd; lokaal ontwikkelen gaat via de sandbox.
- Voor deze release is een volledige controle gedraaid: unittests, de browsertests en een proefuitrol op de sandbox.
