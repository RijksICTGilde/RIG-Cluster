# Uitwijk als SSO Rijk uitvalt

Als SSO Rijk uitvalt kan niemand meer nieuw inloggen met SSO Rijk. Elke projectrealm met SSO
stuurt zijn gebruikers via `rig-platform-oidc` naar `rig-platform`, en de browser-flow van
`rig-platform` stuurt iedereen zonder sessie door naar SSO Rijk. Op 22 oktober 2026 gebeurde
dat, en op 16 november 2026 weer.

Deze feature is de uitwijk: een **storingsstand** die een platformbeheerder in ZAD aanzet, en
waarin een bestaande gebruiker kan inloggen met een **code en een link per mail**.

ZAD zelf logt in via zijn eigen realm `operations-manager` (blauwdruk
`operations-manager-realm.yaml`, die `sso-support` uitbreidt). Die realm heeft naast de knop
naar SSO Rijk een wachtwoordformulier voor lokale accounts. Een platformbeheerder met een
lokaal account komt daarmee bij de beheerpagina, ook als SSO Rijk plat ligt. Dat account heeft
een geverifieerd mailadres nodig dat in ZAD platformbeheerder is.

Het is een tijdelijke workaround voor een storing, geen tweede manier van inloggen die altijd
openstaat.

## De twee stukken

```
Normale stand                          Storingsstand
rig-platform browserFlow =             rig-platform browserFlow =
"External IDP Redirector"              "SSO Rijk storing"
   |                                      |
   +-- auth-cookie                        +-- auth-cookie
   +-- identity-provider-redirector       +-- zad-magic-link-authenticator
       -> meteen naar SSO Rijk                -> keuzescherm: e-mail, of toch SSO Rijk
```

Beide flows staan permanent in de realm; de blauwdruk (`opi/configs/keycloak/bootstrap.yaml`
en de sandbox- en local-varianten) maakt ze aan. Omzetten is één veld: `browserFlow` van de
realm. Dat doe je op de beheerpagina.

Het draaiboek voor het moment zelf (eerst vaststellen hoe het stuk is, dan pas omzetten) staat
in `docs/operations/sso-rijk-is-stuk.md`.

## De storingsstand aan- en uitzetten

**Beheer > Platformacties** (`/admin/platform`), achter een platformbeheerder plus een verse
step-up met de OTP van ZAD.

De pagina staat achter de feature flag **`SSO_OUTAGE_ENABLED`**, standaard uit. Uit betekent:
geen menu-item, en `/admin/platform` en het omzetten antwoorden 404 nog voor de login. Alleen
de sandbox zet hem aan (`overlays/sandboxed-local/configmap.yaml`). De blauwdruk maakt de flow
"SSO Rijk storing" ook zonder flag aan; hij is dan alleen niet vanuit ZAD om te zetten.

De pagina **leest de stand uit Keycloak** en houdt geen kopie in ZAD. Zet iemand de
`browserFlow` in de beheerconsole van Keycloak om, dan staat dat hier ook. Draagt de realm een
flow die het platform niet kent, dan zegt de pagina dat en zet omzetten hem op een van de twee
bekende flows.

Omzetten geldt **alleen voor nieuwe logins**. Sessies die lopen blijven doorlopen, ook nadat de
storingsstand weer uit staat. Elke wijziging komt in het auditlog
(`features/audit.md`), met wie, wanneer en welke kant op.

## Wat de gebruiker ziet

In de storingsstand komt wie geen sessie heeft op een keuzescherm:

1. Bovenaan een melding dat SSO Rijk een storing heeft.
2. Een veld voor het e-mailadres, met de knop "Stuur me een code".
3. Onder het formulier de knop "Toch inloggen met SSO Rijk". Die blijft staan, want op
   22 oktober werkte SSO Rijk vanuit Citrix nog wel.

Na het versturen volgt het scherm met de code. Dat scherm is **hetzelfde voor een bekend en een
onbekend adres** ("Als dit adres bij ons bekend is, hebben we een code gestuurd"), zodat het
formulier geen manier is om uit te zoeken wie hier een account heeft.

Een onbekend adres krijgt daarbij ook dezelfde TOESTAND: er komt een code klaar te staan die
nergens bezorgd wordt, zodat ook een ingetypte code hetzelfde antwoord geeft als bij een bekend
adres. Hetzelfde geldt voor de twee takken die GEEN mail versturen, een opgebruikte
verzendlimiet en een mislukte verzending: die tonen ook het codescherm zonder melding. Een
gebruiker die echt bestaat en zijn limiet op heeft, ziet dus niet waarom er geen tweede mail
komt; zijn eerste code werkt nog, en die staat ook nog in het codescherm. Valt de mailrelay uit,
dan is de storing alleen in de log en in het event-log van de realm te zien, niet op het scherm.

Dat is met opzet zo: de melding bij een opgebruikte verzendlimiet sloeg aan op de TWEEDE
aanvraag binnen 30 seconden, niet op de zesde, en kostte dus één mail en een seconde per adres.
Daarmee was het formulier een werkend middel om uit te zoeken wie hier een account met een
gekoppelde SSO Rijk-identiteit heeft, en dat is gericht phishingmateriaal.

**Wat als verschil overblijft** is het aantal pogingen op een openstaande code. Een bekend adres
dat wel mag versturen krijgt een nieuwe code met 2 nieuwe pogingen; een onbekend adres houdt de
openstaande decoy-code met de pogingen die daar nog op staan. Dat verschil is pas te meten met
twee echte mails naar de houder van dat adres, meer dan 30 seconden na elkaar, en het staat
onder de limiet van 5 mails per kwartier per gebruiker. Luid en zelfbegrenzend, en daarmee geen
werkbare manier om een lijst adressen af te lopen.

De mail bevat de code en een link. De code typ je over; de link logt in op het apparaat waar je
hem opent. Het openen van die link logt **niet** in: er komt een pagina met één knop "Inloggen",
en pas de POST daarvan logt in. Dat is omdat mailscanners (Safe Links en dergelijke) links
vooraf openen, en een link die op een GET inlogt is verbruikt voordat de gebruiker hem ziet.

Die POST gaat naar de **browser-flow**, niet terug naar de link:
`/login-actions/action-token` neemt in Keycloak alleen een GET en een HEAD, dus een POST daar
antwoordt 405. De action-token-handler onthoudt daarom alleen dat er een geldige link is
geopend en geeft de browser terug aan de flow; de authenticator toont de knop en handelt de
POST af. De 5 minuten en de eenmaligheid gelden ook tussen het openen en het klikken.

## De code

| | |
|---|---|
| Vorm | 6 tekens, letters en cijfers uit `23456789ABCDEFGHJKMNPQRSTUVWXYZ` |
| Invoer | hoofdletters maken niet uit, spaties en streepjes worden genegeerd |
| Geldig | 5 minuten |
| Eenmalig | na gebruik werken code en link geen van beide meer |
| Pogingen | 2 foute codes, daarna vervalt de code |
| Gebonden | een code werkt alleen in de inlogpoging die hem aanvroeg |
| Verzendlimiet | hoogstens 1 mail per 30 seconden en 5 per kwartier, per gebruiker |

`0`, `O`, `1`, `I` en `L` zitten niet in het alfabet: de code wordt met de hand overgetypt.

**De grens op gokken zijn de 2 pogingen en de verzendlimiet, niet de brute force-bescherming
van de realm.** Die bescherming sluit een GEBRUIKER uit, en Keycloak zoekt die op via
`authSession.getAuthenticatedUser()` of de auth-note `ATTEMPTED_USERNAME`; de authenticator zet
geen van beide, want een foute code hoort nog bij geen gebruiker. Dat is bewust: wie een
mailadres kent zou anders dat account kunnen laten uitsluiten door codes te gokken, precies
tijdens een storing waarin het account nodig is. Samen laten de twee limieten hoogstens tien
gokken per kwartier toe op 31^6 mogelijkheden.

Van de code wordt alleen een SHA-256 bewaard, in de notes van de inlogpoging. De code zelf
staat in geen log en in geen event. De link draagt diezelfde hash mee, zodat link en code samen
vervallen: een gebruikte code, een nieuw aangevraagde code en een verlopen code laten de link
wijzen naar een hash die niet meer de openstaande is.

## Wie een code kan aanvragen

Alleen een **bestaande, ingeschakelde gebruiker in `rig-platform` met een gekoppelde
`sso-rijk`-identiteit**. Er wordt nooit een account aangemaakt. Een lokaal beheerdersaccount
heeft die koppeling niet en kan dus nooit met een magic link zijn wachtwoord en OTP omzeilen.

## Wat open of schrijfbaar wordt, en de guard erop

| Nieuw | Guard |
|---|---|
| Inloggen zonder SSO Rijk, via een mailbox (storingsstand) | Alleen aan na een actie van een platformbeheerder met verse step-up. Alleen voor gebruikers met een gekoppelde `sso-rijk`-identiteit. 5 minuten geldig, eenmalig, 2 pogingen per code, gebonden aan de inlogpoging, verzendlimiet per gebruiker. |
| `browserFlow` van `rig-platform` schrijfbaar vanuit OPI | Alleen vanaf `/admin/platform`, achter `require_platform_admin`. Alleen de twee bekende flows; een flow-alias komt nooit uit een verzoek. |
| De meldtekst op het inlogscherm | Vaste tekst in de berichtenbundel van het thema, in het Nederlands en het Engels. Er is dus niets schrijfbaar en niets te escapen. |
| De reconcile laat `browserFlow` van `rig-platform` staan | Wie `manage-realm` op `rig-platform` heeft kan de binding zelf omzetten en de reconcile repareert dat niet meer. Dat recht hebben alleen OPI (het service-account `operations-manager`) en de master-beheerders. |

## Hoe de reconcile de binding laat staan

De blauwdruk maakt beide flows aan bij elke verwerking, maar bindt `browserFlow` alleen als de
realm nog **Keycloaks eigen** flow (`browser`) draagt. Dat is de vlag `bindOnlyIfDefault: true`
op het flow-item in `bootstrap.yaml`; `KeycloakYamlHandler._may_bind_browser_flow` leest hem.

Zonder dat zou de volgende verwerking de storingsstand stilletjes terugzetten:
`keycloak_yaml_handler` bond de flow tot RC-496 bij **elke** verwerking opnieuw.

Een projectrealm die zijn browser-flow vandaag bij elke verwerking terugkrijgt, houdt dat
gedrag: de vlag staat alleen op de platformblauwdrukken.

## Waar het staat

| Wat | Waar |
|---|---|
| De authenticator, het action token en de code | `images/keycloak-zad/extensions/custom-mapper/src/main/java/nl/minbzk/rig/keycloak/magiclink/` |
| De inlogpagina's en de mail | `images/keycloak-zad/theme/nl-design-system/login/zad-magic-link*.ftl`, `.../email/` |
| De teksten | `.../login/messages/messages_{nl,en}.properties` en `.../email/messages/` |
| De twee flows | `opi/configs/keycloak/bootstrap.yaml` (plus `-sandbox` en `-local`) |
| De flow bouwen | `KeycloakConnector.ensure_magic_link_browser_flow` |
| De stand lezen en omzetten | `opi/services/sso_outage.py` |
| De beheerpagina | `opi/web/router_platform_admin.py`, `opi/templates_lotc/bg/admin-platform.html.j2` |

De mail loopt via de eigen verzender van het platform, die de relay uit de omgeving van de pod
leest; zie `features/keycloak-mail.md`. `rig-platform` draagt daarvoor de minimale
`smtpServer`-map met alleen een `from`, net als elke andere realm.

**`rig-platform` draagt `emailTheme: nl-design-system`.** Keycloak zoekt `zad-magic-link.ftl`
in het mailthema van de realm, dus zonder dat veld gaat er geen mail uit: het standaardthema
`keycloak` heeft dat sjabloon niet. De blauwdruk noemt het veld en OPI schrijft het
(`_BLUEPRINT_REALM_FIELDS`). De mails van Keycloak zelf houden hun teksten, want het mailthema
erft van `base`, waar elke sleutel staat, en `keycloak/email` voegt daar niets aan toe dan zijn
eigen themabeschrijving. Een projectrealm noemt `emailTheme: keycloak` en verandert dus niet.

Het image moet na een merge gebouwd en per omgeving gepind worden voordat de storingsstand
werkt: zie `features/keycloak-eigen-image.md` en `docs/operations/uitrollen.md`. Zonder dat
bestaat `zad-magic-link-authenticator` niet in Keycloak en kan de flow niet worden gebouwd.

## Buiten deze feature

- Het betrouwbaarheidsniveau in het token (`acr`), langere sessies en meerdere replica's van
  Keycloak.
- Een API-endpoint voor de schakelaar. De API-ingang hangt aan een SSO-token van `rig-platform`
  en voegt daarom niets toe als uitwijk.
- **Een risico dat buiten dit platform ligt:** of mail in Citrix of op de telefoon zelf van SSO
  Rijk afhangt, is niet gevraagd aan werkplekbeheer. Is dat zo, dan vallen storing en uitwijk
  samen en helpt de magic link niet; een lokaal account in `operations-manager` dan wel.
