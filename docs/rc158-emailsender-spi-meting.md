# Meting RC-158: wint een eigen emailSender-SPI van de realmconfiguratie op Keycloak 25.0.6?

**Gemeten op 25 augustus 2026, op het sandboxcluster, tegen `quay.io/keycloak/keycloak:25.0.6`
in PRODUCTIEMODUS (`kc.sh start`, niet `start-dev`).**

**Uitkomst: ja.** Een eigen `EmailSenderProvider` wordt in productiemodus daadwerkelijk
gebruikt, hij wordt ook bereikt op een realm zonder `smtpServer`, en de omleidingsvector
uit RC-156 is ermee weg. De ruwe uitvoer staat in `docs/rc158-metingen/`.

## De opstelling

| Onderdeel | Wat |
|---|---|
| Keycloak | een APARTE proefdeployment `rc158-keycloak` in `rig-system`, zelfde image, zelfde `start`-modus, eigen database `keycloak_rc158`. De echte sandbox-Keycloak is niet aangeraakt. Manifest: `rc158-metingen/keycloak-proef-deployment.yaml` |
| De jar | `keycloak-relay-email-sender-proef-0.1.0-proef.jar`, door een initContainer in de `emptyDir` op `/opt/keycloak/providers/` gezet — hetzelfde patroon als de echte deployment. Bron: `keycloak-migration/relay-email-sender/` |
| De relay | `rig-mail-relay.rig-ron:587`, met een eigen account `keycloak-proef` dat voor deze meting is aangemaakt. Erachter de Mailpit-sink |
| De luisteraar van de "aanvaller" | `rc158-lokaas`: een SMTP-server die niets bezorgt, geen STARTTLS aanbiedt en ELKE regel logt, inclusief de AUTH-regel in platte tekst. Bron: `rc158-metingen/lokaas.py` |
| De realms | `proef-omleiding` (`smtpServer.host` staat op de lokaas, met auth aan), `proef-zonder-smtp` (geen `smtpServer`), `proef-minimale-smtp` (`smtpServer` met precies één sleutel), `proef-idp` (de IdP voor de brokerweg) |

## 1. Wordt onze provider in productiemodus daadwerkelijk gebruikt?

**Ja.** Het merkteken staat op elk bericht in de sink, en de weg erheen is zichtbaar:

```
2026-08-25T13:00:37.845Z | reg-runf-definitief@example.org | Verify email
    From: ['noreply-rijksapp@rijksoverheid.nl']
    Return-Path: ['<noreply-rijksapp+keycloak-proef@rijksoverheid.nl>']
    X-Zad-Email-Sender: ['zad-relay-proef/0.1.0']
    Received: ['from rig-mail-relay (...) by rig-mail-sink-... (Mailpit) with SMTP
                for <reg-runf-definitief@example.org>; Tue, 25 Aug 2026 13:00:37 +0000 (UTC)']
```

Drie dingen tegelijk, en dat is het punt van dat merkteken: `X-Zad-Email-Sender` kan alleen
door ONZE code gezet zijn, `Return-Path` is de bounce-adressering die de relay voor het
account `keycloak-proef` maakt, en `Received` noemt de relay als afzender richting de sink.
"De mail kwam aan" en "onze provider deed het" zijn hier niet te verwarren.

De pod deed er ook zelf verslag van:

```
KC-SERVICES0047: zad-relay-proef (nl.minbzk.rig.keycloak.email.RelayEmailSenderProviderFactory)
  is implementing the internal SPI emailSender.
ZAD-RELAY-PROEF: aangezet, relay rig-mail-relay.rig-ron.svc.cluster.local:587 als keycloak-proef
ZAD-RELAY-PROEF: bericht voor ... gaat naar relay ...; smtpServer van de realm genegeerd (9 sleutels, host=rc158-lokaas...)
ZAD-RELAY-PROEF: bericht voor ... aangeboden aan de relay
```

De terugval uit [keycloak#14522](https://github.com/keycloak/keycloak/issues/14522) trad
NIET op. Alle uitvoer: `rc158-metingen/04-keycloak-relevante-regels.log` en
`rc158-metingen/06-sink-kopregels.txt`.

De meting is gedaan op een pod die al even draaide: de pod startte om 12:58:33, het eerste
bericht ging om 13:00:36.

## 2. Welke vlagvorm wijst hem aan, en is er een bouwstap nodig?

**Twee vormen werken, en ze zijn aantoonbaar OPGEPIKT en niet alleen meegegeven:**

| Vorm | Uitkomst |
|---|---|
| `--spi-email-sender-provider=zad-relay-proef` (argument) | ✅ opgepikt, onze provider verstuurt |
| `KC_SPI_EMAIL_SENDER_PROVIDER=zad-relay-proef` (omgeving) | ✅ opgepikt, onze provider verstuurt |
| `--spi-emailSender-provider=zad-relay-proef` (camelCase) | ❌ **STIL genegeerd**: Keycloak start gewoon door, geen waarschuwing, en de STANDAARDprovider verstuurt |

De SPI heet intern `emailSender` (`EmailSenderSpi.getName()`); in de vlag wordt dat
`email-sender`. Schrijf je de SPI-naam in camelCase, dan is er geen enkel signaal — dat is
dezelfde klasse val als de vault-resolver en de sleutels in de relayconfiguratie.

Het bewijs dat de goede vorm wél wordt gelezen, is de tegenproef: **een provider-id dat niet
bestaat laat Keycloak WEIGEREN TE STARTEN**, bij beide goede vormen:

```
ERROR: Build step org.keycloak.quarkus.deployment.KeycloakProcessor#configureKeycloakSessionFactory
       threw an exception: java.lang.RuntimeException: Failed to find provider bestaat-niet for emailSender
       at org.keycloak.quarkus.deployment.KeycloakProcessor.checkProviders(KeycloakProcessor.java:896)
ERROR: Failed to find provider bestaat-niet for emailSender
```

Daarmee is de vlag geen bewering meer: een pod die met `--spi-email-sender-provider=zad-relay-proef`
OPKOMT, heeft die provider gevonden. De camelCase-vorm daarentegen kwam met
`=bestaat-niet` gewoon op — hij wordt niet gelezen. Zie
`rc158-metingen/05-niet-bestaand-provider-id.log`.

**Een expliciete `kc.sh build` is niet nodig.** De providermap is een `emptyDir` die de
initContainer bij elke podstart opnieuw vult, en `kc.sh start` doet de augmentatie dan zelf:

```
Changes detected in configuration. Updating the server image.
Updating the configuration and installing your custom providers, if any. Please wait.
```

Dat gebeurde in ELKE ronde, dus ook op een verse pod (elke ronde hier is een verse pod: er
is vijf keer een rollout gedaan). Het kost wel ~10 seconden augmentatie per start. Wie dat
niet wil, bakt de jar in een eigen image en draait `--optimized`; dat is een keuze voor het
herziene plan, geen voorwaarde voor deze weg.

## 3. Bereikt Keycloak de provider ook zonder `smtpServer` op de realm?

**Voor de bevestigingsmail: ja, gemeten op drie wegen.** Op `proef-zonder-smtp`, een realm
waarvan `smtpServer` letterlijk leeg is (de provider ziet `0 sleutels, host=null`):

| Weg | Uitkomst |
|---|---|
| `PUT /admin/realms/{realm}/users/{id}/execute-actions-email` met `VERIFY_EMAIL` | HTTP 204, bericht in de sink |
| Zelfregistratie via het registratieformulier (`verifyEmail` aan) | "Verify email" in de sink |
| "Wachtwoord vergeten" via het inlogscherm | bericht verstuurd |

Ook de knop **Test connection** in de beheerconsole
(`POST /admin/realms/{realm}/testSMTPConnection`) komt bij onze provider terecht: hij geeft
de opgegeven configuratie rechtstreeks mee zonder hem op te slaan, en onze provider negeert
hem net zo goed. Gemeten: HTTP **204** — dus de knop meldt SUCCES terwijl het bericht via de
relay ging en de opgegeven host niets kreeg. Dat is voor een tenantbeheerder misleidend en
hoort in het herziene plan als aandachtspunt.

**Er is één uitzondering, en die is echt.** In `keycloak-services` staat precies één plek
waar Keycloak zelf beslist voordat de SPI in beeld komt:
`org.keycloak.authentication.authenticators.broker.IdpEmailVerificationAuthenticator` toetst
`realm.getSmtpConfig().isEmpty()` en slaat zichzelf dan over. Dat is de stap **"Verify
existing account by Email"** in de first-broker-login-flow — de stap die loopt als een
gebrokerde gebruiker een e-mailadres meebrengt dat al bij een lokaal account hoort.

Gemeten met een OIDC-broker tussen twee realms:

| Realm | `smtpServer` | Wat er gebeurde |
|---|---|---|
| `proef-zonder-smtp` | leeg | `KC-SERVICES0023: Smtp is not configured for the realm. Ignoring email verification authenticator` — de provider werd NIET bereikt, de flow viel terug op "Authenticate to link your account with proef-idp" |
| `proef-omleiding` | 9 sleutels (naar de lokaas) | "You need to verify your email address to link your account" — bericht via ONZE provider naar de relay |
| `proef-minimale-smtp` | **één** sleutel: `{"from": "noreply-rijksapp@rijksoverheid.nl"}` | idem: bericht via onze provider (`1 sleutels, host=null`) |

**Het minimum is dus: een `smtpServer` die niet leeg is.** De grendel is `isEmpty()`, niet
"is deze configuratie bruikbaar". Eén sleutel die geen bestemming noemt is genoeg, en
deelvraag 4 hieronder bewijst dat de inhoud er verder niet toe doet. Voor de SPI-route
betekent dat: zet op elke realm een `smtpServer` met precies dat ene onschuldige veld, dan
werkt ook deze authenticator, zonder dat er ergens een bestemming staat om om te leiden.

## 4. Is de omleidingsvector werkelijk weg?

**Ja.** Dit is de belangrijkste uitkomst.

De **controle eerst**, want zonder controle zegt een lege log niets. Met de standaardprovider
(geen SPI-vlag) is de aanval uit RC-156 gewoon nog gelukt — de luisteraar van de aanvaller
kreeg de post én het wachtwoord in platte tekst
(`rc158-metingen/01-controle-standaardprovider-lokaas.log`):

```
=== VERBINDING van 10.244.0.125:54558 ===
<<< EHLO rc158-keycloak-98dbccf98-mkpwn
<<< AUTH LOGIN
!!! AUTH ONTVANGEN: AUTH LOGIN
!!! AUTH LOGIN gebruikersnaam (b64): YnVpdA== -> 'buit'
!!! AUTH LOGIN WACHTWOORD (b64): SEVULUdFSEVJTS1WQU4tSEVULVBMQVRGT1JN -> 'HET-GEHEIM-VAN-HET-PLATFORM'
<<< MAIL FROM:<aanvaller@example.org>
<<< RCPT TO:<proef-omleiding@example.org>
<<< DATA
...
```

Met de SPI aan, dezelfde realm, dezelfde `smtpServer`, dezelfde luisteraar, over de
admin-API, de zelfregistratie, "wachtwoord vergeten", de brokerkoppeling en de
Test-connection-knop. **De volledige uitvoer van de luisteraar**
(`rc158-metingen/02-spi-aan-lokaas-leeg.log`):

```
lokaas luistert op 0.0.0.0:2525 - elke regel komt hier in de log
```

Eén regel — zijn eigen startregel. Geen verbinding, geen EHLO, geen AUTH, geen wachtwoord.
Keycloak logde bij elk van die berichten dat het de `smtpServer` van de realm zag en negeerde
(`9 sleutels, host=rc158-lokaas.rig-system.svc.cluster.local`), en de sink kreeg ze allemaal.

Kind dwingt netwerkbeleid niet af, dus de lokaas was voor Keycloak zonder enige moeite
bereikbaar: de aanval was hier maximaal makkelijk. Over wat op ODCN bereikbaar is zegt dat
niets — dat is een andere vraag.

## 5. Hoe faalt het?

**Het faalt dicht.** Gemeten door de vlag in de verkeerde vorm te zetten (camelCase, dus stil
genegeerd, dus de standaardprovider):

| Realm | Uitkomst |
|---|---|
| `proef-zonder-smtp` (leeg) | `execute-actions-email` → **HTTP 500** `Failed to send execute actions email`; registratie → de gebruiker blijft op "You need to verify your email address to activate your account" en is dus NIET actief. Sink: 0 berichten erbij |
| `proef-omleiding` (naar de lokaas) | de aanval lukt weer — de lokaas krijgt de post en het wachtwoord |

De onderliggende fout is de standaardprovider die geen afzender kan bepalen:

```
KC-SERVICES0029: Failed to send email: org.keycloak.email.EmailException: Please provide a valid address
  at org.keycloak.email.DefaultEmailSenderProvider.toInternetAddress(DefaultEmailSenderProvider.java:175)
KC-SERVICES0088: Failed to send execute actions email: ...
type="SEND_VERIFY_EMAIL_ERROR" ... error="email_send_failed"
```

Er gaat dus **niets** uit langs een weg die we niet willen, zolang de realms geen
`smtpServer` dragen: geen mail, een zichtbare 500, een `SEND_VERIFY_EMAIL_ERROR`-event, en
een gebruiker die niet geverifieerd raakt.

Daar hangt wel een voorwaarde aan die het herziene plan moet vasthouden, en die volgt uit
deelvraag 3: als we op elke realm een minimale `smtpServer` zetten om de brokerweg te laten
werken, dan is dat veld het enige dat een stille terugval zou kunnen voeden. Zolang dat veld
alleen een `from` bevat en geen `host`, blijft de uitkomst dezelfde — de standaardprovider
heeft dan nog steeds geen bestemming en faalt dicht. Een realm mag dus nooit een `host`
krijgen, en dat is precies wat er nu ook al niet mag.

## Aanbeveling

**De SPI-route.** Hij doet wat we ervan hoopten en de aanval die RC-156 vier keer heeft laten
stranden is er meetbaar mee weg: in geen enkele realm bestaat nog een bestemming om om te
leiden, dus `manage-realm` mag blijven waar hij is en er komt geen rolgrendel bij die de
volgende reviewronde alsnog omzeilt via een dragersklasse die niemand bedacht had. Eén
mailaccount blijft genoeg, de bootstrap blijft zoals hij is, en er ontstaat geen koppeling
tussen de dienst keycloak en de dienst mail — waar de route met een account en een vault-sleutel
per realm van Keycloak juist een tussenhandelaar zou maken met een levenscyclus per project.
De prijs is eerlijk en klein: een jar die wij onderhouden tegen een INTERNE SPI (Keycloak
waarschuwt daar zelf voor met KC-SERVICES0047, dus een major upgrade vraagt een hertoets), een
vlagvorm die bij één verkeerd streepje stil terugvalt op de standaardprovider — pin die vorm
met een toets, want de tegenproef laat zien dat het gedrag bestaat — en op elke realm een
minimale `smtpServer` met alleen een `from`, zodat de ene authenticator die vóór de SPI
beslist ook werkt. Dat laatste is geen gat: die ene sleutel noemt geen bestemming.

## Wat hier NIET is gemeten

- Of dit op ODCN net zo loopt. Alleen sandbox, met een sink in plaats van de echte mailserver.
- Of de relay op ODCN op poort 587 STARTTLS aanbiedt. In de sandbox doet hij dat **niet**
  (met `EHLO` nagemeten: `AUTH PLAIN LOGIN`, geen `STARTTLS`), dus deze meting draaide met
  `ZAD_MAIL_RELAY_STARTTLS=false`. Dat is een eigenschap van de submission-listener van de
  relay en staat los van deze vraag, maar het herziene plan moet niet aannemen dat STARTTLS
  er is.
- Wat er gebeurt bij een Keycloak-upgrade. `emailSender` is een interne SPI en mag zonder
  aankondiging veranderen.
- Er is niets uitgerold buiten de proefdeployment. De echte sandbox-Keycloak, de
  infrastructuurmanifesten en odcn zijn niet aangeraakt.

## Opruimen

Na de meting zijn verwijderd: de proefdeployment en zijn Service, de database
`keycloak_rc158`, de luisteraar, de hulppod, de ConfigMaps en het relayaccount
`keycloak-proef`. De sandbox is teruggegeven.
