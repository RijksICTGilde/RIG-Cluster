# De mailketen van Keycloak

Keycloak verstuurt post: bevestigingsmail voor een e-mailadres, "wachtwoord vergeten", en de
berichten die bij het koppelen van een SSO-account horen. Dit document beschrijft hoe die
post het platform verlaat, waarom er in **geen enkele realm een wachtwoord of een bestemming
staat**, wat een projectbeheerder daarmee wel en niet kan, en wat er bij een rotatie gebeurt.

Voor de post die ZAD zelf verstuurt (uitnodigingen, meldingen) en voor de mailrelay als
dienst voor projecten: `features/send-email.md`.

## De opzet

```
Keycloak rendert het bericht (sjablonen en thema blijven van Keycloak)
   |
onze verzender  ->  negeert de smtpServer van de realm, verbindt met de relay
   |               op gegevens uit de omgeving van de POD
mailrelay (Stalwart)  ->  zet de From: vast, herschrijft de envelope, telt het budget
   |
upstream (sandbox: de Mailpit-sink)
```

Het middelste blok is het verschil met een standaard Keycloak. Normaal draagt **elke realm**
zijn eigen `smtpServer`-map met host, gebruiker en wachtwoord; Keycloak heeft geen
serverbrede SMTP-instelling. Op dit platform is die map vervangen door een eigen
`EmailSenderProvider` (`keycloak-migration/relay-email-sender/`) die de relay, de
inloggegevens en het afzenderadres uit de **omgeving van de pod** leest en de map van de
realm volledig negeert.

## Waarom er geen wachtwoord in een realm staat

Dit is de kern, en het is een gemeten en niet een bedachte reden.

Een projectbeheerder heeft `manage-realm` op zijn eigen realm en kan daarmee in de
beheerconsole de `smtpServer` bewerken. Zet hij `host` op een luisteraar die hij zelf
beheert, dan verstuurt Keycloak de volgende bevestigingsmail daarheen - **inclusief de
AUTH-uitwisseling**, dus inclusief het wachtwoord dat in die map staat. Bij een gedeeld
platformaccount is dat het wachtwoord waarmee al het verkeer van heel Keycloak vertrekt.

Een verwijzing in plaats van een wachtwoord (`${vault.smtp-password}`) helpt niet: Keycloak
lost die pas op bij het VERSTUREN, dus de luisteraar krijgt de opgeloste waarde. Dat is
gemeten, met de aanval erbij, in `docs/rc158-emailsender-spi-meting.md`:

```
!!! AUTH LOGIN WACHTWOORD (b64): SEVULUdFSEVJTS1WQU4tSEVULVBMQVRGT1JN -> 'HET-GEHEIM-VAN-HET-PLATFORM'
```

Met de eigen verzender aan, dezelfde realm en dezelfde luisteraar, bevat de volledige log van
die luisteraar één regel: zijn eigen startregel.

De verdediging is dus niet "de projectbeheerder mag dat veld niet bewerken" maar **"dat veld
leidt nergens heen"**. Dat scheelt een hele klasse: elke rolgrendel die het schrijven van
realminstellingen weghoudt bij één drager van `manage-realm`, laat de volgende drager staan
- een groep, een samengestelde rol, een service-account, een uitnodiging. Die weg is vier
reviewrondes lang geprobeerd (RC-156) en steeds opnieuw omzeild.

`manage-realm` blijft daarom gewoon bij de projectbeheerder.

## Wat een projectbeheerder wel en niet kan

| Kan hij | Uitkomst |
|---|---|
| De `smtpServer` van zijn realm bewerken | Ja, maar het doet niets: de verzender leest die map niet. OPI veegt de verbindingsvelden er bij de volgende verwerking weer af. |
| De post naar een eigen luisteraar sturen | **Nee.** De bestemming staat in de omgeving van de pod. |
| Het relaywachtwoord lezen | **Nee.** Het staat in geen enkele realm, en ook niet in een vault die een realm kan aanwijzen. |
| Op **Test connection** klikken | Ja, en die knop **liegt** - zie hieronder. |
| `Reply-To:` op zijn realm zetten | Ja. Dat is het enige veld in die map dat echt van de realm is: de relay raakt `Reply-To:` niet aan, en de reconcile laat het staan. |

### De knop "Test connection" is misleidend

In de beheerconsole van een realm zit een knop **Test connection** bij de mailinstellingen
(`POST /admin/realms/{realm}/testSMTPConnection`). Gemeten in RC-158: die knop komt bij onze
verzender terecht, die de opgegeven configuratie net zo goed negeert, en antwoordt **HTTP
204**. De knop meldt dus SUCCES terwijl het testbericht via de relay ging en de opgegeven
host niets kreeg.

Dat is **geen gat** - er lekt niets en er gaat niets naar de opgegeven host - maar het liegt
tegen een tenantbeheerder die aan het uitzoeken is waarom zijn instellingen niets doen.

**De knop is niet verborgen, en dat is een keuze.** Hij zit in de beheerconsole van Keycloak
zelf; hem weghalen vraagt een eigen thema-override op de admin console, en dat is een tweede
stuk maatwerk tegen een console die bij elke upgrade verandert - meer risico dan de
verwarring waard is. Wat er in plaats daarvan is: deze regel, en het feit dat de hele
`smtpServer` van een realm bij elke verwerking wordt teruggezet naar één veld, zodat er weinig
te testen overblijft.

## Waarom er tóch één veld op elke realm staat

Elke door OPI beheerde realm krijgt een `smtpServer` met **precies één sleutel**:

```json
{"from": "noreply-inloggen@rijksoverheid.nl"}
```

Geen host, geen gebruiker, geen wachtwoord.

Er is precies één plek waar Keycloak beslist vóórdat de verzender in beeld komt:
`IdpEmailVerificationAuthenticator` toetst `realm.getSmtpConfig().isEmpty()` en slaat zichzelf
dan over. Dat is de stap **"Verify existing account by Email"** in de
first-broker-login-flow: de stap die loopt als een gebrokerde gebruiker een e-mailadres
meebrengt dat al bij een lokaal account hoort. De grendel is `isEmpty()` en niet "is deze
configuratie bruikbaar", dus één sleutel is genoeg en de inhoud doet er niet toe (gemeten).

Het is daarom de ene sleutel die **geen bestemming noemt**. Dat is niet cosmetisch:

> **Het faalt dicht, zolang geen enkele realm een `host` draagt.** Zou de eigen verzender ooit
> stil terugvallen op die van Keycloak - een upgrade is de realistische weg - dan kan die
> zonder host niets bezorgen. Hij faalt met `Please provide a valid address`, een zichtbare
> 500 en een `SEND_VERIFY_EMAIL_ERROR`, en er gaat niets uit. Met een host zou hij daarheen
> bezorgen.

Wat een tenant er alsnog in zet (`host`, `port`, `user`, `password`, `auth`, `ssl`,
`starttls`) wordt bij de volgende verwerking weggeveegd. **Dat is driftherstel en geen
verdediging** - de verdediging is dat de verzender de map niet leest. Een tenant kan die
velden een seconde later terugzetten, en het leidt nog steeds nergens heen.

Op een cluster **zonder** mailrelay wordt dit veld niet geschreven. Daar werkt de post toch
niet, en het weglaten doet iets nuttigs: dan slaat die ene authenticator zichzelf weer over
en krijgt een gebrokerde gebruiker het scherm "authenticate to link your account" in plaats
van een mislukte verzending.

## De startvlag, en waarom de jar in git staat

De verzender wordt aangewezen met een startvlag op de Keycloak-pod:

```
--spi-email-sender-provider=zad-relay
```

Zonder die vlag verstuurt de standaardprovider, en die leest de `smtpServer` van de realm.
De vlag is dus wat een stille terugval onmogelijk maakt.

**Twee eigenschappen van die vlag, allebei gemeten:**

1. `--spi-emailSender-provider=` (camelCase) wordt **STIL genegeerd**. Keycloak start gewoon
   door, geeft geen waarschuwing, en de standaardprovider verstuurt. Er is geen enkel signaal
   op het cluster. De vorm is daarom aan twee kanten gepind: aan de Java-kant uit
   `EmailSenderSpi.getName()`, en aan de OPI-kant met een toets die het manifest naast de
   Java-bron legt (`tests/test_keycloak_relay_email_sender.py`).
2. Een provider-id dat **niet bestaat** laat Keycloak **weigeren te starten**. Een pod die
   opkomt heeft zijn provider dus aantoonbaar gevonden.

Uit die tweede eigenschap volgt de rest: Keycloak start niet als de jar er niet is. De twee
andere jars in die pod (het thema en de SAML-mapper) worden bij elke podstart van github.com
gehaald; dat risico bestaat al, maar deze jar mag er niet bij - dan zou een hapering bij
GitHub het inloggen van het hele platform platleggen.

De jar komt daarom uit een **ConfigMap** naast het manifest:

```
infrastructure/bootstrap/infrastructure/keycloak/controller/base/providers/keycloak-relay-email-sender-1.0.0.jar
```

Alle afhankelijkheden staan op `provided` (ook `jakarta.mail`, want Keycloak gebruikt die
zelf al), dus het zijn drie klassen plus het registratiebestand: een handvol kilobytes, ruim
binnen de 1 MiB van een ConfigMap. De generator zet een hash van de inhoud in de naam, dus
een nieuwe jar is vanzelf een rollout.

**Bron en jar horen in dezelfde commit.** Dat is afgedekt met de strenge van de twee opties
uit het plan: de bouw is reproduceerbaar gemaakt (`project.build.outputTimestamp`) en de
CI-job `keycloak-relay-email-sender` bouwt hem opnieuw en vergelijkt **byte voor byte**.
Lokaal: `task check-keycloak-relay-email-sender`.

## STARTTLS: platte AUTH binnen het cluster, en dat is een besluit

De submission-listener van de relay biedt **geen STARTTLS** aan (met `EHLO` nagemeten in
RC-158), dus de verbinding tussen Keycloak en de relay is platte AUTH binnen het cluster.

Dat is precies wat elk project vandaag ook doet met zijn `SMTP_HOST`/`SMTP_PORT`, dus het is
geen nieuwe blootstelling. In de provider staat STARTTLS **aan** als standaard; het uitzetten
is een letterlijke regel in het manifest, zodat de keuze zichtbaar is en niet stil.

Wat er níet is gedaan: `TRUST_ALL` laten staan "omdat er toch geen TLS is". Zo'n knop hangt
aan een omgevingsvariabele en reist mee naar een cluster waar TLS wel iets betekent.

**Vervolgpunt:** STARTTLS aanzetten op die listener helpt alle tenants tegelijk en is een
eigen taak. Zie `plans/mail-vervolgpunten.md`.

## Het account en het geheim

Er is **één account voor heel Keycloak**: `zad-keycloak`, naast `zad-platform` waarmee ZAD
zelf verstuurt. Twee accounts en niet één, want de twee versturen verschillende post - ZAD
schrijft aan de mensen van een project, Keycloak schrijft inlogpost aan wie er inlogt - en
met één account is een bounce op het ene niet te onderscheiden van een bounce op het andere.

Het wachtwoord komt uit de **bootstrap**, niet uit OPI. De regel die de twee gevallen uit
elkaar houdt:

> Een geheim dat een **derde partij** nodig heeft voordat OPI iets gedaan heeft, komt uit de
> bootstrap; een geheim dat **alleen OPI** gebruikt, genereert OPI zelf.

`zad-platform` wordt door niemand anders gelezen, dus OPI maakt dat wachtwoord zelf. Keycloak
kent OPI niet en hoort niet op OPI te wachten, dus dat wachtwoord moet kunnen bestaan zonder
dat OPI ooit gedraaid heeft.

De keten:

| Stap | Waar |
|---|---|
| 1. De generatie zet een willekeurig wachtwoord in het Secret | `infrastructure/bootstrap/infrastructure/secrets/templates/keycloak-mail-secret.yaml` |
| 2. De Keycloak-pod leest het als `ZAD_MAIL_RELAY_PASSWORD` | `secretKeyRef` in `.../keycloak/controller/base/deployment.yaml`, `optional: true` |
| 3. OPI leest hetzelfde Secret en zorgt dat de relay het account draagt | `MailManager.ensure_keycloak_account()`, fase 3b van het opstarten |
| 4. Het account krijgt een eigen `From:` in het gegenereerde sieve-script | `MAIL_SENDER_ADDRESS_PREFIX` in `opi/connectors/mail.py` |

**`optional: true` op stap 2 is een afweging.** Zonder die vlag start Keycloak niet zolang het
Secret er niet is, en dan blokkeert een mailgeheim de hele identiteitsvoorziening van het
cluster. Mét de vlag start hij, logt de provider `geen bruikbare relayconfiguratie in de
omgeving`, en faalt elke verzending luid.

### Het afzenderadres

`noreply-inloggen@<domein>`, met een eigen lokaal deel naast dat van de portal
(`noreply-rijksapp@<domein>`), zodat een ontvanger - en een bounce - inlogpost van de post van
de portal kan onderscheiden. Het **domein** is dat van het cluster en is niet instelbaar:
envelope en `From:` moeten in één domein blijven of DMARC valt om, want wij ondertekenen niet
met DKIM.

Bijzonderheid: de **envelope houdt het afgeleide adres** (`noreply-rijksapp@...`), alleen de
`From:` wordt overschreven. Dat ziet eruit als een vergissing en is het niet: DMARC vergelijkt
de domeinen, die blijven gelijk, en het plusdeel in de envelope blijft de bounce dragen. Het
staat in `config.toml` van de relay en in `opi/connectors/mail.py` allebei opgeschreven, want
wie de twee regels naast elkaar leest ziet er anders een fout in.

Anders dan een projectadres draagt dit geen plusdeel: `zad-keycloak` verstuurt voor alle
realms tegelijk, dus er is geen project om te noemen. Dat is precies de beperking die deze
opzet zou beëindigen als er ooit branding per realm gewenst is.

### Rotatie: de volgorde, en wat er tussendoor faalt

Het geheim staat op één plek in git en wordt door twee partijen gelezen die het op
verschillende momenten oppikken.

1. **Het geheim verandert in git** en ArgoCD zet het in het cluster.
2. **OPI** zet het op de relay bij zijn volgende verwerking (of meteen bij een herstart).
3. **Keycloak** ziet het pas bij een **HERSTART**: het is een omgevingsvariabele, geen mount,
   dus de kubelet ververst hier niets.

Tussen 2 en 3 draagt de relay het nieuwe wachtwoord terwijl Keycloak nog het oude aanbiedt:
**inlogpost faalt op authenticatie** (535 bij de relay), er komt geen bevestigingsmail aan, en
Keycloak logt `SEND_VERIFY_EMAIL_ERROR`. Het inloggen zelf blijft werken.

**De volgorde die dat venster het kleinst maakt:**

```bash
# 1. het nieuwe geheim staat in het cluster (ArgoCD heeft gesynct)
# 2. OPI opnieuw laten starten, zodat de relay het nieuwe wachtwoord krijgt
kubectl -n rig-prd-operations rollout restart deployment/operations-manager
kubectl -n rig-prd-operations rollout status deployment/operations-manager
# 3. daarna pas Keycloak, zodat hij het nieuwe wachtwoord meteen goed aanbiedt
kubectl -n rig-system rollout restart deployment/keycloak
```

Andersom (eerst Keycloak) is het venster juist zo lang als het duurt voordat OPI weer draait.

## `verifyEmail`: wie wordt geraakt

Sinds deze taak staat `verifyEmail: true` op de blauwdrukken `sso-support` en
`algoritmeregister`. Een nieuwe gebruiker moet dan zijn adres bevestigen voordat hij binnen
is.

**De blast radius is klein maar niet nul:**

- **SSO-gebruikers niet.** Die komen via `trustEmail: True` op de identity providers al
  geverifieerd binnen.
- **Bestaande lokale gebruikers niet.** Die zijn bij aanmaak op geverifieerd gezet.
- **Wel geraakt:** nieuwe lokale gebruikers, en iedereen die zijn adres wijzigt.

Dat laatste is nieuw en het is de bedoeling. `create_user()` zette `emailVerified`
onvoorwaardelijk op `True` zodra er een adres was, dus elke via de uitnodigingsweg aangemaakte
gebruiker was vooraf geverifieerd zonder dat er ooit iets bevestigd was. De waarde volgt nu de
realm.

**Voor de uitnodigingsweg betekent dat: wie een account krijgt, bevestigt voortaan eerst zijn
adres.** Hij kiest zijn wachtwoord in het uitnodigingsformulier van OPI zoals altijd, en
loopt bij zijn eerste login tegen het bevestigingsscherm aan. Zie `features/invites.md`.

### De grendel: `verifyEmail` mag nooit voor de post uit lopen

Een realm die verifieert en niet kan mailen **sluit nieuwe gebruikers buiten**: die komen
binnen met `emailVerified: false` en wachten op een bericht dat niemand kan versturen.

OPI zet `verifyEmail` daarom alleen AAN als het **platform een relay heeft**
(`MAIL_RELAY_API_URL` gevuld). Heeft het die niet, dan blijft het veld uit, met een
waarschuwing in de log, en zet de eerstvolgende verwerking ná het instellen van de relay het
alsnog aan. **Uitzetten wordt nooit tegengehouden** - die richting haalt een blokkade juist
weg.

Dat is geen theoretische toestand: het is de vaste toestand van clustertype `local`, en het
was de toestand van **productie op 21 augustus 2026** tijdens de crashlus van de relay.

Wat de grendel **niet** doet: hij draait `verifyEmail` niet terug op een realm die al
verifieert wanneer de relay wegvalt. Dat zou bij elke storing heen en weer flapperen, en de
blauwdruk is de bron van waarheid over dat veld. Valt de relay langer uit, dan is het
zetten van `verifyEmail: false` in de blauwdruk de weg.

De maat is de RELAY en niet meer de `smtpServer` van de realm - die laatste zei daar ooit iets
over, maar draagt nu op elke realm hetzelfde minimale veld.

## Het is een INTERNE SPI

Keycloak zegt dat zelf bij het opstarten:

```
KC-SERVICES0047: zad-relay (nl.minbzk.rig.keycloak.email.RelayEmailSenderProviderFactory)
  is implementing the internal SPI emailSender.
```

Een interne SPI mag zonder aankondiging veranderen. **De geplande upgrade naar Keycloak 26
vraagt een hertoets van dit hele mechanisme**; dat staat genoteerd in
`features/keycloak-26-upgrade.md`.

## Bounces verdwijnen stil

Mislukt een bezorging bij de upstream, dan weigert die ook het envelope-adres en gooit de
relay het bericht weg als dubbele bounce. **"Geen foutmelding" is dus geen bewijs van
aankomst**; kijk in de sink of in de postbus. Een bounce-postbus staat open in
`plans/mail-vervolgpunten.md`.

## Waar het staat

| Onderdeel | Pad |
|---|---|
| De verzender (Java) | `keycloak-migration/relay-email-sender/` |
| De jar in de pod | `infrastructure/.../keycloak/controller/base/providers/` + `configMapGenerator` |
| De vlag en de omgevingsvariabelen | `infrastructure/.../keycloak/controller/base/deployment.yaml` |
| Het geheim | `infrastructure/.../secrets/templates/keycloak-mail-secret.yaml` |
| Het account op de relay | `MailManager.ensure_keycloak_account()` |
| De minimale `smtpServer` en `verifyEmail` | `KeycloakYamlHandler._apply_realm_self_service()` |
| `emailVerified` volgt de realm | `KeycloakConnector.create_user()` |
| De meting waaruit dit alles volgt | `docs/rc158-emailsender-spi-meting.md` |
