# Mail: de vervolgpunten

**Geschreven op**: 21 augustus 2026, na het end-to-end werkend krijgen van de keten op de
sandbox. Elk punt is op zichzelf leesbaar: wat het is, waar het zit, wat het voorstel is
en welke beslissing er open staat.

**Wat er staat en bewezen is**: de relay (Stalwart) draait declaratief op de sandbox
(CNPG-database, geheimen uit de generatie, eigen ArgoCD-Application, config-rollout via
configMapGenerator-hash), de dienst `send-email` loopt de volle klantroute (aanvraag,
goedkeuring, componentkoppeling, netwerkbeleid, account, verzending) en de
identiteitsregels van RC-145 kloppen gemeten: `From: <from-name>
<noreply-rijksapp+<project>@rijksoverheid.nl>`, aangekomen in de Mailpit-sink
(https://mailsink.sandbox.rijksapp.dev). De e2e-image heeft een handmatige
testmailknop op de statuspagina.

## 1. Burst-limiter naast het dagbudget

**Wat**: het dagbudget (standaard 500, klant mag alleen verlagen) stopt geen uitbarsting:
500 berichten in een minuut passen erin, en dat is precies het patroon van een fout in
applicatiecode of een gekaapt account.

**Waar**: de relayconfiguratie (`infrastructure/.../mail/controller/base/config.toml`);
Stalwart kent throttles op de sessie- en wachtrijlaag.

**Voorstel**: een outbound throttle per account (orde: tientallen per minuut), gemeten
tegen een draaiende relay zoals bij de identiteitsregels is gedaan.

**Open**: de maat zelf, en punt 4 hieronder bepaalt of dit per project kan.

## 2. Het spamfilterbesluit

**Wat**: `spam-filter.auto-update` staat uit (de download blokkeerde de start), maar of
het filter zelf AAN hoort op een uitgaande relay is nooit besloten. Een uitgaande relay
filtert normaal niet op spam-score maar leunt op authenticatie plus limieten.

**Waar**: `config.toml`, blok `[spam-filter]`.

**Voorstel**: uit laten en dat expliciet opschrijven, tenzij iemand een concreet scenario
heeft waarin een geauthenticeerd project spam-achtige mail moet worden geweigerd in
plaats van gebudgetteerd.

**Open**: het besluit zelf.

## 3. `messages = 10` per SMTP-sessie

**Wat**: de sessielimiet staat op 10 berichten en levert nauwelijks bescherming: een
client opent gewoon een nieuwe sessie. Het hindert vooral legitieme bulk (een
nieuwsbrief in batches).

**Waar**: `config.toml`, sessielimieten.

**Voorstel**: verhogen naar een waarde die echte clients niet hindert (100+), zodra punt
1 de echte rem is.

## 4. Throttle op `sender` en het plusdeel

**Wat**: uitzoeken of een Stalwart-throttle met sleutel `sender` het volledige adres
neemt (met plusdeel, dus per project) of het kale adres. Bepaalt of afknijpen per project
op de relay zelf kan, of dat het per account moet.

**Waar**: meten tegen een draaiende relay, zoals de expressie-metingen van RC-145
(gedocumenteerd in `config.toml`).

## 5. Productie aanzetten

**Wat**: de manifesten en geheimen zijn klaar; wat rest zijn afspraken en twee regels
config. De stappen staan uitgewerkt in `features/send-email.md` ("Wat er bij het
aanzetten werkelijk moet gebeuren").

- De afspraak met het mailteam dat wij als `noreply-rijksapp(+project)@rijksoverheid.nl`
  versturen. Hun SPF autoriseert de upstream al; de afspraak is de poort. Let op: sinds
  RC-145 draagt de From: het plusdeel, neem dat mee in het gesprek.
- Een bounce-postbus die OPI via IMAP mag legen; zonder die is onbestelbare post
  onzichtbaar.
- De regel `- ../../infrastructure/mail/controller/overlays/odcn` uit het commentaar in
  `infrastructure/bootstrap/clusters/odcn/kustomization.yaml`, en `MAIL_RELAY_API_URL`
  aan in de OPI-overlay van odcn. De geheimen (`mail-relay-secret`,
  `mail-db-credentials`) staan sinds 20 augustus versleuteld in git; de database komt
  declaratief mee via `postgresql/database/base/`.

## 6. Webadmin, en wat er echt aan de hand was

**Wat**: de beheer-UI van Stalwart wordt bij de eerste start van GitHub gehaald; dat
hing stil (voor de logger-init) en is uitgezet met een niet-bestaand `file://`-resource.

**Rechtzetting van de analyse (21 augustus)**: GitHub is NIET onbereikbaar vanaf dit
netwerk. Gemeten vanuit de relay-pod zitten github.com en cdn.jsdelivr.net allebei
dicht: het is gewoon het egress-netwerkbeleid. Dat jsdelivr bij een podstart een keer
WEL lukte komt doordat de netpol-handhaving van Kind (kube-network-policies in kindnet)
asynchroon geprogrammeerd wordt: een fetch in de eerste seconde van een verse pod kan er
doorheen glippen. De oude regel in `config.toml` die dat race-artefact als feit
beschreef is op 21 augustus herschreven.

**Besluit tot nu toe**: webadmin blijft uit; OPI gebruikt de REST-API in de binary en
wijzigingen via webadmin zouden driften van de declaratieve config. Wil iemand hem ooit
toch, dan is het een beleidskeuze: een egress-uitzondering voor de download (simpel,
maar opent internet-egress vanaf een relay) of de bundel vendoren (meer werk, geen
egress). Plus een ingress en het besef dat het een tweede beheervlak is.

## 7. Klein spul

- **Mailpit-ingress voor `local`**: bestaat alleen in de sandboxed-local overlay
  (`ingress-mailsink.yaml`); de local-overlay heeft er geen.
- **Netpol-races bij metingen**: zie punt 6; een verbindingstoets in de eerste seconden
  van een podleven zegt niets over het beleid. Meet op een pod die al even draait.
- **Twee genoteerde kapotte dingen op de hoofdlijn** (stand 19 augustus, mogelijk
  inmiddels opgelost door de RC-merges): `test_template_structure.py::
  test_content_blocks_are_compositions` (over `bg/router.html.j2`) en `ruff format` op
  `opi/services/catalog/cross_domain_access/config_model.py`. Even verifieren en dan
  deze regel schrappen.

## Wat hier bewust NIET staat

De fundament-migratie van de sandbox (app-of-apps): eigen traject, zie
`docs/fundament-stand-van-zaken.md`. En de goedkeurings-UX rond "goedgekeurd maar aan
geen component gekoppeld": besproken op 20 augustus en bewust niet gebouwd; de
koppelvraag bij het aanvragen stellen is de minst ingrijpende variant als het toch gaat
knellen.
