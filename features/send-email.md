# E-mail versturen (send-email)

Een project zet de dienst `send-email` aan en krijgt een eigen SMTP-account op de mailrelay
van het platform. De relay biedt de post met één geauthenticeerd account aan bij de
mailserver van de organisatie. Geen enkel project praat zelf met die mailserver, en geen
enkel project kent het wachtwoord ervan.

Alleen uitgaand. Er is geen postbus en er komt niets binnen.

Het ontwerp en de afwegingen staan in `plans/mailrelay.md`; dit document beschrijft wat er
staat en hoe je het gebruikt.

## Eerst goedkeuring, dan alles tegelijk

Het aanzetten van de dienst is een **aanvraag**. Die loopt via de generieke goedkeuringsweg
die er al was, dezelfde die `publish-on-web` voor domeinen gebruikt: de dienst declareert
een `ApprovalSpec` in `config_approvals(ConfigLayer.PROJECT)` en beantwoordt
`ensure_approval_requests()`. Geen eigen scherm en geen tweede mechanisme — de aanvraag komt
in de bestaande beheerdersinterface (`/admin/approvals`) te staan, naast de domeinaanvragen.

Het gedrag per status is expres saai:

| Status | Wat er gebeurt |
|---|---|
| geen aanvraag / in behandeling | **niets.** Geen account op de relay, geen netwerkbeleid, geen credentials in de projectsecrets. |
| afgewezen | hetzelfde: niets. |
| goedgekeurd | account, netwerkbeleid en variabelen komen er in één keer bij. |

Er is dus geen half werkende tussentoestand. Dat is de reden dat er precies één poort is
(`is_approved` in het servicepakket) waar alle vier de dingen aan hangen: een account zonder
netwerkbeleid, of andersom, is een toestand die niemand kan uitleggen en die bij het
opruimen overblijft.

**Een ingetrokken goedkeuring ruimt op.** Wordt de status van goedgekeurd afgehaald, dan
loopt de volgende verwerking van het project hetzelfde opruimpad als een projectverwijdering
(`MailManager._delete_account`, de enige verwijdering die er is): het account gaat van de
relay af en de vermelding uit het projectbestand. Zonder dat blijft er een weesaccount staan
waar niets meer naar wijst.

**De wachtstand is zichtbaar.** De dienst levert per deployment een notice
(`notices_for`), zodat de stand op de projectpagina staat en via dezelfde weg in de API
terugkomt als bij een domeinaanvraag. Een dienst die aanstaat en stil niets doet is precies
de fout die bij de domeinaanvraag is weggehaald.

## Wat een project krijgt

Een component dat de dienst aanvinkt krijgt vijf variabelen uit de projectsecrets:

| Variabele | Wat het is |
|---|---|
| `SMTP_HOST` | de relay binnen het cluster |
| `SMTP_PORT` | 587 (submission) |
| `SMTP_USERNAME` | het account van dit project |
| `SMTP_PASSWORD` | het wachtwoord van dat account |
| `SMTP_FROM` | het afzenderadres dat de relay afdwingt |

Elke variabele heeft ook een `APP_`-alias, net als bij de andere diensten.

## Wat je instelt

Op projectniveau, in de wizard of via de API:

| Veld | Betekenis |
|---|---|
| `from-name` | de naam die de ontvanger boven het bericht ziet |
| `from-local-part` | het deel voor de @; standaard `noreply` |
| `messages-per-day` | het dagbudget van dit project, maximaal 5000 |

En wat je niet instelt: het domein achter de @ ligt vast op het maildomein van het platform
(`mail.rijksapp.nl` op productie — let op het enkelvoud, `rijksapps.nl` is de zone van
ODC-Noord zelf). Dat is geen betutteling maar techniek: een `From:` in een vreemd domein
haalt DMARC nooit, dus die post komt toch niet aan.

`from-domain` bestaat wel in het model, maar is **geen zelfbediening**: het veld draagt
`platform_managed_fields`, dus een PUT die het meestuurt is een 422 en een formulierveld is
er niet. Een eigen domein kost eerst één DKIM-record in de zone van dat domein, en die ronde
loopt met de hand tot een project erom vraagt. Zonder die grens zou een project na één
goedkeuring zijn afzenderdomein alsnog kunnen verzetten: de goedkeuring wordt één keer
gevraagd, niet opnieuw bij elke wijziging. Wat een projectdomein later goedkoop maakt is dat
de envelope altijd op óns domein blijft — SPF geldt voor het envelope-domein, dus een
projectdomein kost één record in plaats van een volledige set.

### Voorbeeld

```yaml
services:
  - name: send-email
    config:
      from-name: Algoritmeregister
      from-local-part: noreply
      messages-per-day: 750
      # accounts: door het platform geschreven, zie hieronder
```

## Wat het platform beheert

`approval`, `accounts` en `from-domain` zijn platformdata: een beheerder beslist, en OPI maakt het account
aan op de relay en schrijft neer wat het gemaakt heeft (per cluster: gebruikersnaam, AGE-versleuteld wachtwoord, afzenderadres en
bounce-adres). Alle drie dragen `platform_managed_fields`, dus de API kan ze niet wissen
en niet overschrijven — een PUT die ze weglaat verliest ze niet. Bij `approval` is dat niet
alleen netjes maar de kern: een project dat zijn eigen status op `approved` kan zetten heeft
geen goedkeuring.

Het wachtwoord wordt één keer gegenereerd en meteen weggeschreven, om dezelfde reden als bij
het Keycloak-realm: het bestaat nergens anders, en een latere fout in dezelfde run zou een
account op de relay achterlaten waar geen projectbestand het wachtwoord van heeft.

## Het account hangt aan het project

Eén account per project, niet per deployment. Daar hangen ook het dagbudget en het
bounce-adres aan, en een account per deployment zou één project meerdere budgetten geven en
een klacht herleidbaar maken naar een deployment die niemand buiten het platform kent.

Gevolg voor het opruimen: de dienst uit één deployment halen verwijdert het account niet
zolang een andere deployment van hetzelfde project hem nog gebruikt. Pas de laatste laat het
account opheffen.

## Het netwerkbeleid komt uit de dienst

De relay draait in een eigen namespace (`rig-prd-ron` op productie, `rig-ron` op local en
sandbox: ODCN eist dat een namespace daar met de clusterprefix begint) en niet in de
operations-namespace, want de Calico-annotatie `egress.projectcalico.org/egressGatewayPolicy`
neemt exact één waarde: RON aanzetten op `rig-prd-operations` kost daar het internet, en
daarmee ArgoCD, de registry en Keycloak.

De prijs daarvan is dat het verkeer erheen expliciet open moet: de tenant-basis opent alleen
de operations-namespace, dus zonder regel loopt een pod vast op connect nadat DNS al
geslaagd is. Die regel komt uit de dienst zelf, via `contribute_deployment_manifests`: per
component dat de dienst aanvinkt één egress-NetworkPolicy naar de relay op 587, en niets
anders. Dat is met opzet niet met de hand gedaan — de bestandsnaam begint met
`{deployment}-send-email-`, waar de opruimstap op matcht, dus de regel verdwijnt weer zodra
de dienst uitgaat. Handmatig beheerde regels aan die kant zijn precies hoe de storing van
10 juni ontstond.

De ingangskant staat bij de relay zelf, in
`infrastructure/bootstrap/infrastructure/mail/controller/overlays/*/network-policies/`.

## Het account van ZAD zelf

ZAD moet ook kunnen mailen — wachtwoord instellen en resetten voor lokale accounts, en het
herstel bij verlies van een OTP-toestel staan er al maanden op te wachten. Maar ZAD is geen
project: er is geen projectbestand om een account aan op te hangen.

Het is daarom **geen tweede soort account.** Het is een gewoon account op de relay, langs
dezelfde weg gemaakt als dat van een project: OPI logt in op het BEHEERDERSACCOUNT van de
relay en maakt het aan via de connector, precies zoals het bij Keycloak een realm en bij
PostgreSQL een database maakt. Het verschil zit in de aanroeper — de opstart van het platform
in plaats van een projectverwerking — en in het budget, in niets anders. Een projectbestand
`zad.yaml` verzinnen zou een tweede soort project opleveren dat in de portal verschijnt, in
lijsten staat en verwijderd kan worden.

Er zijn dus twee AANROEPERS, en die prijs is alleen te betalen op één voorwaarde: **één stuk
code dat accounts aanmaakt.** Dat is `MailManager.ensure_account`, een staticmethod juist
zodat de platformkant hem zonder project kan aanroepen. Maak je er een instantiemethode van,
dan heeft de platformkant een eigen implementatie nodig, en dan is het platformaccount het
account waar niemand naar kijkt.

### Twee geheimen, twee momenten

Ze ontstaan niet tegelijk, en dat bepaalt waar ze staan.

| | Wanneer | Waar |
|---|---|---|
| Het beheerderswachtwoord van de relay | bij het opzetten van de infrastructuur | gegenereerd in de gedeelde secret-generatie (`@secret-gen:random:24`), SOPS-versleuteld, gerenderd in **twee** namespaces |
| Het wachtwoord van het ZAD-account | pas nadat de relay draait, want OPI maakt het aan | door OPI gegenereerd en weggeschreven in een Secret in zijn **eigen** namespace (`zad-platform-mail-account`) |

Het tweede kan niet uit de bootstrap komen: op dat moment bestaat het nog niet. En het kan
geen omgevingsvariabele zijn: een pod leest zijn omgeving één keer bij het starten, dus een
waarde die OPI zelf later aanmaakt zou er alleen met een herstart in komen — een
opstartvolgorde die niemand meer kan volgen. Daarom een Secret, met deze eigenschappen:

- **Wie beheert hem**: OPI, en niemand anders. Er rendert niets in de bootstrap dat hem
  aanmaakt of overschrijft.
- **Als hij er nog niet is**: dat is de normale toestand van een cluster dat nog nooit een
  draaiende relay heeft gezien. OPI genereert dan een wachtwoord, schrijft de Secret
  **vóór** het de relay belt, en maakt daarna pas het account aan. Die volgorde is de
  veiligheid: een wachtwoord dat alleen op de relay staat sluit ZAD buiten zijn eigen
  account, terwijl een wachtwoord dat alleen in de Secret staat door de volgende opstart
  vanzelf wordt rechtgezet.
- **Als hij niet te lézen is**: dat is iets anders dan "hij is er niet", en het verschil is
  hier het hele punt. `get_secret` antwoordt `None` op een ontbrekende Secret én op elke
  mislukte kubectl-aanroep (geen rechten, API-server weg, timeout), en van een `None` maakt
  deze weg een nieuw wachtwoord. Eén onleesbaar moment zou dus de Secret overschrijven en
  ZAD uit zijn eigen account roteren. Daarom wordt de afwezigheid bevestigd
  (`KubectlConnector.secret_exists`, dat `NotFound` van een fout onderscheidt) en weigert
  OPI bij twijfel: de opstarttaak faalt dan zichtbaar en de volgende opstart leest de Secret
  gewoon terug.
- **Bij een tweede opstart**: het bewaarde wachtwoord wordt teruggelezen en ongewijzigd aan
  de relay gegeven. Er komt dus geen tweede account, en het bestaande wachtwoord wordt niet
  stilzwijgend vervangen door een nieuw dat nergens landt.
- **Na een rotatie**: verwijder de Secret en herstart OPI; de volgende opstart genereert een
  nieuw wachtwoord en zet dat op het bestaande account. Er is één plek, dus er kan geen oude
  kopie achterblijven.

### Het beheerdersgeheim staat in twee namespaces, uit één bron

De relay draait in een eigen namespace (`rig-ron`, op ODCN `rig-prd-ron` — die namespace
draagt de RON-annotatie), en OPI draait in `rig-system` / `rig-prd-operations`. Secrets
steken geen namespacegrens over, dus die twee moeten allebei het beheerdersgeheim hebben.

Dat gebeurt met **één bron en twee renderingen**: het geheim wordt één keer gegenereerd in
`infrastructure/bootstrap/infrastructure/secrets/templates/mail-relay-secret.yaml` en
SOPS-versleuteld weggeschreven in de namespace van OPI; de overlay van de mailcomponent
rendert exact datzelfde versleutelde bestand nog een keer, met de namespace van de relay
eroverheen (`decrypt-sops.yaml` in
`infrastructure/bootstrap/infrastructure/mail/controller/overlays/*/`).

Bewust géén leesrechten voor OPI op de secrets van de relay-namespace: rechten over een
namespacegrens zijn moeilijker terug te draaien dan een tweede rendering van hetzelfde
bestand. En omdat het één bestand is, kan een geroteerd beheerderswachtwoord niet in de ene
namespace landen en in de andere niet — dat zou een storing zijn die pas bij de volgende
accountaanmaak zichtbaar wordt. Wat bij een rotatie wél nodig is: **beide** pods opnieuw
starten, want allebei lezen ze de waarde uit hun omgeving.

Roteren raakt geen enkel projectaccount. De accounts staan als principals in de database van
de relay; het beheerderswachtwoord is alleen waarmee OPI zich meldt om ze te beheren.

### Als de relay er bij het opstarten nog niet is

De taak is non-critical en vangt ook de transportfouten (een onbereikbare relay geeft geen
HTTP-antwoord om een `MailRelayError` van te maken, maar de fout van aiohttp) en een
mislukte kubectl-schrijfactie. OPI boot dus door; er is alleen nog geen platformmail.

Er is met opzet geen achtergrondlus die erop blijft wachten: het account wordt bij elke
opstart opnieuw ingericht en is idempotent, en de relay aanzetten vraagt sowieso een
herstart van OPI (het krijgt dan pas `MAIL_RELAY_API_URL` en het beheerdersgeheim in zijn
omgeving). Dat herstartmoment is precies wanneer het account ontstaat.

## Instellingen

| Instelling | Betekenis |
|---|---|
| `MAIL_RELAY_API_URL` | de management-API van de relay; leeg betekent "geen relay op dit cluster" |
| `MAIL_RELAY_ADMIN_USERNAME` / `MAIL_RELAY_ADMIN_PASSWORD` | waarmee OPI accounts aanmaakt |
| `MAIL_PLATFORM_ACCOUNT` | de naam van het account van ZAD zelf (het wachtwoord is géén instelling, zie hierboven) |
| `MAIL_PLATFORM_SECRET_NAME` | de Secret in de eigen namespace waarin OPI dat wachtwoord bewaart |
| `MAIL_PLATFORM_FROM_LOCAL_PART` / `MAIL_PLATFORM_MESSAGES_PER_DAY` | afzender en budget van dat account |
| `MAIL_PROJECT_DEFAULT_MESSAGES_PER_DAY` | het budget van een project dat er zelf geen kiest |

Per cluster staan de relay-hostnaam, de poort, de namespace en het maildomein in
`opi/core/cluster_config.py`.

Is `MAIL_RELAY_API_URL` leeg, dan weigert de dienst te provisionen in plaats van
credentials uit te delen die nergens op uitkomen. Het platformaccount wordt dan stil
overgeslagen: een cluster zonder relay heeft eenvoudigweg nog geen platformmail, en dat mag
het opstarten niet tegenhouden.

## Wat er nog niet af is

**De relay draait nog nergens.** Stap 2 van de uitrol is nog open: de route naar
`rmrmail.rijksweb.nl` bestaat (de oude `Network is unreachable` is weg), maar op poort 25,
587 én 465 komt geen banner terug en geen weigering. Dat patroon wijst op een firewall die
stil laat vallen of op retourverkeer dat de weg terug niet vindt. De vraag die open staat —
welk bronadres uit `145.21.227.140/30` de tegenpartij werkelijk ziet, of dat adres in hun
toelating staat, en of het retourpad is ingericht — kunnen wij zelf niet beantwoorden: de
namespace `quattro-egress-gateway` is van ODCN.

Daarom staat de overlay in `infrastructure/bootstrap/clusters/*/kustomization.yaml` nog
uitgecommentarieerd. De manifesten zijn er en ze bouwen. Zolang die banner er niet is, zou
een relay draaien die niets kan bezorgen.

### Wat er bij het aanzetten werkelijk moet gebeuren

Het is niet één regel, en dat is belangrijker om op te schrijven dan om mooi te zeggen.

1. **De geheimen vullen en genereren**:
   `infrastructure/bootstrap/infrastructure/secrets/templates/mail-relay-secret.yaml` — de
   upstream-gegevens van het mailteam en de DKIM-sleutel er met de hand in (die zijn niet te
   genereren), daarna `task generate-secrets-for-cluster <cluster>` voor de rest.

   Twee dingen die je hier op je neus laten vallen:

   - **Twee waarden in dat sjabloon zijn per cluster, en het sjabloon is er één voor alle
     clusters.** `MAIL_DOMAIN` staat op de productiewaarde `mail.rijksapp.nl` en
     `MAIL_DB_HOST` op `rig-db-rw.rig-prd-operations`. Op `local` en `sandboxed-local` klopt
     geen van beide: `get_mail_domain` in `opi/core/cluster_config.py` zegt daar `mail.kind`
     respectievelijk `mail.sandbox.rijksapp.dev`, en de database draait in `rig-system`. Pas
     ze aan vóór het genereren — anders ondertekent de relay met een DKIM-sleutel voor het
     ene domein wat OPI als het andere aankondigt, en start hij sowieso niet op zonder
     database.
   - **`generate-secrets-for-cluster` doet niets als er al geheimen liggen.** De taak stopt
     met `exit 0` zodra er één `*.sops.yaml` in de clustermap staat ("To regenerate, delete
     the *.sops.yaml files in that directory first"). Op `odcn` staan die er allemaal al,
     dus deze stap levert daar géén `mail-relay-secret.sops.yaml` op en de fout uit zich pas
     bij stap 4 als een relay zonder inloggegevens. Genereer het bestand daar apart: vul het
     sjabloon, versleutel het met `sops --encrypt` naar
     `infrastructure/bootstrap/infrastructure/secrets/config/overlays/<cluster>/mail-relay-secret.sops.yaml`
     en zet het in de `kustomization.yaml` van die map. De bestaande geheimen van dat
     cluster weggooien om te kunnen hergenereren is géén optie: dan roteren Keycloak,
     PostgreSQL en MinIO mee.
2. **Overlay aanzetten**: de regel `- ../../infrastructure/mail/controller/overlays/<type>`
   in `infrastructure/bootstrap/clusters/<type>/kustomization.yaml` uit het commentaar
   halen. Daarmee komt het gegenereerde geheim ook in de namespace van de relay te staan
   (de tweede rendering hierboven).
3. **OPI de schakelaar geven.** Dit is de stap die je vergeet en die zich uit als "de dienst
   doet niets": `MAIL_RELAY_API_URL` staat uitgecommentarieerd in
   `bootstrap/rig-system/kustomize/operations-manager/overlays/<cluster>/patches/deployment.yaml`
   en moet aan. Het beheerdersgeheim staat er al bij (`optional: true` uit
   `mail-relay-credentials`), dus daar hoeft niets meer te gebeuren zodra stap 1 gedraaid is.
4. **Herstarten en de log lezen.** `ensure_platform_mail_account` draait bij het opstarten
   en zegt of het platformaccount klaarstaat; het maakt dan ook de Secret met zijn
   wachtwoord aan. Blijft `MAIL_RELAY_API_URL` leeg, dan slaat het die stap stil over —
   geen fout, maar ook geen mail.
5. **DNS**: SPF op ons maildomein dat de uitgaande IP's van de upstream autoriseert, en de
   publieke helft van de DKIM-sleutel als TXT op `zad._domainkey.<maildomein>`. Zonder deze
   twee vertrekt de post wel en komt hij niet aan.

Verder nog niet gebouwd, en bewust:

- **De maillog in ZAD.** MTA Hooks naar een OPI-endpoint, zodat een ontwikkelaar de
  directe weigeringen (limiet, te groot, verkeerde afzender) zelf ziet. Fase 1 van het plan.
- **Echte bounces.** Die zijn inkomende post; het plan kiest daarvoor een mailbox bij het
  mailteam die OPI over IMAP leegtrekt. Fase 2.

## Bestanden

| Wat | Waar |
|---|---|
| De dienst | `opi/services/catalog/send_email/` |
| De manager (accounts) | `opi/manager/mail_manager.py` |
| De connector (management-API) | `opi/connectors/mail.py` |
| Het account van ZAD | `ensure_platform_mail_account` in `opi/core/startup.py` |
| De relay | `infrastructure/bootstrap/infrastructure/mail/` |
| De geheimen | `infrastructure/bootstrap/infrastructure/secrets/templates/mail-relay-secret.yaml` |
| Tests | `tests/test_send_email_service.py` |
