# De installatie in drie fasen: keuzes, geheimen en een overdracht

**Dit is een ONTWERPNOTITIE en geen bouwcontract.** Hij is bedoeld om mee te nemen naar de sessie die aan fundament werkt en daar verder is met de scheiding tussen configuratie en inrichting. Er staat hier geen stappenplan met asserties in; er staan beslissingen, de redenering eronder, en wat er vandaag al ligt om op te bouwen.

## Waar dit vandaan komt

De aanleiding is dat wij vandaag twee verschillende dingen op dezelfde manier opslaan, en dat op vier plekken tegelijk voelen.

**Gewenste toestand die evolueert**: welke componenten, welke versies, welke instellingen. Die hoort in git, want daar wil je een diff, een review en een terugweg. Versiebeheer en upgrades (een nieuwe OPI, een nieuwe Keycloak) zijn precies waarvoor git er is.

**Gegenereerde inloggegevens**: hoge entropie, één keer gemaakt, en een diff erop is betekenisloos of erger. Roteren is een operationele handeling en hoort geen git-actie te zijn.

Wij gooien die twee samen in SOPS en betalen dat in herversleutelingschurn, een bootstrap die met de hand toegepast moet worden, de val dat een commit in de bootstrap stil niets doet, en de kip-en-ei dat de AGE-sleutel moet bestaan voordat er iets versleuteld kan worden. Dat zijn geen losse ongelukken maar één ontwerpkeuze die zich vier keer laat voelen.

**De opbrengst van scheiden is groter dan minder gedoe.** Leven geheimen niet meer in git, dan vervalt het grootste deel van de taak van de AGE-sleutel. Wat overblijft is het versleutelen van projectbestanden in de projects-repo. SOPS wordt dan een gegevensbeschermingsmechanisme in plaats van een bootstrapmechanisme, en dat is een veel kleiner en beter uitlegbaar ding.

## De drie fasen

Een installatie is: **keuzes, uitvoeren, overdracht.** Die vorm is niet toevallig ook de vorm van een fundament-plugin, en dat is de reden om hem nu zo te bouwen.

| Fase | Standalone | Als fundament-plugin |
|---|---|---|
| 1. Keuzes | een configuratiebestand, met een script dat het heel basaal kan uitvragen | `PluginInstallation.spec.config` |
| 2. Uitvoeren | een installer die meer doet dan `kubectl apply` | het `Start()`-pad van de plugin, dat `PhaseInstalling` meldt |
| 3. Overdracht | een exportbestand plus een beheerderspagina | `ReportStatus` plus de console-assets die een plugin mag meeleveren |

Bouw je het nu in deze drie fasen, dan is de plugin later een omhulsel en geen herschrijving. Dat is het antwoord op de zorg dat ZAD niet alleen een plugin wordt: dezelfde configuratie, dezelfde uitvoerstap, en een terugkoppeling die standalone een pagina is en als plugin een console-asset.

## Fase 1: de keuzes

Dit is de configuratie die vandaag verspreid zit over `.env-taskfile-{cluster}`, de overlays per cluster en een stappenplan in iemands hoofd. Voorbeelden van wat er als vraag in hoort:

- Een eigen Forgejo, of een bestaande GitHub-repository.
- De upstream-SSO: welke, en met welke gegevens.
- De domeinen en de cluster-eigen hosts.
- **En de vraag die deze notitie heeft opgeleverd: waar komen de wachtwoorden vandaan.** Zie de volgende paragraaf; dit is een keuze en geen constante.

## De drie herkomsten van geheimen, en waarom dat een keuze is

Dit is de kern, en het inzicht is dat er niet één goed antwoord is maar drie, per installatie te kiezen.

**A. Vooraf bekend en gedeeld.** De sandbox. Daar is de AGE-sleutel gedeeld (`security/sandbox-key.txt`) en zijn de wachtwoorden expres altijd hetzelfde, zodat iedereen dezelfde omgeving heeft en een verse sandbox reproduceerbaar is. Het mechanisme bestaat al: `_generate-secrets-shared` in de Taskfile kent een `FIXED_PASSWORD`-variabele met precies dat doel. Hier is SOPS in git juist GOED: de geheimen zijn geen geheim, en in git staan geeft je reproduceerbaarheid.

**B. Gegenereerd en rechtstreeks toegepast.** Een nieuwe echte installatie. De installer genereert, doet `apply` op het cluster, en het komt nooit in git. Dan hoeft het onderweg ook niet versleuteld te worden, want er is geen tussenstation: het gaat van geheugen naar de Kubernetes-API. Dit is de standaard voor een productie-installatie.

**C. Gegenereerd en wel vastgelegd.** Optioneel, voor wie zijn geheimen buiten het cluster wil bewaren. Zie de derde categorie hieronder; dit is geen algemene modus maar een oplossing voor één specifieke categorie.

**Let op wat hiermee vervalt.** De TODO in `infrastructure/bootstrap/infrastructure/secrets/TODO.MD` zegt vandaag "THIS NEEDS TO BE FIXED, per cluster sops encrypt it". Onder deze indeling is dat de verkeerde afslag voor modus B: versleutelen maakt er weer een bestand van dat je moet bewaren en waar een sleutel bij hoort. Voor modus A klopt SOPS juist wel.

## De drie soorten geheimen

Los van waar ze vandaan komen, verschillen geheimen in wat er gebeurt als je ze kwijtraakt. Die indeling bepaalt wat er buiten het cluster moet bestaan.

**Machine-naar-machine.** Twee componenten binnen het cluster die elkaar herkennen: de relay-admin, de databaserollen, Redis, de mailaccounts, de metrics-auth. Kwijt is niet erg: opnieuw genereren en beide kanten bijwerken. Deze horen in geen enkele kluis en in geen enkele export. Ze zijn de meerderheid.

**Mens-logt-hier-in.** Keycloak, Forgejo, ArgoCD, MinIO, pgAdmin. Een mens moet deze kunnen vinden op het moment dat hij ergens in moet. Deze horen in de overdracht.

**Overleeft het cluster.** De AGE-sleutel, want die ontsluit de projectbestanden in de projects-repo, en de versleuteling van de Kopia-backups, want zonder die sleutel zijn je backups onleesbaar. Voor deze categorie geldt de regel:

> Een geheim dat gegevens beschermt die het cluster OVERLEVEN, moet ergens buiten het cluster bestaan. Een geheim dat alleen twee componenten binnen het cluster aan elkaar knoopt, mag je gewoon opnieuw genereren.

Dit is ook waarom "wachtwoorden veranderen in principe niet" de aanname is om scherp te houden. Ze veranderen wel: na een incident, bij een lek, bij een component dat een ander formaat eist. En belangrijker: de vraag is niet of ze veranderen maar wat er gebeurt als het cluster weg is.

## Fase 3a: de export, voor herstel

Er bestaat vandaag al een overdrachtsbestand. `_generate-secrets-shared` schrijft `secrets-overview-{MODE}-{CLUSTER_TYPE}.yaml`, met bovenaan "handle with care" en "copy passwords and delete this file manually", en het staat in `.gitignore`. Dit is dus geen nieuw ding om te bedenken maar een bestaand ding om af te maken.

**Voorstel: maak er een importeerbare CSV van.** Bitwarden heeft een gedocumenteerd CSV-formaat met een vaste, hoofdlettergevoelige kopregel:

```
folder,favorite,type,name,notes,fields,reprompt,login_uri,login_username,login_password,login_totp
```

1Password kan datzelfde CSV importeren, maar alleen in de desktopapps en niet in de webversie. Eén CSV in het Bitwarden-formaat bedient dus allebei, en dat scheelt een tweede exportpad. Voor 1Password bestaat daarnaast `op item create` via de CLI, maar dat vraagt dat de beheerder die heeft ingericht, dus als standaardweg is CSV beter.

**Wat er in gaat**: de categorie mens-logt-hier-in, plus de categorie overleeft-het-cluster. Dat is het verschil tussen een export van een handvol regels die een beheerder daadwerkelijk in zijn kluis zet, en een dump van twintig regels die hij wegklikt.

**Wat er niet in gaat**: alles machine-naar-machine.

Het bestand is eenmalig en wegwerpbaar: gebruiken, importeren, verwijderen. Het is geen levende opslag.

## Fase 3b: de beheerderspagina, voor gemak

Naast de export komt er een pagina onder Beheer, in dezelfde geest als het bestaande tabblad Toegang. Weergave per dienst:

```
Kop:        <servicenaam>
url:        <de ingress-url>
login:      <gebruikersnaam>
wachtwoord: <verborgen, met een toon-knop>
```

**Niet opslaan maar renderen.** ZAD leest vandaag al versleutelde wachtwoorden en toont ze achter een rolpoort; dat is precies wat het tabblad Toegang doet met het realm-adminwachtwoord en de OTP-code (`opi/web/router.py`, rond regel 1570). Deze pagina kan de waarden dus LIVE uit de losse Secrets lezen op het moment van renderen.

Dat is bewust anders dan het alternatief dat op tafel lag, namelijk alles één keer als blob of JSON in één Secret zetten. Die blob heeft drie nadelen die renderen niet heeft: er ontstaat een tweede kopie die kan verouderen, er ontstaat één object dat het hele platform waard is om te stelen, en een rotatie moet iemand handmatig doorvoeren. De wachtwoorden staan toch al op het cluster; het waardevolle is de AGGREGATIE, en aggregeren kun je bij het tonen.

**Waar de blob wél zin zou hebben is precies waar de pagina niets kan**: als het cluster weg is. Dat is de derde categorie, en daarvoor is de export er. Vandaar de scheiding: **de pagina is voor gemak, de export is voor herstel.**

**De lijst moet configureerbaar zijn.** Welke diensten er getoond worden, hoort geen hardgecodeerde lijst te zijn maar configuratie, zodat een installatie met minder of andere componenten geen dode regels toont.

**Open punt: waar komt de URL vandaan.** `opi/core/cluster_config.py` kent vandaag interne hosts (`minio_host`, `mail_relay_host`) en de Keycloak-discovery-URL, maar geen publieke URL per dienst. Twee wegen: uitlezen uit de Ingress-objecten in het cluster, wat de waarheid is en een domeinwijziging vanzelf volgt, of declareren in de configuratie van fase 1, wat simpeler is maar kan gaan afwijken. Voorkeur: uitlezen, met een gedeclareerde terugval voor diensten zonder Ingress.

## Wat dit oplevert voor de beheerder

Na installatie is de enige ingang die je nodig hebt: de ZAD-URL, het lokale adminaccount dat we toch al aanmaken, en eventueel de directe URL naar de Toegang-pagina. Alles wat daarachter zit vindt hij daar.

## Waarom niet een eigen vault

Dit pad is al eens ingelopen: `vault-init-secret.yaml` staat uitgecommentarieerd in `infrastructure/bootstrap/infrastructure/secrets/templates/kustomization.yaml`. Een vault heeft een unseal-sleutel en een root-token, dus hij verplaatst de kip-en-ei in plaats van hem op te lossen, en je krijgt er een component bij om te beheren en te upgraden. Voor "één login na installatie" heb je hem niet nodig: dat is het lokale adminaccount plus de pagina hierboven.

## Erbij: een welkomstscherm bij de eerste login

Klein en los van het bovenstaande, maar het hoort bij dezelfde gedachte dat een verse installatie zichzelf moet uitleggen. Bij de eerste login van een gebruiker een "welkom bij ZAD"-scherm tonen, met een andere inhoud voor een beheerder dan voor een gewone gebruiker. Nu simpel, later mooier.

Er ligt een werkend voorbeeld in Wies dat de moeite van het kopiëren waard is, want de vorm is precies goed. Een context processor (`wies/core/context_processors.py`, functie `onboarding`) zet `show_onboarding` in de basistemplate, en die blijft `True` tot `user.onboarding_completed_at` gezet is. De inhoud staat in `wies/core/jinja2/parts/onboarding/`, met een wizard-template. Eén veld op de gebruiker, één context processor, één blok in de basistemplate: dat is de hele constructie.

Voor een beheerder zou de welkomsttekst logischerwijs naar de Toegang-pagina wijzen.

## Wat fundament hiervoor nog niet heeft

Twee dingen die de sessie die daar verder is moet meenemen, want ZAD raakt ze als eerste.

**Er is geen kanaal voor een MEEGEGEVEN geheim.** `PluginInstallation.spec.config` is een platte `map[string]string` en die landt als omgevingsvariabelen met voorvoegsel `FUNP_` op de plugin-deployment (`plugin-controller/pkg/controller/resources.go:213`), in een cluster-scoped CR. Dat is prima voor keuzes en onbruikbaar voor geheimen. Zolang de plugin alles zelf genereert is er niets aan de hand, maar zodra een beheerder iets moet aanleveren (upstream-SMTP-gegevens, een DNS-API-token, PKIoverheid-certificaten) is er geen afgesproken weg en val je terug op "maak een Secret met deze naam", wat ongedocumenteerde koppeling is. Een `secretRef` in het installatiecontract zou dat dichten.

**Genereren in de plugin kan wel.** De controller maakt per plugin een namespace met een ServiceAccount, en de definitie kan RBAC op `secrets` aanvragen; dat staat in de controllertests. De installatiefase bestaat dus al en is van de plugin. Wat wij "een script dat de basis neerzet" noemen is daar `Start()`, en dat is beter dan een script omdat een controller reconcilieert en een script opnieuw goed gedraaid moet worden.

**Wat het versiebeheer betreft is fundament al verder dan wij.** `definitionRef.pluginVersion` met een onveranderlijke `definitionHash` is een expliciete, vastgepinde toestemming van de beheerder. Dat is sterker dan onze huidige praktijk van een image-tag pinnen in een overlay, waar die pin een commit is die iemand nog met de hand moet toepassen.

## Openstaande vragen

1. Waar krijgt de derde categorie (overleeft-het-cluster) zijn kopie buiten het cluster, en is de eenmalige export daarvoor genoeg of wil je er een tweede, bewustere weg voor.
2. Waar komt de getoonde URL vandaan: uit de Ingress-objecten of uit de configuratie.
3. Hoe levert een beheerder een meegegeven geheim aan, zowel standalone als via het plugincontract.
4. Wat er precies configureerbaar is aan de dienstenlijst op de Toegang-pagina, en of dat dezelfde configuratie is als fase 1.

## Wat er NIET moet gebeuren

- ArgoCD een geheimen-repository laten adopteren. Dan is git weer de waarheid en draait een rotatie die je in het cluster doet bij de volgende sync terug, en dat is precies de koppeling die deze hele notitie weghaalt. Wil je zichtbaarheid, dan is dat de pagina.
- De geaggregeerde blob als opslag. Zie hierboven: renderen heeft alle voordelen en geen van de nadelen.
- SOPS als bootstrapmechanisme houden voor modus B. Voor modus A blijft het staan en is het juist de goede keuze.
