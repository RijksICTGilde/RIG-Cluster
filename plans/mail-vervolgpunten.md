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

## 2. Het spamfilter aanzetten

**Wat**: het filter HOORT aan te staan en staat uit. Dat is nooit een besluit geweest maar
een blokkade: de regelset wordt bij elke start van github gehaald, dat kan niet vanuit deze
namespace, dus er zijn simpelweg geen regels. Een eerdere versie van dit punt stelde voor
het "uit te laten en dat expliciet op te schrijven". Dat was een redenering achter een
storing aan, en is hierbij ingetrokken.

**Waar**: `config.toml`, blok `[spam-filter]`. Daar staat sinds 21 augustus 2026
`resource = "file:///nonexistent/..."` om de starthang van 60 seconden te doden die de
relay op productie in een crashloop hield (de analyse staat in het commentaar bij dat
blok). Dat is de noodstop, niet de oplossing.

**Voorstel**: het bestand VENDOREN. `spam-filter.toml` erbij in de base als tweede bestand
in dezelfde configMapGenerator, een eigen mount, en `resource` daarop richten met
`file://`. Dan leest Stalwart de regels van schijf in plaats van van internet, laadt hij ze
bij de start in de configopslag, en blijft de start instantaan.

**Gemeten, zodat de volgende hier niet opnieuw naar hoeft te kijken**: de `spam-filter.toml`
van de laatste release is 76 KB en past dus ruim binnen de 1 MiB van een ConfigMap. Hij
draagt `version.spam-filter = "2.0.5"` en `version.server = "0.11.0"`, en
`fetch_spam_rules()` weigert alleen als de vereiste serverversie HOGER is dan de draaiende,
dus v0.11.8 voldoet.

**Open, en dit is het echte werk**: het regelbestand brengt sleutels mee die zelf naar
buiten willen. Het opent met `[asn] type = "resource"` met jsdelivr-URL's, terwijl onze
eigen config `type = "disable"` zet - uitzoeken welke van de twee wint zodra de externe
sleutels in de configopslag landen. Verder zitten er `http-lookup.`- en `lookup.`-sleutels
in en regels die om verkeer vragen dat deze namespace niet heeft. Vendoren lost de
STARTHANG op; welke regels daarna nog stil niets doen omdat ze niet naar buiten kunnen,
moet per stuk worden nagelopen tegen een draaiende relay, zoals bij de identiteitsregels
van RC-145 is gedaan.

**Ook open**: of een uitgaande relay op spam-score hoort te filteren is een aparte vraag
dan of het filter kan draaien. Die vraag mag gesteld worden, maar niet meer als reden om
het uit te laten staan.

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
- De namespace `rig-prd-ron` komt uit de bootstrap (`namespace-ron.yaml` in de
  odcn-production bootstrap-overlay, met de RON-annotatie en het managed-by-label;
  ArgoCD op ODCN is namespaced en kan dat niet zelf): `task bootstrap-argo-system`
  draaien. Daarna het `sops-age-key`-secret erin kopieren; het commando staat in
  `features/send-email.md`.
- De eigen Application `ron-infrastructure` aanzetten
  (`bootstrap/rig-system/kustomize/overlays/odcn-production/`, regel staat klaar in
  commentaar) en `MAIL_RELAY_API_URL` aan in de OPI-overlay van odcn. NIET via
  `clusters/odcn`: de CMP slaat die build plat naar `rig-prd-operations`. De geheimen
  (`mail-relay-secret`, `mail-db-credentials`) staan sinds 20 augustus versleuteld in
  git; de database komt declaratief mee via `postgresql/database/base/`.

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

## 8. Naar buiten mailen kan nog niet: de upstream weigert externe ontvangers

**Wat**: de upstream accepteert post AAN `rijksoverheid.nl` en weigert al het andere.
GEMETEN op 21 augustus 2026 met twee testberichten vanaf `project-tvas-7pb` op productie:
naar `robbert.uittenbroek@rijksoverheid.nl` volgde `250 ok: Message 139253981 accepted`,
naar een gmail-adres volgde `550 #5.1.0 Address rejected.` op de RCPT TO bij
`rmrmail.rijksweb.nl`. Ons eigen pad deed het dus goed; de weigering komt van de upstream.

**Waar**: niets in onze configuratie. Dit is de afspraak met het mailteam.

**Voorstel**: bij het mailteam navragen of uitgaande post naar buiten uberhaupt de
bedoeling is voor dit relaypad, en zo ja onder welke voorwaarde. Zolang dit staat, is de
dienst send-email feitelijk alleen bruikbaar voor post binnen de Rijksoverheid, en dat is
iets anders dan wat er nu in `features/send-email.md` staat.

**Open**: het gesprek. Let op dat punt 10 hieronder betekent dat een project deze
weigering op dit moment niet te zien krijgt.

## 9. De MTA-STS-lookup hangt 131 seconden op geblokkeerde egress

**Wat**: voor elke ontvangende domeinnaam met een MTA-STS-record haalt Stalwart de policy
op via HTTPS (`https://mta-sts.<domein>/.well-known/mta-sts.txt`). Deze namespace heeft
geen internet-egress, dus die fetch loopt in een timeout. GEMETEN: de bezorgpoging naar
gmail.com duurde 131 seconden, waarvan vrijwel alles in die lookup zat. Bij
`rijksoverheid.nl` viel het niet op, want daar bestaat het DNS-record niet en strandde de
lookup in 7 ms.

**Waar**: Stalwart doet dit per bezorgpoging, dus dit raakt de doorlooptijd van elk
bericht naar een domein dat MTA-STS publiceert (gmail, outlook en de meeste grote
partijen).

**Voorstel**: MTA-STS uitzetten, met dezelfde redenering als DKIM. Wij praten nooit
rechtstreeks met de ontvangende server: alles gaat naar een upstream die zijn eigen
transportbeveiliging regelt. Een policy ophalen over een verbinding die wij niet leggen,
beschermt niets en kost hier twee minuten per bericht.

**Let op de klasse**: dit is dezelfde val als de starthang uit punt 6. Een geblokkeerde
uitgang levert hier geen foutmelding op maar een wachttijd, en op de sandbox (Kind dwingt
netwerkbeleid niet af) is het per definitie niet te reproduceren.

## 10. Bounces verdwijnen stil, want de upstream weigert ons eigen afzenderadres

**Wat**: mislukt een bezorging, dan maakt Stalwart netjes een DSN en stuurt die naar het
envelope-adres, dus naar `noreply-rijksapp+<project>@rijksoverheid.nl`. Die DSN gaat langs
dezelfde upstream, en die weigert dat adres als ONTVANGER met `550 #5.1.0 Address
rejected.`. Stalwart noteert dan "discarding message after double bounce" en gooit hem
weg. GEMETEN op 21 augustus 2026, direct achter de weigering uit punt 8.

**Gevolg**: een project dat post kwijtraakt, krijgt daar niets over te horen. Wij ook
niet. De enige plek waar het staat is de relaylog, en die bewaart drie uur.

**Waar**: dit is de "bounce-postbus" die in `features/send-email.md` al als voorwaarde bij
het aanzetten staat en die er nog niet is. Het is dus geen nieuw gat, maar het is nu
gemeten in plaats van voorspeld, en het is ernstiger dan "onbestelbare post is
onzichtbaar": ook een 550 op de eerste hop verdwijnt.

**Voorstel**: het adres moet een echte postbus krijgen bij het mailteam, en OPI moet die
legen. Tot die er is, is de relaylog de enige waarheid en zou een mislukte bezorging
minstens een alert moeten opleveren.

## 11. Een commit is hier geen wijziging

**Wat**: `bootstrap/rig-system/kustomize/overlays/odcn-production` wordt met de hand
toegepast (`task bootstrap-argo-system`), niet door ArgoCD. Een wijziging daarin is dus
pas een wijziging als iemand die taak draait, en niets laat zien dat dat nog moet.

**Hoe het bewezen is**: #168 zette de relay uit "tot de RCA rond is", op beide
schakelaars. Die commit heeft het cluster nooit bereikt. De OPI-deployment draagt geen
ArgoCD-tracking-id en had `MAIL_RELAY_API_URL` gewoon nog staan, dus OPI wees die hele
periode naar de crashende relay. De noodrem zat in git en nergens anders. Vastgesteld op
21 augustus 2026.

**Waarom dit erger is dan het klinkt**: het gaat hier om de noodrem. Precies de
wijzigingen die je onder druk maakt (iets uitzetten omdat het stuk is) landen in het deel
van het platform waar een commit stil niets doet, en je gaat naar huis in de overtuiging
dat het uit staat.

**Voorstel**: een detectie in plaats van een afspraak. De gerenderde bootstrap vergelijken
met de live toestand en het verschil melden, bijvoorbeeld als CI-stap of als controle in
de Services-statuspagina. Een ArgoCD-Application die de bootstrap zelf bewaakt kan ook,
maar dat is een grotere ingreep en de kip-en-ei met ArgoCD zelf moet dan opgelost worden.

**Open**: welke van de twee, en waar de melding landt.
## STARTTLS op de submission-listener van de relay

**Stand**: open, en het raakt alle tenants tegelijk.

De submission-listener van de relay biedt **geen STARTTLS** aan. Gemeten met `EHLO` in
RC-158 (sandbox): `AUTH PLAIN LOGIN`, geen `STARTTLS`. Alles wat post aanbiedt praat daar
dus platte AUTH binnen het cluster: elk project met `SMTP_HOST`/`SMTP_PORT`, ZAD zelf, en
sinds RC-159 ook Keycloak.

Dat is geen nieuwe blootstelling en het is bewust aanvaard en opgeschreven
(`features/keycloak-mail.md`), maar het is er wel een. Wie het cluster kan afluisteren leest
het gedeelde relaywachtwoord van het platform mee.

**Voorstel**: STARTTLS aanzetten op die listener, met een certificaat dat de clients kunnen
valideren. Let op de tweede helft: de eigen verzender van Keycloak heeft STARTTLS standaard
AAN en wordt in het manifest expliciet uitgezet (`ZAD_MAIL_RELAY_STARTTLS: "false"`); die
regel moet er dan uit, en hij staat in de basis van het Keycloak-manifest.

**Open**: welk certificaat, en of de bestaande projectapplicaties het aankunnen zonder
aanpassing.

## Declaratieve platformaccounts in de relay

**Stand**: open, en het vraagt eerst een METING.

`zad-platform` en `zad-keycloak` zijn gewone principals in de database van de relay, die OPI
bij het opstarten aanmaakt en bijwerkt. Mooier zou zijn als de relay dit soort
platformaccounts zelf declaratief kende - dan komt OPI er helemaal niet aan te pas en
verdwijnt de rotatievolgorde uit `features/keycloak-mail.md`.

Stalwart kan principals in de configuratie dragen via een directory van het type `memory`,
maar `[session.auth]` wijst **één** directory aan en de projectaccounts leven in de interne
directory in de database. Of dat per LISTENER te scheiden is - een tweede submission-listener
voor platformcomponenten, met een eigen `directory` - is **niet gemeten**.

**Open**: die meting. Zonder die uitkomst is dit geen plan maar een wens.

## Eigen mailsjablonen voor Keycloak, en de vork eronder

**Stand**: open, en er ligt een ontwerpvraag onder die eerst beslist moet worden.

RC-175 heeft de inlogpost Nederlands gemaakt door `internationalizationEnabled`,
`supportedLocales` en `defaultLocale` op elke realm te zetten. Dat gebruikt Keycloaks eigen
vertalingen, en die zijn onvolledig (406 regels tegen 534 Engelse op het moment van
schrijven), dus een enkele zin valt terug op het Engels. `emailTheme` is bewust leeg
gebleven: het MinBZK-thema levert geen bruikbaar mailthema (waargenomen: kale Engelse tekst
met dat thema geladen).

**De vork, en hij moet in die taak beslist worden en niet erbuiten:**

- Wil je **EEN taal per bericht**, op basis van de locale van de gebruiker, dan is het
  locale-mechanisme van RC-175 de goede weg en zijn eigen sjablonen alleen een kwestie van
  mooiere teksten.
- Wil je **BEIDE talen in EEN bericht**, dan werkt dat mechanisme juist tegen je: Keycloak
  rendert precies een locale. Dan schrijf je eigen FreeMarker-sjablonen die beide talen zelf
  bevatten.

**Wat het NIET vraagt**: geen Java en geen build. De bezorging bestaat al - een ConfigMap
gemount onder `/opt/keycloak/themes/`, dezelfde weg die de relay-emailSender-jar al gebruikt
(zie de `zad-providers`-volume in het Keycloak-manifest).

## Wat hier bewust NIET staat

De fundament-migratie van de sandbox (app-of-apps): eigen traject, en er ligt op dit
moment geen document dat de stand daarvan beschrijft. En de goedkeurings-UX rond
"goedgekeurd maar aan geen component gekoppeld": besproken op 20 augustus en bewust
niet gebouwd; de koppelvraag bij het aanvragen stellen is de minst ingrijpende variant
als het toch gaat knellen.
