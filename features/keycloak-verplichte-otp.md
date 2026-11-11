# Verplichte OTP voor lokale accounts

## Wat het is

Een lokaal account in Keycloak logt in met een gebruikersnaam en een wachtwoord. Dat is één
factor, en BIO2 8.05 vraagt sterkere authenticatie voor wie niet via de federatie
binnenkomt. Deze feature voegt die 2-factor toe op twee plekken:

1. een **derde realmblauwdruk**, `sso-support-otp`, die een project zelf kiest;
2. de **master-realm**, waar de platformbeheerders en de beheeraccounts van de
   projectrealms staan. Daar is de OTP altijd verplicht.

Wie via SSO Rijk inlogt merkt er niets van: die komt langs de Identity Provider Redirector
en bereikt het wachtwoordformulier, waar de OTP-stap achter hangt, nooit. De 2-factor
hoort daar bij SSO Rijk.

Service accounts en client-credentials vallen erbuiten. Dat is geen browserlogin, dus de
OTP-stap komt er niet aan te pas en de code slaat `service-account-*` over.

## De drie blauwdrukken

| Template | Inlogscherm | Lokaal account |
|---|---|---|
| `sso-only` | geen, Keycloak stuurt meteen door naar SSO Rijk | geen weg naar binnen |
| `sso-support` | keuze: SSO Rijk of een lokaal account | wachtwoord |
| `sso-support-otp` | zelfde keuze | wachtwoord **en** een code uit een authenticator-app |

Kiezen gaat in het portaal, bij de configuratie van de dienst `keycloak`, of via de API:

```yaml
services:
  - keycloak:
      config:
        template: sso-support-otp
```

`sso-only` en `sso-support` gedragen zich precies zoals voorheen.

## Hoe de flow eruitziet

`sso-support-otp.yaml` erft alles van `sso-support.yaml` en verschilt op één punt: de
browser flow is niet Keycloaks `browser` maar een eigen flow met de OTP-stap op REQUIRED:

```
Browser with required OTP
  Cookie                          ALTERNATIVE
  Identity Provider Redirector    ALTERNATIVE
  <flow> forms                    ALTERNATIVE
    Username Password Form           REQUIRED
    <flow> otp                       REQUIRED
      Condition - user configured       DISABLED
      OTP Form                          REQUIRED
```

In `sso-support` staat die laatste subflow op CONDITIONAL met de voorwaarde op REQUIRED, en
dat is precies het verschil: wie geen OTP had ingesteld kreeg er ook geen gevraagd.

**De flow wordt door OPI gebouwd, niet door de blauwdruk beschreven.** De blauwdruklezer
(`_process_authentication_flows`) kent alleen platte executions met een
`setAsBrowserFlow`-redirector en kan geen geneste subflows aanmaken, en de OTP-stap zit in
een subflow. De blauwdruk noemt dus alleen het alias (`requireOtp: true` plus
`browserFlow:`); `KeycloakConnector.ensure_required_otp_browser_flow` bouwt de structuur,
met dezelfde helpers (`_ensure_subflow`, `_ensure_execution_in_flow`) als de rolpoortflow.
Een tweede flowbouwer naast die van de rolpoort is er niet: beide gebruiken
`_ensure_browser_authenticators`.

### De authorization wall gaat mee

Een project met `restrict-access` krijgt een eigen rolpoortflow die als
`browserFlowOverride` op de client hangt, en een clientoverride wint van de browser flow van
de realm. Die poort draagt daarom dezelfde OTP-eis als de template: voorwaardelijk bij
`sso-support`, verplicht bij `sso-support-otp`. Zonder die doorgifte zou juist de beschermde
client de lossere eis krijgen.

### De uitnodigingsclient draagt de flow van het realm

De publieke uitnodigingsclient (`operations-manager-invites`) hangt in `sso-support` op
`browserFlowOverride: "browser"`, Keycloaks eigen flow, om de rolpoort te omzeilen zodat een
uitnodiging ook zonder rol ingewisseld kan worden. Een clientoverride wint echter OOK van de
`browserFlow` van het realm, en in die ingebouwde flow is de OTP-stap voorwaardelijk. Dat
gaf een publieke client waarop een lokaal account zonder OTP-credential alleen met zijn
wachtwoord een realmsessie kreeg, en de Cookie-stap liet die sessie daarna ook op de
applicatieclient binnen: geen 2-factor. Daarom wijst de override onder
`sso-support-otp` naar dezelfde verplichte-OTP-flow als het realm. De blauwdruklezer maakt
flows voordat hij clients maakt, dus de flow bestaat op dat moment al.

## Overstappen

Bij elke verwerking van een project met `template: sso-support-otp` krijgt elk bestaand
lokaal account in dat realm de required action `CONFIGURE_TOTP`. Overgeslagen worden:
gebruikers met een koppeling naar een identity provider (die komen via SSO Rijk binnen),
gebruikers die al een OTP-credential hebben, en service accounts. Draagt een gebruiker met
zo'n koppeling toch `CONFIGURE_TOTP`, dan haalt dezelfde ronde hem weg. Idempotent: een
tweede ronde schrijft niets.

Terug naar `sso-support` kan ook. De OTP-stap wordt dan weer voorwaardelijk, en elke
blauwdruk zegt expliciet of `CONFIGURE_TOTP` aan een nieuw account wordt gehangen
(`requiredActionDefaults`), zodat de wissel in beide richtingen werkt. Zwijgen zou een
verplichte OTP laten staan nadat het project hem afzette.

Een OTP die al INGESTELD is blijft staan, en zo ook een `CONFIGURE_TOTP` die nog openstaat
op een bestaande gebruiker: wie er een heeft, geeft hem na de wissel nog steeds, want de
voorwaardelijke stap vraagt hem dan. Dat is Keycloaks eigen gedrag en wordt niet
weggehaald.

Een nieuw account krijgt `CONFIGURE_TOTP` niet als default action
(`requiredActionDefaults` staat in alle drie de blauwdrukken op `false`). Keycloak hangt een
default action namelijk ook aan een gebruiker die via SSO Rijk binnenkomt (first broker
login), en die werd zo om een authenticator-app gevraagd. Een nieuw lokaal account stelt zijn
app toch in: een REQUIRED OTP-stap zonder credential laat Keycloak zelf `CONFIGURE_TOTP`
afdwingen.

## De master-realm

Bij elke start van OPI (`KeycloakSetup.ensure_master_otp`, naast `ensure_otp_master_admin`):

- de browser flow van `master` wordt dezelfde verplichte-OTP-flow;
- elke menselijke gebruiker in `master` zonder OTP krijgt `CONFIGURE_TOTP`.

Faalt die stap, dan logt OPI een ERROR en gaat de bootstrap door: een master-realm zonder
2-factor is beter dan een OPI die niet start en geen project meer kan uitrollen.

### Het account waarmee OPI zelf inlogt

`KEYCLOAK_ADMIN_USERNAME` (standaard `admin`) wordt overgeslagen. Een openstaande required
action maakt de password grant onbruikbaar ("Account is not fully set up"), en zonder
`KEYCLOAK_ADMIN_CLIENT_SECRET` is dat precies de grant waarop `create_keycloak_connector`
terugvalt; OPI zou daarna niet meer bij Keycloak kunnen, en de break-glass van het platform
zou weg zijn. Browsertoegang verliest er niets: de flow van het realm staat op REQUIRED, dus
een console-login van dat account vraagt alsnog een OTP-registratie. Het menselijke
beheeraccount met factor is `admin-otp`, dat `ensure_otp_master_admin` aanmaakt.

De beheeraccounts van projectrealms (`{project}_{cluster}_admin`) staan ook in `master`. Die
krijgen hun TOTP al bij het aanmaken als `KEYCLOAK_ENFORCE_ADMIN_OTP` aanstaat (zie
[keycloak-realm-admin-otp.md](keycloak-realm-admin-otp.md)); zo'n account heeft dan een
credential en wordt overgeslagen. Staat die vlag uit, dan krijgt het account hier
`CONFIGURE_TOTP` en stelt het zijn app bij de volgende console-login zelf in.

## Configuratie

| Instelling | Standaard | Wat het doet |
|---|---|---|
| `KEYCLOAK_ENFORCE_MASTER_OTP` | `true` | De verplichte OTP op de master-realm. Op `false` slaat OPI de stap over: er worden geen required actions gezet en de flow wordt niet gebouwd of gebonden. |

De vlag bestaat als weg terug bij een uitrolprobleem op de beheerconsole, zonder
codewijziging: zet `KEYCLOAK_ENFORCE_MASTER_OTP=false` in de configmap van de
operations-manager van dat cluster en herstart de pod. Hij draait de flow niet terug, hij
houdt hem alleen niet in stand; de bestaande binding blijft dan staan tot iemand hem in de
console omzet.

Voor een project is er geen vlag: de keuze IS de template.

## Een telefoon kwijt

Er is geen eigen herstelweg. Het herstel loopt via Keycloak zelf:

- **in een projectrealm**: de realmbeheerder verwijdert in de beheerconsole de
  OTP-credential van de gebruiker (Users > de gebruiker > Credentials). Bij de volgende
  login stelt die een nieuwe authenticator-app in.
- **in `master`**: hetzelfde, door een platformbeheerder.

Een beheeraccount van een projectrealm deelt zijn TOTP-seed via het projectbestand; daarvoor
geldt het herstel uit [keycloak-realm-admin-otp.md](keycloak-realm-admin-otp.md).

## Afhankelijkheden

- Keycloak 25 of hoger (expliciete execution-prioriteiten, zie `_ensure_execution_in_flow`)
- `opi/configs/keycloak/sso-support-otp.yaml`
- `KeycloakConnector.ensure_required_otp_browser_flow` en `ensure_configure_totp_required`
- `opi/services/catalog/keycloak/templates.py`: de namen van de blauwdrukken en de regel
  welke een lokaal account toelaat

## Buiten scope

- De OTP op de ZAD-gebruiker in het portaal: een andere factor, zie
  [otp-op-de-zad-gebruiker.md](otp-op-de-zad-gebruiker.md).
- Zelfregistratie en wachtwoordreset: die staan in alle blauwdrukken uit.
- WebAuthn en passkeys. Dat kan later een vierde template worden.
