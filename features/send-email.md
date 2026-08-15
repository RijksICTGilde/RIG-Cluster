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
| `from-domain` | een eigen afzenderdomein; leeg laten geeft het platformdomein |
| `messages-per-day` | het dagbudget van dit project, maximaal 5000 |

En wat je niet instelt: het domein achter de @ ligt vast op het maildomein van het platform
(`mail.rijksapp.nl` op productie — let op het enkelvoud, `rijksapps.nl` is de zone van
ODC-Noord zelf). Dat is geen betutteling maar techniek: een `From:` in een vreemd domein
haalt DMARC nooit, dus die post komt toch niet aan.

`from-domain` bestaat wel in het model en in de API, maar heeft bewust geen formulierveld:
een eigen domein kost eerst één DKIM-record in de zone van dat domein. Het is een aanvraag,
geen invulveld. Wat het goedkoop maakt is dat de envelope altijd op óns domein blijft —
SPF geldt voor het envelope-domein, dus een projectdomein kost één record in plaats van een
volledige set.

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

`approval` en `accounts` zijn platformdata: een beheerder beslist, en OPI maakt het account
aan op de relay en schrijft neer wat het gemaakt heeft (per cluster: gebruikersnaam, AGE-versleuteld wachtwoord, afzenderadres en
bounce-adres). Beide velden dragen `platform_managed_fields`, dus de API kan ze niet wissen
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

Daarom staat het platformaccount in de configuratie van de relay (`mail-relay-credentials`
in de namespace van de relay), niet in een projectbestand. OPI leest het als instelling en
richt het bij het opstarten in (`ensure_platform_mail_account` in `opi/core/startup.py`).
Een projectbestand `zad.yaml` verzinnen zou een tweede soort project opleveren dat in de
portal verschijnt, in lijsten staat en verwijderd kan worden.

Er zijn dus twee wegen naar een account, en die prijs is alleen te betalen op één
voorwaarde: **één stuk code dat accounts aanmaakt.** Dat is `MailManager.ensure_account`,
een staticmethod juist zodat de platformkant hem zonder project kan aanroepen. Maak je er
een instantiemethode van, dan heeft de platformkant een eigen implementatie nodig, en dan is
het platformaccount het account waar niemand naar kijkt.

## Instellingen

| Instelling | Betekenis |
|---|---|
| `MAIL_RELAY_API_URL` | de management-API van de relay; leeg betekent "geen relay op dit cluster" |
| `MAIL_RELAY_ADMIN_USERNAME` / `MAIL_RELAY_ADMIN_PASSWORD` | waarmee OPI accounts aanmaakt |
| `MAIL_PLATFORM_ACCOUNT` / `MAIL_PLATFORM_PASSWORD` | het account van ZAD zelf |
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
uitgecommentarieerd. De manifesten zijn er, ze bouwen, en aanzetten is één regel. Zolang die
banner er niet is, zou een relay draaien die niets kan bezorgen.

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
