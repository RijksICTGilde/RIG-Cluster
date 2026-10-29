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
die er al was, dezelfde die `publish-on-web` voor domeinen gebruikt. Geen eigen scherm en
geen tweede mechanisme — de aanvraag komt op `/admin/approvals` te staan, in een eigen groep
met de naam en het icoon van de dienst, naast de domeinaanvragen.

De vorm zelf ("mag dit project deze dienst gebruiken") staat één keer, als
`service_use_approval()` in `opi/services/catalog/approval.py`. Deze dienst declareert
alleen nog wát er wordt goedgekeurd en wat het betekent zolang dat niet gebeurd is:

```python
APPROVAL = service_use_approval(
    ServiceType.SEND_EMAIL,
    label="E-mail versturen",
    activity="Het versturen van e-mail",
    consequence="Er is nog geen SMTP-account, geen netwerktoegang naar de relay en geen SMTP_-variabelen in deze deployment.",
)
```

Dat levert de `ApprovalSpec` voor `config_approvals(ConfigLayer.PROJECT)`, de poort
`is_approved(project_data)` en het vastleggen van de aanvraag
(`ensure_approval_requests()`). De toestand staat op
`services/[send-email]/config/approval`, met een `status` en een `history`.

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

**Er is een tweede goedkeuring, en die staat los.** Deze gaat over het gebruik van de
dienst; vraagt een project daarnaast een eigen afzenderadres aan, dan is dat een aanvraag
over een WAARDE, met een eigen blok en een eigen oordeel (zie "Een eigen afzenderadres
aanvragen"). Een project mag mailen zonder eigen adres, en zonder deze eerste goedkeuring
gebeurt er sowieso niets.

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
| `SMTP_FROM` | het afzenderadres waaronder de post werkelijk vertrekt: je goedgekeurde eigen adres, of anders `noreply-rijksapp+<project>@rijksoverheid.nl` |

Elke variabele heeft ook een `APP_`-alias, net als bij de andere diensten.

## Wat je instelt

Op projectniveau, in de wizard of via de API:

| Veld | Betekenis |
|---|---|
| `from-name` | de naam die de ontvanger boven het bericht ziet |
| `from-address` | het e-mailadres van de afzender, na goedkeuring. Laat je het leeg, dan stelt het platform er een samen uit de naam van je project |
| `messages-per-day` | het dagbudget van dit project, dat de relay per account afdwingt. Tot en met 1000 kies je zelf; hoger is een aanvraag. Zie "Het dagbudget" hieronder |

Laat je `from-address` leeg, dan stelt het platform het adres samen uit de naam van je
project:

```
From:           <from-name> <noreply-rijksapp+<project>@rijksoverheid.nl>
Return-Path:    noreply-rijksapp+<project>@rijksoverheid.nl
X-ZAD-Project:  <project>
```

### Een eigen afzenderadres aanvragen

Vul `from-address` in en het opslaan IS de aanvraag; een platformbeheerder keurt hem goed
of af, langs dezelfde weg als een domeinaanvraag. Zolang dat niet gebeurd is, staat de
stand op de projectpagina en in de API, met daarbij het adres waaronder je post
ondertussen vertrekt. Na goedkeuring ziet een bericht er zo uit:

```
From:           <from-name> <noreply@rijksoverheid.nl>
Return-Path:    noreply-rijksapp+<project>@rijksoverheid.nl
X-ZAD-Project:  <project>
```

Drie dingen die daarbij horen:

- **De goedkeuring hangt aan de WAARDE.** Wijzig je een goedgekeurd adres, dan is dat een
  nieuwe aanvraag en vertrekt je post tot die is goedgekeurd weer onder het samengestelde
  adres. Maak je het veld leeg, dan trek je de aanvraag in.
- **De envelope blijft het samengestelde adres**, en dat is geen vergissing. DMARC
  vergelijkt de DOMEINEN van envelope en `From:`, en die blijven gelijk; het plusdeel in de
  envelope houdt ondertussen een bounce herleidbaar tot je project.
- **`X-ZAD-Project` noemt je project.** De relay zet die kopregel uit de LOGIN van het
  account, niet uit het bericht, en gooit een meegestuurde kopregel met die naam eerst weg.
  Zonder die kopregel zegt een bericht met een gedeeld adres niet meer van wie het komt.
  Post van het platform zelf heeft hem niet.

Het domein ligt vast op `rijksoverheid.nl`: alleen het deel voor de `@` is te kiezen
(hoogstens 64 tekens uit kleine letters, cijfers en `. _ + -`). Waarom het domein niet,
staat hieronder.

Er is geen controle op een adres dat een ander project al gebruikt. Twee projecten die
allebei `noreply@rijksoverheid.nl` voeren is het verwachte geval; wie een bericht
verstuurde lees je af aan de envelope en aan `X-ZAD-Project`.

De relay schrijft die hele `From:` zelf. Zet je applicatie er een eigen in, dan wordt die
weggegooid — adres én naam. Wat de ontvanger ziet komt dus uit `from-name` en nergens
anders; tot augustus 2026 bleef de naam van de applicatie staan, en die stond dan boven de
post van je project.

Overschrijven en niet weigeren, want vrijwel elke maillibrary zet standaard een `From:`. Een
550 op iets waar de ontwikkelaar niets aan kan doen is geen regel maar een val.

**`Reply-To:` blijft wel van jou** en wordt niet aangeraakt. Dat is de scheiding: de `From:`
is identiteit en wordt door het platform geschreven omdat wij op de mailserver van de
organisatie zitten, de `Reply-To:` zegt alleen waar een antwoord heen moet. Een `noreply`
in je `From:` met een eigen `Reply-To:` is het verwachte gebruik: op het afzenderadres komt
geen post binnen.

Waarom een domein dat niet van ons is: onze post gaat de deur uit via de mailserver van de
Rijksoverheid, dus hij draagt hun identiteit. Dat is ook de enige opzet die aankomt.
`rijksoverheid.nl` publiceert `p=reject`, en wij ondertekenen niet met DKIM omdat wij in die
zone geen sleutel kunnen publiceren. Daarmee is SPF-uitlijning tussen envelope en `From:` het
enige dat een bericht door DMARC krijgt, en die uitlijning bestaat alleen zolang beide in
`rijksoverheid.nl` zitten. Een eigen afzenderDOMEIN breekt precies dat, en dan komt er bij
elke ontvanger buiten de Rijksoverheid niets meer aan. Een eigen lokaal deel raakt de
uitlijning niet: het domein blijft hetzelfde, en de envelope houdt het plusdeel, dus een
bounce blijft herleidbaar.

Wat `from-name` mag zijn: geen regeleindes of andere stuurtekens, geen `@`, geen punthaken,
geen aanhalingstekens, backslash of dollarteken, en hoogstens 64 tekens. Dat is geen
willekeur — de naam gaat rechtstreeks een mailheader in, en een naam die op een adres lijkt
(`beveiliging@bank.nl`) wordt bij menig ontvanger als het afzenderadres gelezen. Een komma
of een punt mag wel; de relay zet aanhalingstekens om de naam heen, zodat die de `From:`
niet in tweeën knipt.

Laat je `from-name` weg, dan verstuurt je project met een kaal projectadres en zonder naam.
Dat is een geldige uitkomst, geen storing.

### Voorbeeld

Voor een project dat `algor-odc` heet:

```yaml
services:
  - name: send-email
    config:
      from-name: Algoritmeregister   # de ontvanger ziet
                                     # Algoritmeregister <noreply-rijksapp+algor-odc@rijksoverheid.nl>
      # from-address: noreply@rijksoverheid.nl   # pas na goedkeuring, zie hierboven
      messages-per-day: 750
      # accounts: door het platform geschreven, zie hieronder
```

## Wat het platform beheert

`approval`, `from-address-approval` en `accounts` zijn platformdata: een beheerder beslist,
en OPI maakt het account aan op de relay en schrijft neer wat het gemaakt heeft (per
cluster: gebruikersnaam, AGE-versleuteld wachtwoord, afzenderadres en bounce-adres). Alle
drie dragen `platform_managed_fields`, dus de API kan ze niet wissen en niet overschrijven,
en een PUT die ze weglaat verliest ze niet. Bij de twee goedkeuringsblokken is dat niet
alleen netjes maar de kern: een project dat zijn eigen status op `approved` kan zetten heeft
geen goedkeuring. `from-address-approval` draagt naast `status` en `history` ook de `value`
waar het oordeel over ging, en dat is wat een goedkeuring aan een adres bindt in plaats van
aan het project.

Het wachtwoord wordt één keer gegenereerd en meteen weggeschreven, om dezelfde reden als bij
het Keycloak-realm: het bestaat nergens anders, en een latere fout in dezelfde run zou een
account op de relay achterlaten waar geen projectbestand het wachtwoord van heeft.

## Het dagbudget

Standaard mag een project **1000 berichten per dag** versturen. Een lager getal kiest het
project zelf; een hoger getal is een aanvraag die een platformbeheerder goedkeurt, net als
het adres.

De goedkeuring hangt aan het GETAL: een goedgekeurde 20.000 die het project daarna verhoogt
naar 50.000 is een nieuwe aanvraag. Zolang die openstaat geldt het laatst goedgekeurde
getal, dus een aanvraag verlaagt nooit wat er al was. Verlagen gaat meteen. Een waarde die
al eens is goedgekeurd blijft goedgekeurd als het project er na een wijziging op terugkomt,
voor het getal en voor het adres.

Wijst een beheerder precies de waarde af die gold, dan is die goedkeuring daarmee
ingetrokken: het project valt terug op de standaard en terugkomen op die waarde is een
nieuwe aanvraag. Een afwijzing van een HOGER gevraagd getal raakt de staande goedkeuring
niet.

De relay dwingt het getal per account af, per bericht en niet per ontvanger.

### Uitrollen: eerst verwerken, dan is de limiter er

Het getal komt op de relay te staan als een limiter per account, en die schrijft OPI bij een
VERWERKING van het project. Er is geen periodieke reconciliatie. Omdat
`[queue.limiter.inbound.account]` met zijn vaste 5000 per dag uit `config.toml` is gehaald,
heeft een bestaand project na het uitrollen van die `config.toml` geen eigen limiet meer tot
het een keer is verwerkt; wat er dan nog begrenst is het vangnet
`[queue.limiter.inbound.platform]`, en dat is EEN emmer voor de hele relay.

Dus: laat na het uitrollen van `config.toml` elk project met `send-email` opnieuw verwerken,
voordat je de uitrol als klaar beschouwt. Pas dan heeft elk account zijn eigen limiter.

## Het account hangt aan het project

Eén account per project, niet per deployment. Daar hangen ook het dagbudget en het
bounce-adres aan, en een account per deployment zou één project meerdere budgetten geven en
een klacht herleidbaar maken naar een deployment die niemand buiten het platform kent.

Gevolg voor het opruimen: de dienst uit één deployment halen verwijdert het account niet
zolang een andere deployment van hetzelfde project hem nog gebruikt. Pas de laatste laat het
account opheffen.

### En het heet `project-<project>`, niet `<project>`

De relay heeft één platte naamruimte voor accounts: het account van ZAD zelf
(`MAIL_PLATFORM_ACCOUNT`, standaard `zad-platform`) staat ernaast. Zonder voorvoegsel is
`zad-platform` gewoon een geldige projectnaam, en dan kan dat project met goedkeuring het
wachtwoord en het afzenderadres van ZAD overnemen (een bestaand account wordt bijgewerkt,
niet geweigerd) en zonder goedkeuring het platformaccount laten verwijderen. Het
voorvoegsel maakt de twee verzamelingen disjunct, en de projectweg (`ensure_account`,
`_delete_account`) weigert de platformnaam daarnaast nog expliciet — ook als die naam uit
het projectbestand komt, en ook als iemand `MAIL_PLATFORM_ACCOUNT` juist ín de
projectnaamruimte zet.

## Het netwerkbeleid komt uit de dienst

De relay draait in een eigen namespace (`rig-prd-ron` op productie, `rig-ron` op de sandbox:
ODCN eist dat een namespace daar met de clusterprefix begint) en niet in de
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
`infrastructure/bootstrap/infrastructure/mail/controller/overlays/*/`). Hetzelfde geldt
voor `mail-db-credentials-secret.yaml`: dat geheim heeft CNPG als eerste lezer (de rol
`mailrelay` en de database in
`infrastructure/bootstrap/infrastructure/postgresql/database/base/` komen eruit voort) en
de relay als tweede, via dezelfde tweede rendering.

Bewust géén leesrechten voor OPI op de secrets van de relay-namespace: rechten over een
namespacegrens zijn moeilijker terug te draaien dan een tweede rendering van hetzelfde
bestand. En omdat het één bestand is, kan een geroteerd beheerderswachtwoord niet in de ene
namespace landen en in de andere niet — dat zou een storing zijn die pas bij de volgende
accountaanmaak zichtbaar wordt. Wat bij een rotatie wél nodig is: **beide** pods opnieuw
starten, want allebei lezen ze de waarde uit hun omgeving.

Roteren raakt geen enkel projectaccount. De accounts staan als principals in de database van
de relay; het beheerderswachtwoord is alleen waarmee OPI zich meldt om ze te beheren.

### En het account van KEYCLOAK, dat er niet op lijkt

Er is sinds RC-159 een TWEEDE platformaccount: `zad-keycloak`, waarmee Keycloak zijn
inlogpost en bevestigingsmail verstuurt. Het is opzettelijk geen variant van het account
hierboven, want het verschilt in het enige dat telt: **waar het wachtwoord vandaan komt.**

`zad-platform` wordt door niemand anders gelezen, dus OPI genereert dat wachtwoord zelf.
Keycloak is een ander programma dat OPI niet kent en niet op OPI hoort te wachten, dus zijn
wachtwoord komt uit de BOOTSTRAP en OPI is daar de reconciler in plaats van de bron. De regel
die de twee uit elkaar houdt staat in `mail-relay-secret.yaml` zelf.

De hele keten - het geheim, de eigen verzender in de Keycloak-pod, de minimale `smtpServer`
op elke realm, het eigen afzenderadres en wat `verifyEmail` nog wel en niet meer bepaalt -
staat in **`features/keycloak-mail.md`**.

### Als de relay er bij het opstarten nog niet is

De taak is non-critical en vangt ook de transportfouten (een onbereikbare relay komt sinds
RC-261 als `MailRelayUnavailableError` uit de connector, maar de kubectl-aanroep en het
ontsleutelen eromheen gooien nog steeds hun eigen fout) en een mislukte
kubectl-schrijfactie. OPI boot dus door; er is alleen nog geen platformmail.

**En hij blijft het proberen** (RC-261). Beide accounts zitten in de bestaande
opstart-herhaallus van OPI (`_startup_retry_loop`, elke 60 seconden), naast database,
projects, keycloak en oauth. Dat was er niet, en bij een verse uitrol is de relay er
meestal nog niet als OPI opstart: `zad-platform` en `zad-keycloak` ontstonden dan pas bij
de VOLGENDE herstart van OPI, en tot die tijd ging er geen platformmail en geen inlogmail
uit. Een cluster zonder relay, of zonder het Keycloak-geheim, heeft niets in te richten en
laat de lus met rust.

De accounts staan met opzet NIET in `ReadinessState`: dat bepaalt of het portaal zijn
onderhoudspagina toont, en zonder platformmail hoort het portaal gewoon te werken.

## Instellingen

| Instelling | Betekenis |
|---|---|
| `MAIL_RELAY_API_URL` | de management-API van de relay; leeg betekent "geen relay op dit cluster" |
| `MAIL_RELAY_ADMIN_USERNAME` / `MAIL_RELAY_ADMIN_PASSWORD` | waarmee OPI accounts aanmaakt |
| `MAIL_PLATFORM_ACCOUNT` | de naam van het account van ZAD zelf (het wachtwoord is géén instelling, zie hierboven) |
| `MAIL_PLATFORM_SECRET_NAME` | de Secret in de eigen namespace waarin OPI dat wachtwoord bewaart |
| `MAIL_PLATFORM_MESSAGES_PER_DAY` | het budget van dat account (de afzender is geen instelling: dit account hoort bij geen project en verstuurt daarom als het kale basisadres, zonder plusdeel en zonder naam) |
| `MAIL_PROJECT_DEFAULT_MESSAGES_PER_DAY` | het budget van een project dat er zelf geen kiest (1000), en de grens tussen zelf kiezen en aanvragen |
| `MAIL_KEYCLOAK_ACCOUNT` | de naam van het account waarmee Keycloak verstuurt (zie `features/keycloak-mail.md`) |
| `MAIL_KEYCLOAK_SECRET_NAME` | het Secret uit de BOOTSTRAP waaruit OPI dat wachtwoord leest - het genereert het hier niet |
| `MAIL_KEYCLOAK_SECRET_KEY` | de sleutel in dat Secret, en de naam die het bootstraptemplate, de `secretKeyRef` van de Keycloak-deployment en de leesweg van OPI gemeen hebben |
| `MAIL_KEYCLOAK_MESSAGES_PER_DAY` | het budget van dat account, gedeeld door alle realms |
| `MAIL_KEYCLOAK_FROM_NAME` | de weergavenaam van dat account. Er is geen eigen ADRES meer: inlogpost vertrekt onder het kale basisadres van het cluster, net als de post van de portal, en onderscheidt zich alleen nog door deze naam |
| `MAIL_RELAY_CHECK_INTERVAL_SECONDS` | hoe vaak de watcher `GET /healthz/ready` op de relay doet (60) |
| `MAIL_RELAY_DOWN_ALERT_SECONDS` | hoe lang de relay niet ready mag zijn voor het ntfy-bericht volgt (300) |

Per cluster staan de relay-hostnaam, de poort, de namespace en het BASISadres in
`opi/core/cluster_config.py` (`get_mail_from_address`). Het adres dat een project
daadwerkelijk gebruikt wordt daaruit samengesteld met het project in het plusdeel; dat
gebeurt op één plek, in `MailManager._sender_address`.

Is `MAIL_RELAY_API_URL` leeg, dan weigert de dienst te provisionen in plaats van
credentials uit te delen die nergens op uitkomen. Het platformaccount wordt dan stil
overgeslagen: een cluster zonder relay heeft eenvoudigweg nog geen platformmail, en dat mag
het opstarten niet tegenhouden.

## De upstream: een klein record, en waar de tweede vandaan komt

Een upstream is niet meer dan **host, poort, TLS, en eventueel inloggegevens**. Dat is de hele abstractie, en zodra je hem zo bekijkt vallen alle omgevingen samen:

| Waar | Wat het werkelijk is |
|---|---|
| Productie | `rmrmail.rijksweb.nl:25` over RON, geen auth (hun toelating staat op ons uitgaande IP) |
| Sandbox | de SMTP-sink `rig-mail-sink:25` in dezelfde namespace, geen auth |
| Een tweede cluster, later | het chisel-eindpunt naar de relay op het hoofdcluster, **wél** auth, want onze eigen submissiepoort eist die |

Vandaag varieert daarvan alleen de host, en die staat per cluster in `MAIL_UPSTREAM_HOST` in het geheim. De poort staat vast op 25 en dat is geen toeval: de Service van de sink vertaalt 25 naar 1025, zodat de sink als niet-root kan draaien zonder dat de poort een instelling per cluster hoeft te worden. Een numerieke instelling via een omgevingsvariabele is hier precies de knop die je niet wilt, want bij een verkeerd type valt Stalwart stil terug op zijn standaard 25 en dan werkt productie per ongeluk wel en de sandbox stil niet.

### Wat er moet gebeuren als er een tweede upstream komt

Niets nieuws, en dat is met opzet zo gelaten. Stalwart spreekt deze taal al:

- **Meerdere upstreams** zijn meerdere `[remote.<naam>]`-blokken in de configmap van dát cluster. Auth komt daar terug als een optioneel `[remote.<naam>.auth]`-blok. Dat blok stond er ooit en is eruit gehaald toen bleek dat de Rijksoverheid-server niet authenticeert; het hoort optioneel per upstream te zijn, niet globaal afwezig.
- **De keuze** is een expressie. `next-hop` is bij Stalwart geen vaste waarde maar een expressie (vandaar de enkele quotes in `"'upstream'"`), dus kiezen op basis van het geauthenticeerde account is één regel: een expressie op `authenticated_as`.
- **De catalogus hoort op één plek**, en dat is de configmap van de relay: daar staan de hosts en daar hangen de geheimen aan. Zet hem niet ook in OPI; dan heb je twee bronnen voor dezelfde host en poort.

Eén ding zou ik daarbij anders doen dan bij de Keycloak-blueprints, en dat is de les uit die hoek. Daar staat de keuzelijst hardgecodeerd in `KeycloakTemplateOptionsProvider` en is hij voor elk cluster gelijk; de clusterafhankelijkheid zit ergens anders, in welk bestand de bootstrap laadt. Kopieer je dat hier, dan krijgt een project in de sandbox een upstream aangeboden die daar niet draait, en dat merk je pas bij het versturen. **Een keuzelijst voor upstreams hoort afgeleid te worden van wat er op dit cluster staat.**

Tot die tweede upstream er is, kiest het cluster en niet de service. Elk scenario dat we vandaag kennen (sandbox, tweede cluster) is er een per cluster.

## De sandbox: een sink in plaats van een echte mailserver

`infrastructure/bootstrap/infrastructure/mail/sink/` bevat Mailpit, ingeladen door alleen de overlay `sandboxed-local`. Op ODCN staat hij er niet, dus dat image hoeft niet langs de registry en het signature-beleid daar. Ingeladen wordt hij via de component `sink/as-upstream` en niet via `sink/base` rechtstreeks: die component levert de sink samen met `MAIL_UPSTREAM_ALLOW_INVALID_CERTS: "true"`, want de sink draagt een zelfondertekend certificaat en zonder die schakelaar blijft elke bezorging steken op `invalid peer certificate`. Zo kan een cluster de sink niet krijgen zonder de schakelaar.

Hij doet precies twee dingen: hij neemt SMTP aan en bezorgt niets, en hij geeft elk bericht integraal terug via een HTTP-API (`/api/v1/messages`, `/api/v1/message/{id}` met `From` en `ReturnPath`, en `/api/v1/message/{id}/headers`). Niets wordt bewaard, want er is geen `MP_DATABASE` gezet: een herstart is de manier om schoon te beginnen.

Daarmee is de identiteitscontrole een assertie geworden in plaats van een handmatige proef. `scripts/mail_identity_check.py` stuurt drie berichten met een expres foute `From:` en een expres foute envelope, en toetst wat er aan de andere kant uitkomt:

```bash
kubectl -n rig-ron port-forward svc/rig-mail-relay 1587:587 &
kubectl -n rig-ron port-forward svc/rig-mail-sink 8025:8025 &
cd operations-manager/python
MAIL_RELAY_PASSWORD=<geheim> uv run python scripts/mail_identity_check.py --user <account>
```

Het wachtwoord komt uit `MAIL_RELAY_PASSWORD` en anders uit een prompt; er is met opzet geen `--password`. Een wachtwoord op de opdrachtregel staat in `/proc/<pid>/cmdline` voor iedereen op de machine en blijft in de shellgeschiedenis staan, en dit script wordt gedraaid met een GEDEELD platformwachtwoord in de hand.

De `From:` en de envelope komen langs twee wegen tot stand — de relay leidt de envelope af uit de accountnaam, het sieve-script overschrijft alleen de `From:` — dus de toets is domeinuitlijning (dat is wat DMARC vergelijkt) plus het adres dat je opgeeft:

```bash
MAIL_RELAY_PASSWORD=<geheim> uv run python scripts/mail_identity_check.py --user zad-keycloak \
    --verwacht-adres noreply-rijksapp@rijksoverheid.nl \
    --verwacht-naam "Rijksapps"
```

RC-159 gaf `zad-keycloak` een eigen lokaal deel in de `From:`, waardoor die twee wegen uiteenliepen en het script een `--verwacht-envelope` nodig had. Sinds RC-175 vertrekt inlogpost onder het kale adres en vallen ze weer samen; die vlag is eruit.

De eerste sandbox-run beantwoordde en passant een vraag die openstond: de relay praat STARTTLS met strikte certificaatcontrole, en nergens stond wat Stalwart doet als de tegenpartij STARTTLS niet aanbiedt. **Gemeten op 19 augustus 2026: het is een garantie.** Zonder STARTTLS weigert hij permanent (`STARTTLS was not advertised by host`) en bouncet het bericht; met een certificaat dat niet valideert weigert hij tijdelijk en blijft het bericht in de wachtrij. Hij valt in geen van beide gevallen terug op platte tekst. Daar hoort een tweede uitkomst bij die productie raakt: Stalwart leest de trust store van het besturingssysteem niet, dus een interne CA valt niet te vertrouwen. Beide staan uitgewerkt in `docs/ron-koppeling.md`. De sink biedt STARTTLS daarom nu wel aan, met een certificaat dat een initContainer bij elke start maakt.

Het netwerkbeleid van de relay staat in de dev-overlays op precies deze sink dichtgezet. Daar stond eerst `0.0.0.0/0` met het argument dat niet vastligt welke testupstream een ontwikkelaar gebruikt; nu er een sink meekomt ligt het wel vast, en een relay die overal heen mag is een open relay zodra iemand een account bemachtigt.

## Wat er nog niet af is

**De relay draait op de sandbox en op productie** (`rig-mail-relay` in `rig-prd-ron`). Op
19 augustus 2026 liep er een bericht doorheen en eindigde `scripts/mail_identity_check.py`
met exitcode 0: alle vier de
identiteitsregels doen wat ze beloven. De weg naar de upstream is los daarvan bewezen. Op 17 augustus
2026 gemeten vanuit een pod met `rig-ron`-egress: `rmrmail.rijksweb.nl` antwoordt op poort 25
met een banner en neemt een testbericht aan. De eerdere meting, waarin alle drie de poorten
stil bleven, liep door het baseline-netwerkbeleid van de tenant dat egress alleen op 443 en 80
toestaat; die pakketten hadden de pod nooit verlaten. Zie `docs/ron-koppeling.md`.

Wat dat opleverde en wat hier verwerkt is: poort 25 en niet 587, geen authenticatie maar een
toelating op ons uitgaande IP, STARTTLS, en 30 MB als grens aan hun kant.

De relay draait op elk cluster via zijn EIGEN Application (`ron-infrastructure`) en niet
via `infrastructure/bootstrap/clusters/*/kustomization.yaml`; de reden staat bij stap 2b
hieronder.

### Wat er bij het aanzetten moet gebeuren

Het is niet één regel, en dat is belangrijker om op te schrijven dan om mooi te zeggen. Op
sandbox en odcn-production is dit gedaan; dit is de lijst voor een volgend cluster, en de
plek om te kijken als de dienst niets doet.

1. **De geheimen genereren**: `task select-cluster` en dan
   `task generate-infrastructure-secrets-for-cluster` (die leest de keuze uit
   `CLUSTER_TYPE`). Er zijn er
   twee (`mail-relay-secret.yaml` en `mail-db-credentials-secret.yaml` in
   `infrastructure/bootstrap/infrastructure/secrets/templates/`) en er is niets met de
   hand in te vullen: de sjablonen zijn cluster-agnostisch. De per-cluster-waarden
   (`MAIL_UPSTREAM_HOST`, `MAIL_DB_HOST`) zijn geen geheimen en staan in het Deployment
   van de relay; de basis draagt de productiewaarden en de overlays en de sink-component
   zetten ze om. De generatietaak slaat bestaande geheimen per bestand over, dus op een
   cluster dat al draait komen alleen de twee nieuwe mailgeheimen erbij en roteert er
   niets. Voor `odcn` staan beide al versleuteld in git
   (`infrastructure/bootstrap/infrastructure/secrets/config/overlays/odcn/`).

   De database zelf hoeft nergens te worden aangemaakt: de rol `mailrelay` (met zijn
   wachtwoord uit `mail-db-credentials`) en de database staan declaratief in
   `infrastructure/bootstrap/infrastructure/postgresql/database/base/`, dus CNPG maakt
   ze ook op een al draaiend cluster aan zodra de wijziging synct.
2. **De RON-namespace komt uit de bootstrap (ODCN).** De ArgoCD op ODCN draait in
   namespaced mode: hij kan geen Namespace-resource aanmaken, en de CMP weigert zelfs te
   renderen zolang de doelnamespace of het sleutelsecret ontbreekt. De namespace staat
   daarom als `namespace-ron.yaml` in de bootstrap-overlay
   (`bootstrap/rig-system/kustomize/overlays/odcn-production/`), met de RON-annotatie
   (`egress.projectcalico.org/egressGatewayPolicy: rig-ron`) en het
   `argocd.argoproj.io/managed-by`-label erop; `task bootstrap-argo-system CLUSTER_TYPE=odcn-production` past hem met
   kubectl toe, buiten ArgoCD om.

   **Diezelfde taak zet sinds RC-261 ook het sleutelsecret in die namespace**: de CMP
   leest `sops-age-key` in de namespace van de app, dus zonder hem rendert
   `ron-infrastructure` niet en komt de relay er niet. Dat was handwerk en ontbrak dus op
   elk cluster waar niemand die stap deed. De taak heeft de sleutel al in de hand
   (`KEY_FILE`) en maakt het secret alleen aan als het er nog NIET is: een apply met de
   lokale sleutel zou op een cluster met een andere platformsleutel een rotatie zijn, en
   die loopt langs `scripts/set-sops-key-secret.py` (zie
   `instructions/sops-sleutel-in-het-cluster.md`).

2b. **De eigen Application**: `- argocd-application-ron-infrastructure.yaml` in
   `bootstrap/rig-system/kustomize/overlays/odcn-production/kustomization.yaml`, en dan de
   bootstrap toepassen. NIET de mail-regel in
   `infrastructure/bootstrap/clusters/odcn/kustomization.yaml`: de CMP injecteert de
   destination-namespace over die hele build, en dat slaat de rig-prd-ron-resources plat
   naar rig-prd-operations (het ID-conflict dat de sandbox op 19 augustus 2026 heeft
   gemeten; daarom heeft mail op elk cluster een eigen Application). Daarmee komt ook
   het gegenereerde geheim in de namespace van de relay te staan (de tweede rendering
   hierboven).
3. **OPI de schakelaar geven.** Dit is de stap die je vergeet en die zich uit als "de dienst
   doet niets": `MAIL_RELAY_API_URL` in
   `bootstrap/rig-system/kustomize/operations-manager/overlays/<cluster>/patches/deployment.yaml`.
   Op sandboxed-local en odcn-production staat hij AAN en wijst hij naar
   `rig-mail-relay.<ron-namespace>.svc.cluster.local:8080`. Het beheerdersgeheim staat er
   al bij (`optional: true` uit `mail-relay-credentials`), dus daar hoeft niets meer te
   gebeuren zodra stap 1 gedraaid is.
4. **De log lezen.** `ensure_platform_mail_account` draait bij het opstarten en zegt of
   het platformaccount klaarstaat; het maakt dan ook de Secret met zijn wachtwoord aan. Is
   de relay er nog niet, dan probeert de opstart-herhaallus het elke 60 seconden opnieuw
   (zie "Als de relay er bij het opstarten nog niet is"), dus een herstart van OPI is
   hiervoor niet meer nodig. Blijft `MAIL_RELAY_API_URL` leeg, dan slaat het die stap stil
   over: geen fout, maar ook geen mail.
5. **DNS**: niets. Dit was de langste post op deze lijst en hij is vervallen. Het
   afzenderdomein is `rijksoverheid.nl`, en dat autoriseert de uitgaande IP's van de
   upstream al via `v=spf1 redirect=spf-a.ssonet.nl` — de upstream ís hun eigen
   mailinfrastructuur. Wij hebben geen zone om iets in te zetten en hoeven dat ook niet.
   Wat daar tegenover staat: er is geen tweede been. Wij ondertekenen niet met DKIM, dus
   SPF-uitlijning tussen envelope en `From:` is het enige dat een bericht door DMARC krijgt.
   Gaat de envelope-herschrijving ooit stuk, dan weigert elke ontvanger buiten de
   Rijksoverheid alles.

Twee dingen die bij het naspelen tegen een echte Stalwart v0.11.8 stuk bleken en nu goed
staan — ze horen hier omdat ze allebei pas bij het aanzetten zichtbaar zouden worden:

- **Het image negeert `args`.** `/usr/local/bin/entrypoint.sh` draait `--init` als
  `/opt/stalwart-mail/etc/config.toml` ontbreekt en start daarna altijd met dát bestand,
  wat je ook meegeeft. Met alleen `args: ["--config", "/etc/mail/config.toml"]` draaide de
  relay dus op een zelf gegenereerde standaardconfiguratie: geen identiteitsregels, geen
  upstream, geen limieten. Het deployment zet daarom `command:
  ["/usr/local/bin/stalwart-mail"]`.
- **De relay moet het maildomein kennen vóór het eerste account.** Een principal met een
  adres in een onbekend domein wordt geweigerd met status 200 en
  `{"error":"notFound","item":"<domein>"}` — dus het allereerste projectaccount mislukte,
  met een fout die het domein noemt en het account niet. `MailConnector.ensure_domain`
  maakt het domein aan (idempotent) en `ensure_account` roept het aan voor het afzender- en
  het bounce-adres.

En twee dingen die de relay vandaag niet dichtzet, met de reden erbij:

- **De management-API loopt over plain HTTP met Basic auth** binnen het cluster, dus het
  beheerderswachtwoord gaat base64 over het podnetwerk. Wat het inperkt is het
  NetworkPolicy: alleen de OPI-namespace mag poort 8080 aan. Echt dicht vraagt een
  certificaat op de listener.
- **Submission heeft geen TLS** terwijl er PLAIN/LOGIN overheen gaat. Zelfde inperking en
  hetzelfde certificaat lost beide op.

Verder nog niet gebouwd, en bewust:

- **De maillog in ZAD.** MTA Hooks naar een OPI-endpoint, zodat een ontwikkelaar de
  directe weigeringen (limiet, te groot, verkeerde afzender) zelf ziet. Fase 1 van het plan.
- **Echte bounces.** Die zijn inkomende post; het plan kiest daarvoor een mailbox bij het
  mailteam die OPI over IMAP leegtrekt. Fase 2.

## Storingen

### De relay staat in CrashLoopBackOff en de log zegt `security.ip-blocked`

**Wat je ziet.** De pod `rig-mail-relay` komt niet ready, elke probe krijgt een reset, en
elke uitrol van een project met send-email faalt op
`Cannot connect to host rig-mail-relay.rig-prd-ron.svc.cluster.local:8080`. In de log van
de relay staat `Banned due to scan (security.scan-ban)` met een `remoteIp` dat een
NODE-adres is, en daarna `Blocked IP address (security.ip-blocked)`.

**Wat er aan de hand is.** Stalwart v0.11.8 telt een HTTP-verbinding die sluit voordat het
verzoek compleet is als scan (`reason = "connection closed before message completed"`), en
na 30 per dag (`server.auto-ban.scan.rate`, standaard `30/1d`) verbant hij het IP. De
probes van de kubelet komen van het node-adres en hebben de default `timeoutSeconds: 1`,
dus een trage relay laat ze afbreken: op 13 oktober 2026 verbande de relay op productie
daarmee de kubelet, en lag hij twee uur plat.

Sinds RC-261 staat `[server.allowed-ip]` met `10.0.0.0/8`, `172.16.0.0/12` en
`192.168.0.0/16` in de relayconfiguratie, en al het verkeer naar de relay is intern. Een
nieuwe ban kan dus niet meer ontstaan. `scripts/mail_autoban_check.py` toetst dat tegen een
draaiende relay.

**Een BESTAANDE ban heft die regel niet op.** Gemeten tegen v0.11.8, en het is ook wat de
accept-lus in `listener/listen.rs` doet: die kijkt alleen naar de blocked-lijst. Die lijst
staat in de configopslag (tabel `s` in de database `mailrelay` op `rig-db`) en overleeft
een herstart, dus de relay herstelt zich niet zelf.

**Herstel met de hand.** Het IP uit de logregel, de eigen rol `mailrelay` (niet `postgres`), en dit patroon: wachtwoord via stdin (niet argv, want dat staat in het auditlog van de API-server en van elke proxy ertussen), en TCP (niet socket, want de pod UID faalt in peer-auth).

```bash
# Het verbannen adres opzoeken
kubectl -n rig-prd-ron get secret mail-db-credentials -o jsonpath='{.data.password}' | base64 -d \
  | kubectl -n rig-prd-operations exec -i rig-db-1 -c postgres -- sh -c \
    'PGPASSWORD=$(cat) exec psql -h localhost -U mailrelay -d mailrelay -c "SELECT convert_from(k, '\''UTF8'\'') FROM s WHERE convert_from(k, '\''UTF8'\'') LIKE '\''server.blocked-ip%'\'';"'

# En weghalen
kubectl -n rig-prd-ron get secret mail-db-credentials -o jsonpath='{.data.password}' | base64 -d \
  | kubectl -n rig-prd-operations exec -i rig-db-1 -c postgres -- sh -c \
    'PGPASSWORD=$(cat) exec psql -h localhost -U mailrelay -d mailrelay -c "DELETE FROM s WHERE k = '\''server.blocked-ip.<ip>'\''::bytea;"'

# De relay leest de lijst bij het starten, dus de pod moet eraf
kubectl -n rig-prd-ron delete pod -l app=rig-mail-relay
```

`PGPASSWORD=$(cat)` en geen `read`: `base64 -d` levert het wachtwoord zonder newline, en `read` geeft dan exitcode 1 en breekt een `&&`-keten.

### Een uitrol faalt op de mailrelay

Sinds RC-261 zegt de melding dat zelf: `De mailservice van het platform is niet
bereikbaar, dit ligt niet aan je project (...)`. Op vier taaksoorten staat daarnaast
`error_category: InternalError` op de gefaalde taak (`Fault.PLATFORM`, exit 2 in de
zad-cli): `create_project`, `refresh_project`, `refresh_deployment` en `delete_component`.
Daarvoor kwam dit aan als `processing_failed` zonder categorie, en dus als "niet toe te
schrijven" op een moment waarop er niets aan het project te repareren was.

Op de overige schrijfpaden gebeurt dat niet, met `upsert_deployment` vooraan: die melden
een mislukte uitrol zonder `error_type` en krijgen daarom helemaal geen categorie, ook niet
`Unknown`, want `_with_error_category` slaat een resultaat zonder `error_type` over. Dat
gat is breder dan mail en staat als eigen taak op PR #236.

Een podwissel van de relay kost geen uitrol meer: de connector herhaalt een connectfout
30 seconden lang (`opi/utils/connect_retry.py`, dezelfde lus als de ArgoCD-connector).

### Niemand merkt het als de relay plat ligt

Dat was de werkelijke les van 13 oktober 2026, en OPI meet het sinds RC-261 zelf: elke
`MAIL_RELAY_CHECK_INTERVAL_SECONDS` (60) een `GET /healthz/ready` op de relay, en is die
langer dan `MAIL_RELAY_DOWN_ALERT_SECONDS` (300) niet ready, dan gaat er EEN bericht naar
het ntfy-topic van de log_watcher, en EEN bij herstel. Geen melding per tik: een kanaal dat
elke minuut afgaat is na een uur onbruikbaar.

Er is in deze repo geen Alertmanager-routering, dus een PrometheusRule zou mogelijk in het
niets vuren; ntfy is het enige kanaal waarvan bewezen is dat het aankomt
(`features/log-watcher.md`). Zonder `MAIL_RELAY_API_URL` of zonder `LOGWATCHER_NTFY_TOPIC`
doet de check niets.

## Bestanden

| Wat | Waar |
|---|---|
| De dienst | `opi/services/catalog/send_email/` |
| De manager (accounts) | `opi/manager/mail_manager.py` |
| De connector (management-API) | `opi/connectors/mail.py` |
| Het account van ZAD | `ensure_platform_mail_account` in `opi/core/startup.py` |
| De relay | `infrastructure/bootstrap/infrastructure/mail/` |
| De geheimen | `infrastructure/bootstrap/infrastructure/secrets/templates/mail-relay-secret.yaml` en `mail-db-credentials-secret.yaml` |
| De database (rol + Database, declaratief) | `infrastructure/bootstrap/infrastructure/postgresql/database/base/` |
| De readiness-wacht + melding | `opi/core/mail_relay_watcher.py` |
| De proef op de auto-ban | `scripts/mail_autoban_check.py` |
| Tests | `tests/test_send_email_service.py`, `tests/test_mail_relay_watcher.py` |
