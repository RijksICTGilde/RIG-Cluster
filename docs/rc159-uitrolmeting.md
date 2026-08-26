# Meting RC-159: de eigen emailSender op de ECHTE Keycloak-deployment

**Gemeten op 26 augustus 2026 op het sandboxcluster, op de deployment uit deze PR** -
`quay.io/keycloak/keycloak:25.0.6` in productiemodus, met de jar uit de ConfigMap en de
startvlag `--spi-email-sender-provider=zad-relay`. De ruwe uitvoer staat in
`docs/rc159-metingen/`.

RC-158 mat hetzelfde mechanisme op een APARTE proefdeployment
(`docs/rc158-emailsender-spi-meting.md`). Deze meting doet het over op de echte
`keycloak`-deployment in `rig-system`, met de manifesten uit deze PR.

## De opstelling

| Onderdeel | Wat |
|---|---|
| Keycloak | de ECHTE deployment `keycloak` in `rig-system`, met de wijziging uit deze PR via Forgejo en ArgoCD uitgerold |
| De jar | `keycloak-relay-email-sender-1.0.0.jar` uit de `configMapGenerator`, door de bestaande initContainer naar `/opt/keycloak/providers/` gekopieerd. Geen netwerk |
| Het geheim | `keycloak-mail-credentials`, met de hand aangemaakt (de sandbox-clustergeheimen zijn gitignored en worden hier niet gegenereerd) |
| Het account | `zad-keycloak`, door OPI bij het opstarten op de relay gezet uit dat geheim |
| De luisteraar van de "aanvaller" | `rc159-lokaas`, de SMTP-server uit RC-158 die niets bezorgt en ELKE regel logt, inclusief de AUTH-regel in platte tekst |
| De realm van de aanval | `rc159-omleiding`, met `smtpServer.host` op die luisteraar, auth aan, wachtwoord `HET-GEHEIM-VAN-HET-PLATFORM` |

Na afloop is alles opgeruimd en staat de sandbox terug zoals hij was: de wijziging in
Forgejo teruggedraaid, het geheim, de ConfigMap, de luisteraar, de proefrealm, de
testgebruiker en het relayaccount verwijderd.

## 1. De provider draait, op de echte deployment

```
KC-SERVICES0047: zad-relay (nl.minbzk.rig.keycloak.email.RelayEmailSenderProviderFactory)
  is implementing the internal SPI emailSender. This SPI is internal and may change without notice
kc.sh start --proxy-headers=forwarded --hostname-strict=false
  --spi-theme-welcome-theme=nl-design-system --spi-email-sender-provider=zad-relay --optimized
ZAD-RELAY 1.0.0: aangezet, relay rig-mail-relay.rig-ron.svc.cluster.local:587 als zad-keycloak,
  afzender noreply-inloggen@rijksoverheid.nl
```

Uitvoer: `rc159-metingen/02-keycloak-spi-aan.log`.

## 2. De aanvalsproef: de luisteraar krijgt NIETS

Op `rc159-omleiding` (`smtpServer.host` naar de lokaas) een bevestigingsmail gestuurd met
`PUT /admin/realms/rc159-omleiding/users/<id>/execute-actions-email` → **HTTP 204**.

**De volledige log van de luisteraar** (`rc159-metingen/01-spi-aan-lokaas-leeg.log`):

```
lokaas luistert op 0.0.0.0:2525 - elke regel komt hier in de log
```

Eén regel, zijn eigen startregel. Geen verbinding, geen EHLO, geen AUTH, geen wachtwoord.

Keycloak logde ondertussen dat hij de map van de realm zag en passeerde:

```
ZAD-RELAY: bericht gaat naar de relay rig-mail-relay.rig-ron.svc.cluster.local:587;
  smtpServer van de realm genegeerd (8 sleutels, eigen host: true)
ZAD-RELAY: bericht aangeboden aan de relay
```

En het bericht kwam in de sink aan, met het merkteken erop
(`rc159-metingen/03-sink-kopregels.json`):

```
X-Zad-Email-Sender: zad-relay/1.0.0
Return-Path:        <noreply-rijksapp+zad-keycloak@rijksoverheid.nl>
From:               noreply-rijksapp@rijksoverheid.nl
Subject:            Update Your Account
```

Het merkteken kan alleen door onze code gezet zijn, en het plusdeel in de `Return-Path` is
de bounce-adressering die de relay voor het account `zad-keycloak` maakt. "De mail kwam aan"
en "onze provider deed het" zijn hier niet te verwarren.

> **Over die `From:`.** Er staat `noreply-rijksapp@` en niet `noreply-inloggen@`. Dat is
> geen fout in deze PR maar de staat van de SANDBOX: het infrastructuurmanifest in de
> Forgejo van dit cluster staat nog op RC-140 en kent het `zad-afzenders`-sieve-script uit
> RC-145 niet, dus de relay valt terug op het afgeleide adres. De sleutels die OPI schrijft
> (`zad.afzender.naam.zad-keycloak` = `Rijksapps`,
> `zad.afzender.adres.zad-keycloak` = `noreply-inloggen@rijksoverheid.nl`) stonden wel
> gewoon op de relay. **Het eigen afzenderadres is op dit cluster dus NIET aangetoond**;
> die keten is gepind met toetsen (`TestHetAfzenderadresPerAccount`) en moet bij de uitrol
> op productie in de sink of de postbus gecontroleerd worden.

## 3. De tegenproef: met de camelCase-vlag lukt de aanval WEL

Zonder deze tegenproef zegt een lege log niets. Dezelfde realm, dezelfde luisteraar, alleen
de vlag in de vorm `--spi-emailSender-provider=zad-relay`. Keycloak start gewoon door, laadt
de fabriek zelfs (`KC-SERVICES0047: zad-relay ...`) - en gebruikt hem niet.

`execute-actions-email` → **HTTP 204**, en de luisteraar
(`rc159-metingen/04-tegenproef-camelcase-lokaas.log`):

```
=== VERBINDING van 10.244.0.156:53944 ===
<<< EHLO keycloak-66dff5f7c7-q8dmc
<<< AUTH LOGIN
!!! AUTH LOGIN gebruikersnaam (b64): YnVpdA== -> 'buit'
!!! AUTH LOGIN WACHTWOORD (b64): SEVULUdFSEVJTS1WQU4tSEVULVBMQVRGT1JN -> 'HET-GEHEIM-VAN-HET-PLATFORM'
<<< MAIL FROM:<aanvaller@example.org>
<<< RCPT TO:<rc159-omleiding@example.org>
<<< DATA
... Subject: Update Your Account
... https://keycloak.sandbox.rijksapp.dev/realms/rc159-omleiding/login-actions/action-token?key=...
```

63 regels, met het wachtwoord in platte tekst **en de action-token**, die een geldige
overnamesleutel voor het account is. Die token is uit de vastgelegde log geredigeerd; de
realm is verwijderd.

**Nul waarschuwingen, nul foutmeldingen.** Dit is waarom de vlagvorm aan twee kanten gepind
is: aan de Java-kant uit `EmailSenderSpi.getName()`, en in
`tests/test_keycloak_relay_email_sender.py` door het manifest naast de Java-bron te leggen.

## 4. De canarie: een niet-bestaand provider-id laat de pod NIET opkomen

Met `--spi-email-sender-provider=bestaat-niet`
(`rc159-metingen/06-canarie-niet-bestaand-provider-id.log`):

```
ERROR: Failed to run 'build' command.
ERROR: io.quarkus.builder.BuildException: Build failure: Build failed due to errors
  [error]: Build step ...KeycloakProcessor#configureKeycloakSessionFactory threw an exception:
           java.lang.RuntimeException: Failed to find provider bestaat-niet for emailSender
  at org.keycloak.quarkus.deployment.KeycloakProcessor.checkProviders(KeycloakProcessor.java:896)
```

De pod herstartte en werd nooit ready; de OUDE pod bleef draaien, dus de rollout ging niet
door en het inloggen bleef werken. Daarmee is de vlag geen bewering meer: **een pod die
opkomt heeft zijn provider aantoonbaar gevonden.**

## 5. De realm na een verwerking

`ensure_realm_self_service` met het blauwdruk `sso-support` op de bestaande projectrealm
`jc-77j-sandboxed-local`, via de echte connector in de OPI-pod:

```
VERSCHIL: {
 "smtpServer":  [ {},   {"from": "noreply-inloggen@rijksoverheid.nl"} ],
 "verifyEmail": [ false, true ]
}
```

**Twee velden en niet één.** Het plan schreef "precies één veld", en dat was geschreven voor
de commit-volgorde van RC-156, waar de `smtpServer` in een eerdere commit landde. Hier landen
ze in dezelfde gang. Het tweede veld is de minimale `smtpServer`: van een LEGE map naar één
sleutel, en die noemt geen bestemming.

Een tweede gang verandert niets (`rc159-metingen/08-realm-tweede-gang-idempotent.txt`):

```
VERSCHIL: {}
verifyEmail: True
smtpServer: {"from": "noreply-inloggen@rijksoverheid.nl"}
host aanwezig: False
```

## 6. Een nieuwe gebruiker bevestigt zijn adres

Via `KeycloakConnector.create_user` op die realm:

```
realm verifyEmail: True
NIEUWE GEBRUIKER emailVerified: False
```

`send-verify-email` → HTTP 204, bericht in de sink met `X-Zad-Email-Sender: zad-relay/1.0.0`
en `Subject: Verify email`. De link uit dat bericht gevolgd (met een cookie-jar, want de
bevestiging loopt over twee verzoeken in één authenticatiesessie):

```
emailVerified NA HET AANKLIKKEN: True
```

`rc159-metingen/09-nieuwe-gebruiker-bevestigt.txt`.

## 7. Het aantal gebruikers met `emailVerified: false`

**Sandbox** (`rc159-metingen/07-emailverified-telling-sandbox.txt`), gemeten VOOR het
omzetten:

| realm | gebruikers | niet geverifieerd |
|---|---:|---:|
| `jc-77j-sandboxed-local` | 0 | 0 |
| `master` | 3 | 1 |
| `operations-manager` | 1 | 0 |
| `rig-platform` | 0 | 0 |
| **totaal** | **4** | **1** |

(De rij `rc159-omleiding` uit de ruwe uitvoer is de proefrealm van deze meting en telt niet
mee.) De ene niet-geverifieerde in `master` is een beheerdersaccount; `master` verifieert
niet en wordt door dit plan niet geraakt.

**Productie: NIET GEMETEN.** Deze sessie heeft geen kubeconfig voor het odcn-cluster. Draai
vóór de uitrol op productie:

```bash
# met een admin-token op keycloak.rijksapp.nl
for realm in $(curl -s "$KC/admin/realms" -H "Authorization: Bearer $TOKEN" | jq -r '.[].realm'); do
  n=$(curl -s "$KC/admin/realms/$realm/users?max=5000" -H "Authorization: Bearer $TOKEN" \
      | jq '[.[] | select(.emailVerified != true)] | length')
  echo "$realm: $n niet geverifieerd"
done
```

Alleen de realms van de blauwdrukken `sso-support` en `algoritmeregister` gaan om.

## Wat hier NIET is gemeten

- **Het eigen afzenderadres van het Keycloak-account.** Zie de kanttekening bij deelvraag 2:
  het infrastructuurmanifest van dit sandboxcluster staat op RC-140 en kent het
  `zad-afzenders`-sieve-script niet. De keten OPI → relay is wel gemeten (de sleutels
  stonden op de relay), de weg relay → `From:` niet.
- **Of projectpost zijn eigen adres houdt.** Zelfde reden.
- **Productie.** Zie deelvraag 7 en de uitrolstappen in het plan.
- **Egress.** Kind dwingt netwerkbeleid niet af, dus dat de lokaas bereikbaar was zegt
  niets over wat op ODCN bereikbaar is. Voor de aanvalsproef is dat gunstig: de aanval was
  hier maximaal makkelijk.
