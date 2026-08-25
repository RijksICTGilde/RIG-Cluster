# Meet of een eigen emailSender-SPI het wint van de realmconfiguratie op Keycloak 25.0.6

Dit is een METING, geen bouwtaak. Er landt geen functionaliteit op productie en er gaat niets aan. Wat er landt is een antwoord met bewijs, en dat antwoord bepaalt de architectuur van de bevestigingsmail voor Keycloak.

## De beslissing die hieraan hangt

RC-156 bouwde één mailaccount voor heel Keycloak, met het wachtwoord uit de bootstrap en via de file-vault in `smtpServer` van elke realm. Die aanpak is gestopt na vier reviewrondes die alle vier DEZELFDE aanval vonden, elke keer via een andere drager van `manage-realm`: de gedelegeerde realmbeheerder, een service-account dat de `algoritmeregister`-blauwdruk de rol geeft, een rolmapping die de beheerder zelf legt op een gebruiker of groep, en een zelfgemaakte samengestelde rol op de `realm-management`-client. Elke ronde is met een echte 25.0.6 gemeten en elke reparatie was degelijk; de volgende ronde vond een dragersklasse die de vorige niet dekte.

De aanval is steeds identiek. Een projectbeheerder zet `smtpServer.host` van zijn realm op een luisteraar die hij beheert. Keycloak lost `${vault.smtp-password}` pas op bij het VERSTUREN en biedt het geheim met AUTH aan bij die luisteraar. Omdat alle realms hetzelfde bestand lezen, is dat het inlogwachtwoord van het hele platform.

Er zijn nog twee routes, en deze meting kiest ertussen:

- **De SPI.** Een eigen `EmailSenderProvider` die het relaywachtwoord uit de omgeving van de pod haalt en `smtpServer` volledig negeert. Dan is er in geen enkele realm nog een bestemming om om te leiden. Eén account blijft, de bootstrap blijft, `manage-realm` mag blijven, en er ontstaat geen koppeling tussen de dienst keycloak en de dienst mail.
- **Een account en een vault-sleutel per realm.** Werkt zeker, maar maakt van Keycloak een tussenhandelaar die namens elk project een mailaccount nodig heeft, inclusief levenscyclus, opruimen en roteren. Dat is precies de harde koppeling tussen twee diensten die we niet willen.

De SPI is de voorkeursroute. Deze meting bestaat omdat er één gemeld probleem is dat hem onderuit kan halen, en dat wil ik weten voordat er een plan op wordt geschreven.

## Wat er extern al is nagezocht

De `emailSender`-SPI is SYSTEEMBREED en per definitie niet per realm in te stellen ([Keycloak-forum](https://forum.keycloak.org/t/how-to-use-a-custom-emailsenderproviderfactory/4049), [EmailSenderProvider javadoc](https://www.keycloak.org/docs-api/latest/javadocs/org/keycloak/email/EmailSenderProvider.html)). Je registreert een `EmailSenderProviderFactory` via `META-INF/services` en wijst hem aan als standaardprovider. Er bestaat een werkend voorbeeld in het wild dat precies dit doet ([dasniko/keycloak-aws-ses-email-provider](https://github.com/dasniko/keycloak-aws-ses-email-provider)).

Het risico: er is gemeld dat een eigen e-mailprovider in PRODUCTIEMODUS terugvalt op `DefaultEmailSenderProvider` ([keycloak#14522](https://github.com/keycloak/keycloak/issues/14522)). Onze deployment draait productiemodus (`start`, niet `start-dev`) en zet de jars met een initcontainer in `/opt/keycloak/providers/`. Of dat samen werkt op 25.0.6 is exact de vraag.

## De vraag, in vijf deelvragen

Elke deelvraag heeft een assertie. Beantwoord ze alle vijf, ook als vraag 1 al negatief uitpakt, want dan is de reden waarom nog steeds waardevol.

**1. Wordt onze provider in productiemodus daadwerkelijk gebruikt?** Zet er iets in dat onmiskenbaar van ons is, zodat "hij werkt" niet te verwarren is met "de standaardprovider deed het". Assertie: een bevestigingsmail komt aan in de Mailpit-sink EN er is bewijs dat hij door onze code is verstuurd.

**2. Welke vlagvorm wijst hem aan op 25.0.6, en is er een bouwstap nodig?** Dit is dezelfde klasse val als de vault-resolver en de sleutels in de relay-configuratie: een SPI-optie die Keycloak niet kent, wordt STIL genegeerd en dan valt hij terug op de standaard zonder dat er iets misgaat bij het opstarten. Meet ook of `kc.sh build` of een herstart nodig is nadat de initcontainer de jar heeft neergezet, en of het na een verse pod nog steeds klopt. Assertie: de gekozen vorm is aantoonbaar opgepikt, niet alleen aantoonbaar meegegeven.

**3. Bereikt Keycloak de provider ook als de realm GEEN `smtpServer` heeft?** Dit is de deelvraag die de hele opzet kan laten kantelen en hij wordt makkelijk overgeslagen. Keycloak kan best vóór de provider al besluiten dat mail niet is ingesteld en niets versturen. Zo ja, dan is de opzet compleet. Zo nee, meet dan wat het MINIMUM is dat er moet staan, en bewijs met deelvraag 4 dat de inhoud daarvan er niet toe doet.

**4. Is de omleidingsvector werkelijk weg?** Dit is de belangrijkste assertie van deze taak. Zet op een testrealm een `smtpServer` met `host` op een luisteraar die de "aanvaller" beheert, met auth aan. Verstuur een bevestigingsmail. Assertie: het bericht komt bij de RELAY aan en de luisteraar van de aanvaller krijgt NIETS, in het bijzonder geen AUTH-regel met een wachtwoord. Leg de uitvoer van die luisteraar in de PR, ook als hij leeg is; juist dan.

**5. Hoe faalt het?** Als de provider om welke reden dan ook niet wordt gebruikt en de realms hebben geen `smtpServer`, gaat er dan niets uit, of gaat er iets uit langs een weg die we niet willen? Assertie: het faalt DICHT. Dat is de eigenschap die de SPI-route verdedigbaar maakt ook als hij ooit stilletjes terugvalt.

## Hoe

Op het lokale sandboxcluster. Daar staat de mailrelay al en zit er een Mailpit-sink achter in plaats van de echte mailserver, precies om dit soort dingen te kunnen meten. De sink is te zien op https://mailsink.sandbox.rijksapp.dev.

**Claim de sandbox voordat je begint** (`orch sandbox claim`) en geef hem daarna terug (`orch sandbox release`). Hij is gedeeld en er is beurtwisseling op.

De "luisteraar van de aanvaller" mag gewoon een tweede Mailpit of een kale netcat in een pod op het cluster zijn. Het gaat er niet om dat hij realistisch is, het gaat erom dat hij zichtbaar maakt wat hem wordt aangeboden.

De provider houdt het simpel: hij krijgt van Keycloak een al gerenderd onderwerp en een al gerenderde tekst, dus de sjablonen blijven van Keycloak en daar hoeft niets voor gebouwd te worden. Wat hij zelf doet is verbinden met de relay, authenticeren met inloggegevens uit de omgeving van de pod, en het bericht aanbieden. Het afzenderadres hoeft hij niet te bepalen; de relay zet de `From:` hoe dan ook zelf.

Volg voor het bouwen het patroon dat er al ligt in `keycloak-migration/custom-mapper` (pom, `src`, README, en de taak `build-keycloak-custom-mapper` in de Taskfile). Zet de jar-bron ernaast en merk hem zichtbaar aan als proef.

## Wat er landt

Het VERSLAG is het product: vijf antwoorden met de meetuitvoer erbij, en een aanbeveling in één alinea. Zet dat in de PR-beschrijving, niet alleen in een bestand.

De bron van de proef-jar mag mee de tak op zodat een vervolgtaak erop kan doorbouwen. Wijzigingen aan de Keycloak-deployment blijven op de tak en worden NIET uitgerold; als de meting slaagt, komt dat in het herziene plan met de bijbehorende afwegingen.

## Valkuilen

**Een genegeerde SPI-optie ziet eruit als een werkende opstelling.** Keycloak start gewoon door. "Hij staat in de command line" is geen bewijs; "hij is aantoonbaar opgepikt" wel.

**Kind dwingt netwerkbeleid niet af.** Voor deze meting is dat juist gunstig: de luisteraar van de aanvaller is er zonder moeite bereikbaar, dus de aanval is maximaal makkelijk. Komt de post dan nog steeds bij de relay, dan is dat een sterke uitkomst. Trek er wel geen conclusies uit over wat op ODCN bereikbaar is; dat is een andere vraag en hoort hier niet.

**Meet op een pod die al even draait.** Een verbindingstoets in de eerste seconden van een podleven zegt hier niets, en dat is bij deze relay al twee keer misgegaan.

**Verwar "de mail kwam aan" niet met "onze provider deed het".** Dat is de hele reden dat deelvraag 1 om een eigen merkteken vraagt.

**Niets aanzetten op productie.** Geen enkele wijziging uit deze taak hoort op odcn te landen.

## Wat hier buiten valt

- Het herziene plan zelf. Dat komt na deze meting en op basis van de uitkomst.
- De route met een account en een vault-sleutel per realm. Alleen als deze meting negatief uitpakt wordt dat het onderwerp, en dan met een eigen plan.
- De tak van RC-156 (`keycloak-verstuurt-bevestigingsmail-n-geprovisione`, tip `49d28787`) is BEWAARD en wordt hier niet aangeraakt. Daar staat werk in dat losstaat van het credentialmodel en dat in beide routes bruikbaar blijft: het blueprintmechanisme, de runtimegrendel die `verifyEmail` overslaat als een realm niet kan mailen, `merge_user_variables`, het afzenderadres per account, en dat een nieuwe gebruiker niet vanzelf geverifieerd is.

## Verifieerbaar

- Vijf antwoorden in de PR, elk met de uitvoer waaruit het antwoord volgt.
- De kopregels uit de Mailpit-sink van een bevestigingsmail die via de eigen provider is verstuurd.
- Het bewijs dat de gekozen SPI-vlagvorm is opgepikt op 25.0.6.
- De uitvoer van de luisteraar van de aanvaller bij deelvraag 4, ook als die leeg is.
- Het gedrag bij deelvraag 5, met de vaststelling of het dicht faalt.
- Een aanbeveling van één alinea: SPI of per realm, en waarom.
- De sandbox is teruggegeven (`orch sandbox release`).
