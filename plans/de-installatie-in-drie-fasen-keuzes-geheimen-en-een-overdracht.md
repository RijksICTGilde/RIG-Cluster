# De installatie in drie fasen: keuzes, geheimen en een overdracht

**Dit was een ONTWERPNOTITIE en is de gekozen weg geworden.** Er staat hier nog steeds geen stappenplan met asserties in; er staan beslissingen, de redenering eronder, en wat er vandaag al ligt om op te bouwen. Het stappenplan is een apart document, en dit is wat het als vast mag aannemen.

De notitie is aangevuld na een leesronde in de fundament-sessie. Die aanvullingen staan niet apart maar zijn in de betreffende paragrafen verwerkt, met een aantekening waar ze een eerdere conclusie bijstellen.

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
- **En de vraag die deze notitie heeft opgeleverd: wat gebeurt er met de wachtwoorden.** Niet waar ze VANDAAN komen, want dat is altijd dezelfde generator; wel waar ze HEEN gaan. Zie de paragraaf over de drie bestemmingen; dit is een keuze en geen constante.

## Fase 2: uitvoeren, en waarom het genereren al bestaat

Dit is de paragraaf die in de eerste versie ontbrak, en het gat was groter dan alleen een kop: de notitie besprak drie herkomsten van geheimen alsof dat drie te bouwen mechanismen waren. Dat is niet zo. **Het mechanisme dat alle wachtwoorden vooraf aanmaakt bestaat, draait, en heeft alle clusters die er nu staan ingericht.**

In `infrastructure/bootstrap/infrastructure/secrets/templates/` staan dertien Secret-blauwdrukken: Forgejo, Keycloak, MinIO, pgAdmin, PostgreSQL, Redis, de mailrelay, de metrics-auth, chisel. Elk veld dat een wachtwoord moet worden draagt een annotatie:

```yaml
stringData:
  KEYCLOAK_ADMIN_PASSWORD: "changeMe123!"    # @secret-gen:random:16
  KEYCLOAK_ADMIN_CLIENT_SECRET: "changeMe123!" # @secret-gen:random:64
```

`task generate-secrets-for-cluster <cluster>` loopt die map af, vult elk geannoteerd veld met verse entropie van de gevraagde lengte (`random:N`, of `bcrypt:N` voor wat een hash wil), en schrijft het resultaat naar `secrets/config/overlays/<cluster>/`. Velden zonder annotatie houden hun sjabloonwaarde, dus configuratie en geheim staan in hetzelfde bestand zonder door elkaar te lopen.

**Een nieuwe installatie hoeft dus niets te bedenken. Hij draait de generator tegen zijn eigen clusternaam en heeft een complete, verse set.** Dat is de hele fase 2 voor geheimen. Wat een installatie kiest is niet HOE ze ontstaan maar WAT ermee gebeurt:

| Bestemming | Wat je ervoor terugkrijgt | Wat het kost |
|---|---|---|
| Rechtstreeks `apply` op het cluster | geen bestand, geen sleutel, geen kip-en-ei | niets reconcilieert het |
| Versleuteld in git, met een Argo-app | reconciliatie, diff, terugweg | git wint van het cluster (zie de invariant hieronder) |
| Allebei | de eerste inrichting gaat snel, daarna bewaakt | de twee kunnen uit elkaar lopen |

Modus A uit de volgende paragraaf is dezelfde generator met `FIXED_PASSWORD` gezet. Er is dus één generator met drie bestemmingen, en dat is een veel kleiner ding om te bouwen dan drie herkomsten.

**Wat de generator vandaag NIET kan**, en dat is de echte bouwlijst:

1. Er is geen pad dat rechtstreeks toepast. Hij schrijft bestanden en gaat uit van SOPS. De bestemming "apply, nooit een bestand" bestaat nog niet. *Wel makkelijker geworden: de generatie staat sinds `0ecfcd1e` in `scripts/generate-secrets.sh` en krijgt zijn invoer uit omgevingsvariabelen, dus er is nu een plek om die bestemming aan toe te voegen.*
2. ~~Hij kent geen reconciliatie.~~ **Bijgesteld, en de eerste formulering was fout.** Hij roteerde niet: een bestaand uitvoerbestand werd overgeslagen. Het gat zat een niveau dieper, op VELDNIVEAU. Kreeg een blauwdruk er later een veld bij, dan bestond het bestand al, werd het in zijn geheel overgeslagen, en landde dat veld op een draaiend cluster nooit meer. Gemeten op odcn: `keycloak-admin-secret` mist vier velden die de blauwdruk wel heeft. **Opgelost in `0ecfcd1e`**: bestaat het bestand niet, dan wordt het volledig gegenereerd; bestaat het wel, dan worden alleen ontbrekende velden aangevuld, en wordt een bestaande waarde nooit overschreven.
3. Het overzichtsbestand dat hij oplevert is de kiem van de export uit fase 3a, maar het faalde tot voor kort stil (zie daar).

**En hier hoort een keuze over vorm bij.** De notitie stelde standalone een installerscript voor naast het `Start()`-pad van de plugin. Dat zijn dan twee implementaties van fase 2, en de standalone is degene die verrot omdat hij minder gedraaid wordt. Bouw in plaats daarvan één ding en roep het van twee kanten aan.

Hoe dat eruitziet is af te kijken bij fundament zelf. Een plugin is daar een container-image dat een HTTP-server op poort 8080 draait met `/livez` en `/readyz`, configuratie krijgt als `FUNP_`-omgevingsvariabelen, en optioneel `Reconcile` implementeert (standaard elke vijf minuten). Hun eigen referentieplugin, `plugins/cert-manager/plugin.go`, is 121 regels Go die het echte werk doen met `exec.CommandContext(ctx, "helm", ...)`, en de Dockerfile is het binary plus `apk add helm`. **Het contract is dus een image met een klein serverlaagje; het werk daarbinnen mag gewoon CLI-aanroepen zijn.**

Daarmee vervalt het idee dat we alles naar een andere taal moeten overzetten om plugin-ready te zijn. Wat wel moet is dat elke stap (a) zijn configuratie uit omgevingsvariabelen haalt, (b) veilig herhaald kan worden, en (c) geen interactieve stap en geen lokaal bestand nodig heeft dat moet overleven. Punt (c) is de AGE-sleutel opnieuw, nu met een scherpere rand: een plugin-pod heeft geen plek voor `security/key.txt`.

En let op wat er in fundament ontbreekt aan invoer: `spec.config` is een platte `map[string]string` zonder schema. De rijke UI-machinerie in de PluginDefinition (`menu`, `uiHints.formGroups`, `customComponents`) gaat over de CRD's die de plugin MEEBRENGT, niet over het installeren ervan. Voor ZAD betekent dat: onze fase-1-keuzes zijn geen handvol platte knoppen en horen dus een eigen CRD te worden met een formulier eromheen, niet in `spec.config` geperst.

## De invariant, en de puzzel die eronder ligt

Los van welke bestemming je kiest, is er één ding dat altijd waar moet blijven:

> Het wachtwoord dat in het Secret op het cluster staat, IS het echte wachtwoord.

Alles wat daarvan afwijkt is per definitie een kopie die kan verouderen, en een verouderde kopie van een wachtwoord is erger dan geen kopie: iemand gaat hem gebruiken.

Die invariant staat op gespannen voet met reconciliatie uit git, en dat is geen detail maar de kern van de puzzel. Is git de bron en synct ArgoCD, dan wint git, en draait een rotatie die je in het cluster doet bij de volgende sync terug. Git-als-bron geeft je reconciliatie en kost je de invariant. Cluster-als-bron geeft je de invariant en heeft geen reconciliatie.

**Die knoop wordt hier bewust niet doorgehakt.** Wat wel vastligt is dat de vraag niet "wel of niet in git" is, maar: wie mag een wachtwoord wijzigen, en wat gebeurt er daarna. Dat valt uiteen in drie stukken die los beantwoord kunnen worden:

1. Hoe komt een wijziging die in het cluster is gedaan terug in git, of vervalt git als bron voor dat ene geheim.
2. Hoe pakt de toepassing die het gebruikt de wijziging op: een herstart, een hot reload, of helemaal niet.
3. Hoe weet je dat beide kanten klaar zijn, want tussen die twee momenten is het wachtwoord aan de ene kant al veranderd en aan de andere kant nog niet.

Dat derde punt is de reden waarom "opnieuw genereren en beide kanten bijwerken" verderop wordt bijgesteld: verlies is goedkoop, rotatie is een uitrol.

## De drie bestemmingen van geheimen, en waarom dat een keuze is

Dit is de kern, en het inzicht is dat er niet één goed antwoord is maar drie, per installatie te kiezen.

**Bijstelling na de leesronde:** dit zijn geen drie HERKOMSTEN. Ze komen alle drie uit dezelfde generator uit fase 2. Het zijn drie BESTEMMINGEN, plus in geval A een schakelaar op diezelfde generator. Dat scheelt twee mechanismen die niet gebouwd hoeven te worden. De beschrijvingen hieronder kloppen verder; lees ze als "wat gebeurt er met het resultaat".

**A. Vooraf bekend en gedeeld.** De sandbox. Daar is de AGE-sleutel gedeeld (`security/sandbox-key.txt`) en zijn de wachtwoorden expres altijd hetzelfde, zodat iedereen dezelfde omgeving heeft en een verse sandbox reproduceerbaar is. Het mechanisme bestaat al: `_generate-secrets-shared` in de Taskfile kent een `FIXED_PASSWORD`-variabele met precies dat doel. Hier is SOPS in git juist GOED: de geheimen zijn geen geheim, en in git staan geeft je reproduceerbaarheid.

**B. Gegenereerd en rechtstreeks toegepast.** Een nieuwe echte installatie. De installer genereert, doet `apply` op het cluster, en het komt nooit in git. Dan hoeft het onderweg ook niet versleuteld te worden, want er is geen tussenstation: het gaat van geheugen naar de Kubernetes-API. Dit is de standaard voor een productie-installatie.

**C. Gegenereerd en wel vastgelegd.** Optioneel, voor wie zijn geheimen buiten het cluster wil bewaren. Zie de derde categorie hieronder; dit is geen algemene modus maar een oplossing voor één specifieke categorie.

**Let op wat hiermee vervalt.** De TODO in `infrastructure/bootstrap/infrastructure/secrets/TODO.MD` zegt vandaag "THIS NEEDS TO BE FIXED, per cluster sops encrypt it". Onder deze indeling is dat de verkeerde afslag voor modus B: versleutelen maakt er weer een bestand van dat je moet bewaren en waar een sleutel bij hoort. Voor modus A klopt SOPS juist wel.

## De drie soorten geheimen

Los van waar ze vandaan komen, verschillen geheimen in wat er gebeurt als je ze kwijtraakt. Die indeling bepaalt wat er buiten het cluster moet bestaan.

**Machine-naar-machine.** Twee componenten binnen het cluster die elkaar herkennen: de relay-admin, de databaserollen, Redis, de mailaccounts, de metrics-auth. Kwijt is niet erg: opnieuw genereren en beide kanten bijwerken. Deze horen in geen enkele kluis en in geen enkele export. Ze zijn de meerderheid.

  Wel scherper formuleren dan de eerste versie deed: **verlies is goedkoop, rotatie is een uitrol.** "Beide kanten bijwerken" verbergt een volgorde. Het adminwachtwoord van de mailrelay roteren raakt de relay en OPI, en de relay bewaart zijn accounts in PostgreSQL, dus daar zit ook nog een derde kant aan. Tussen het moment dat de ene kant het nieuwe wachtwoord heeft en de andere kant nog het oude, werkt de koppeling niet. Dat is te overzien, maar het is geen `kubectl edit`.

**Mens-logt-hier-in.** Keycloak, Forgejo, ArgoCD, MinIO, pgAdmin. Een mens moet deze kunnen vinden op het moment dat hij ergens in moet. Deze horen in de overdracht.

**Overleeft het cluster.** De AGE-sleutel, want die ontsluit de projectbestanden in de projects-repo, en de versleuteling van de Kopia-backups, want zonder die sleutel zijn je backups onleesbaar. Voor deze categorie geldt de regel:

> Een geheim dat gegevens beschermt die het cluster OVERLEVEN, moet ergens buiten het cluster bestaan. Een geheim dat alleen twee componenten binnen het cluster aan elkaar knoopt, mag je gewoon opnieuw genereren.

**Er is een vierde categorie, en die is bij de eerste indeling gemist: niet-geheime configuratie die meereist in een Secret.** Het scherpste voorbeeld staat in de mailrelay. `MAIL_FROM_LOCAL` en `MAIL_DOMAIN` zitten in `mail-relay-credentials`, maar het zijn geen geheimen: het is het afzenderadres. Ze MOETEN gelijk zijn aan `mail_from_address` in `opi/core/cluster_config.py`, en de docstring van `get_mail_from_address` waarschuwt daar met zoveel woorden voor: driften ze, dan ziet een ontwikkelaar het ene adres terwijl het andere het pand verlaat. Op fundament-poc is dat op 26 augustus 2026 met de hand naast elkaar gelegd voordat de sleutels erin gingen.

Zulke waarden horen aan de GIT-kant van de streep, want ze zijn gewenste toestand die evolueert, precies zoals de eerste paragraaf het beschrijft. Verhuizen de geheimen uit git en zij liften mee, dan verdwijnt de helft van een afspraak uit het zicht en controleert niemand hem meer. Twee wegen: haal ze uit het Secret en laat beide kanten uit dezelfde bron lezen, of laat ze staan en zet er een controle op die klaagt als de twee uit elkaar lopen. Het eerste is beter, het tweede is goedkoper.

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

**En daar zit meteen de zwakte, want de voorganger heeft een staat van dienst.** Een CSV met wachtwoorden in platte tekst op schijf is precies het artefact waarvan deze notitie elders zegt dat het niet mag blijven bestaan, en "gebruiken, importeren, verwijderen" wordt door niets afgedwongen. Het bestaande `secrets-overview` droeg de instructie "copy passwords and delete this file manually" al, en was op fundament-poc gewoon LEEG: een afkapfout in de taak schreef de kop en gooide de rest weg, en niemand merkte het tot iemand de wachtwoorden nodig had. Dat is op 25 augustus 2026 gerepareerd door de regels naar een tijdelijk bestand te schrijven en pas samen te voegen als er iets in staat.

Bouw de export op die machinerie, dan hoort er een assertie bij: het bestand is niet leeg, en elke dienst die de configuratie noemt komt erin voor. Een export die stil niets bevat is erger dan geen export, want iemand denkt dat hij hem heeft.

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

  De terugval is goedkoper dan gedacht: `cluster_config` heeft per cluster al een `ingress_postfix` (`.fundament-poc.rijksapp.dev`, `.sandbox.rijksapp.dev`), en elke dienst hangt daaronder. Er hoeft dus niets nieuws gedeclareerd te worden. Belangrijker is wat je doet als de twee bronnen het oneens zijn: MELD dat, want een Ingress die niet onder de postfix van zijn eigen cluster hangt betekent een halve domeinmigratie, en dat is precies de storingsklasse van de router-zone waar we eerder op SERVFAIL liepen.

## Wat dit oplevert voor de beheerder

Na installatie is de enige ingang die je nodig hebt: de ZAD-URL, het lokale adminaccount dat we toch al aanmaken, en eventueel de directe URL naar de Toegang-pagina. Alles wat daarachter zit vindt hij daar.

Dat lokale account is geen aanname meer. Het heet `zad-admin`, krijgt bij de eerste bootstrap een gegenereerd wachtwoord in het cluster-Secret `zad-local-admin`, en staat in de adminlijst van ZAD. Het hoeft niet te wijzigen bij de eerste login, want het is een terugvalaccount en geen persoonsaccount. **Onder de indeling hierboven is dit de belangrijkste regel van de hele export**: het is de enige die je terugbrengt als de upstream-SSO eruit ligt, en dat is precies de reden dat hij bestaat.

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
2. Waar komt de getoonde URL vandaan: uit de Ingress-objecten of uit de configuratie. *Grotendeels beantwoord: uitlezen, met `ingress_postfix` als terugval, en melden bij onenigheid.*
3. Hoe levert een beheerder een meegegeven geheim aan, zowel standalone als via het plugincontract.
4. Wat er precies configureerbaar is aan de dienstenlijst op de Toegang-pagina, en of dat dezelfde configuratie is als fase 1.
5. **De grote: hoe verhoudt de invariant zich tot reconciliatie.** Zie de paragraaf daarover. Wie mag een wachtwoord wijzigen, hoe komt die wijziging terug in de bron, en hoe pakt de toepassing hem op. Bewust uitgesteld, niet vergeten: de weg hieronder is zo gekozen dat het antwoord later nog beide kanten op kan.
6. Wat een tweede run van de generator moet doen met geheimen die al bestaan. Vandaag maakt hij nieuwe, en dat is precies wat je niet wilt bij een aanvulling of een reparatie.

## Wat er NIET moet gebeuren

- ArgoCD een geheimen-repository laten adopteren **zonder de invariant te hebben beantwoord**. Dan is git weer de waarheid en draait een rotatie die je in het cluster doet bij de volgende sync terug. *Bijgesteld na de leesronde:* dit is geen verbod meer maar een volgorde. De bestemming "versleuteld in git, met een Argo-app" staat bewust in de tabel bij fase 2, want hij is de enige die vandaag reconciliatie geeft. Wat niet mag is hem kiezen en dan doen alsof de spanning met de invariant er niet is. Wil je alleen zichtbaarheid, dan is dat de pagina en niet de repo.
- De geaggregeerde blob als opslag. Zie hierboven: renderen heeft alle voordelen en geen van de nadelen.
- SOPS als bootstrapmechanisme houden voor modus B. Voor modus A blijft het staan en is het juist de goede keuze.

## De eerste stappen

Deze notitie is de gekozen weg geworden, dus hier staat waar het uitvoeren begint. Geen stappenplan met asserties (dat is een apart document), maar de volgorde die uit het bovenstaande volgt, en waarom die volgorde.

**Eerst de generator, want daar hangt de rest aan.** Twee dingen: een tweede run die ziet wat er al is in plaats van alles opnieuw te maken, en een bestemming die rechtstreeks toepast zonder onderweg een bestand te maken. Die twee samen maken van "genereren" een reconciliatie, en pas daarna is het zinvol om over standalone-versus-plugin te praten, want dan is er iets om in beide vormen aan te roepen.

**Daarna de export, want die is bijna af en levert meteen waarde op.** Het overzichtsbestand bestaat, is net gerepareerd en staat al in `.gitignore`. Wat het nodig heeft is de indeling in categorieen (alleen mens-logt-hier-in en overleeft-het-cluster), het Bitwarden-CSV-formaat, en de assertie dat hij niet leeg is. Dat is af te ronden zonder dat de invariant beantwoord hoeft te zijn.

**Dan pas de beheerderspagina.** Die leest live uit de losse Secrets en heeft dus geen enkele keuze uit de vorige paragrafen nodig, maar hij is wel het minst urgent: hij is voor gemak, en de export is voor herstel.

**Het welkomstscherm loopt hier los van** en kan wanneer dan ook.

Wat expliciet NIET vooraan staat is de vraag of geheimen in git horen. Elk van de stappen hierboven is te zetten zonder dat te beslissen, en elk van hen maakt die beslissing daarna makkelijker in plaats van moeilijker.
