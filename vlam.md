# VLAM-gateway: runbook

De werkende opzet op `odcn-production` (project `vlam-wt8`), met een echte RON-bestemming. Plus de
valkuilen die bij een volgende opzet opnieuw bijten.

De onderbouwing (waarom een VPN, waarom headscale, wat er is afgevallen) staat in
`features/futures/vlam-api-vpn-proxy.md`. Dit document is het bouwverslag.

Alles wat over het gebruik van VLAM gaat is verhuisd naar een eigen project, `~/IdeaProjects/vlam`: het gebruikersdocument voor deelnemers, de CISO-samenvatting, het script dat nagaat welke API-routes openstaan, en de scripts om de tunnel te starten en er een agent tegenaan te zetten. Dit document blijft hier, want de valkuilen hieronder gaan over ZAD en niet over VLAM.

**Welk document wanneer.** Er is ook `inrichting.md` in `~/IdeaProjects/vlam`, en die lijkt hierop maar dient iets anders. Dit document is het **bouwverslag**: waarom elke instelling er staat en wat er stukgaat als je hem weglaat. `inrichting.md` is de **momentopname**: hoe het er nu uitziet, wat er onlangs is gewijzigd en wat je moet aanpassen. Ga je iets bouwen of begrijpen, lees dan dit. Wil je weten wat er vandaag draait, lees dan die. Loopt er iets uiteen, dan wint `inrichting.md`, want die wordt bij elke wijziging bijgewerkt.

## Wat we bouwen

```
laptop → https://vlam-api.rijksweb.nl        (DNS via de tunnel)
       → tailnet-IP van de gateway :443
       → tailscaled userspace, TCPForward naar vlam-proxy:8080
       → HAProxy mode tcp, routeert op SNI
       → vlam-api.rijksweb.nl:443  of  chat.rijksweb.nl:443   over RON
```

TLS wordt **niet** getermineerd. HAProxy kopieert bytes, dus de TLS-sessie loopt van de laptop
tot aan de bestemming met diens eigen certificaat. Wij hebben nooit een sleutel en kunnen het
verkeer niet lezen.

**Er zijn sinds RC-142 twee paden, en ze delen niets behalve de RON-koppeling.** Het pad hierboven
is voor MENSEN op een laptop: VPN, geen terminatie, end-to-end TLS. Het tweede pad is voor
WORKLOADS die al in het cluster draaien: een eigen component `vlam-proxy-intern` op poort 8081 dat
plat HTTP aanneemt en zelf de geverifieerde TLS naar VLAM opzet, plus de ZAD-dienst `vlam` die een
afnemer het adres en de netwerkregel geeft. Zie "Component 4" hieronder en
`features/vlam-service.md`. Een afnemer in het cluster heeft dus geen tunnel nodig; wie
versleuteling tot aan VLAM zelf wil, gebruikt het VPN-pad.

## De RON-koppeling

De koppeling zelf, inclusief het gekoppelde IP-blok en de ingresskant, staat in
`docs/ron-koppeling.md`. Hieronder alleen wat deze gateway ervan gebruikt.

Egress naar RON wordt gekozen met een **annotatie op de namespace**, geen label:

```
egress.projectcalico.org/egressGatewayPolicy: rig-ron
```

OPI zet die standaard op `internet`. De handmatige wijziging naar `rig-ron` **overleefde een
refresh**, dus OPI raakt bestaande annotaties niet aan. Tot ZAD dit in het projectbestand kan
uitdrukken, blijft het wel een handmatige stap die je bij een nieuwe namespace moet herhalen.

**`rig-ron` vervangt internet, het komt er niet bij.** Gemeten in de namespace: github HTTP 000,
`chat.rijksweb.nl` HTTP 200. Dat is voor dit project geen bezwaar en zelfs gewenst:

| Bestemming | Via | Werkt met rig-ron |
|---|---|---|
| RON (vlam-api, chat) | egressgateway | ja |
| Keycloak (`keycloak.rijksapp.nl`) | ODCN-router, niet internet | ja, HTTP 200 |
| Database | in-cluster | ja |
| DERP-relay | eigen pod | ja |
| Image pulls | node-niveau, geen pod-egress | onaangetast |

Wie internet **en** RON in dezelfde namespace nodig heeft, moet bij Quattro zijn: de annotatie
neemt één waarde en de documentatie noemt alleen `internet` of `<customer-name>.*`.

## Services op projectniveau

| Service | Waarvoor |
|---|---|
| `publish-on-web` | publieke HTTPS-route voor headscale |
| `postgresql-database` | database van headscale |
| `persistent-storage` | sleutels en unix socket van headscale, **en de state van de gateway** |
| `keycloak` | OIDC-client, inclusief de rolfilter |
| `attachments` | het headscale-configbestand (zie DNS) |

```yaml
account-link: automatic
restrict-access:
  enabled: true
  realm-role: allowed-user
```

De rolfilter wordt **per client** toegepast, niet op de authenticatieflow van de realm. Staat
`restrict-access` niet aan, dan kan iedereen in de project-realm zich als node registreren.

`account-link: automatic` is nodig voor accounts die wij vooraf hebben aangemaakt. Standaard
stuurt Keycloak iemand met een bestaand maar nog niet gekoppeld account langs "Confirm link
existing account" plus verificatie via e-mail of wachtwoord, en een vooraf aangemaakt account
heeft geen wachtwoord. Met deze instelling bouwt OPI een eigen first-broker-login-flow met
`idp-auto-link` en koppelt hij de SSO-identiteit meteen aan het bestaande account. Alleen die ene
tak verandert; nieuwe en al gekoppelde gebruikers merken niets, en de rolfilter blijft gelden.
Redirect-URI's hoef je niet op te geven: `opi/connectors/keycloak.py:483` zet per ingress-host een
wildcard.

## Component 1: headscale

| | |
|---|---|
| image | `ghcr.io/juanfont/headscale:v0.28.0` (niet Docker Hub) |
| ports | inbound `[8080]`, outbound `[443, 5432]` |
| services | `publish-on-web`, `postgresql-database`, `persistent-storage` (`/data`), `keycloak`, `attachments` |
| probe | `scheme: http`, `liveness-path: /health` |
| security | **niets zetten**, ZAD regelt de UID |

`command` is verplicht, want het image heeft geen CMD:

```yaml
command: [/ko-app/headscale, serve]
```

**`aliases`** (afgeleid van wat ZAD injecteert):

```yaml
HEADSCALE_DATABASE_POSTGRES_HOST: ${DATABASE_SERVER_HOST}
HEADSCALE_DATABASE_POSTGRES_PORT: ${DATABASE_SERVER_PORT}
HEADSCALE_DATABASE_POSTGRES_NAME: ${DATABASE_DB}
HEADSCALE_DATABASE_POSTGRES_USER: ${DATABASE_SERVER_USER}
HEADSCALE_DATABASE_POSTGRES_PASS: ${DATABASE_PASSWORD}
HEADSCALE_OIDC_ISSUER: ${OIDC_URL}/realms/${OIDC_REALM}
HEADSCALE_OIDC_CLIENT_ID: ${OIDC_CLIENT_ID}
HEADSCALE_OIDC_CLIENT_SECRET: ${OIDC_CLIENT_SECRET}
```

Op een OPI zonder de alias-scoping-fix moeten `HEADSCALE_SERVER_URL`,
`HEADSCALE_NOISE_PRIVATE_KEY_PATH` en `HEADSCALE_UNIX_SOCKET` **niet** als alias maar als
env-var, zie valkuilen.

**Scalaire instellingen** (plat in `env-vars`, of in `user-env-vars` op een oudere OPI):

```
HEADSCALE_SERVER_URL=https://<hostname>
HEADSCALE_LISTEN_ADDR=0.0.0.0:8080
HEADSCALE_NOISE_PRIVATE_KEY_PATH=/data/noise_private.key
HEADSCALE_UNIX_SOCKET=/data/headscale.sock
HEADSCALE_DATABASE_TYPE=postgres
HEADSCALE_PREFIXES_V4=100.80.0.0/16
HEADSCALE_PREFIXES_V6=fd7a:115c:a1e0::/48
HEADSCALE_DNS_OVERRIDE_LOCAL_DNS=false
HEADSCALE_DNS_BASE_DOMAIN=vlam.internal
HEADSCALE_OIDC_EXPIRY=24h
HEADSCALE_DERP_SERVER_ENABLED=true
HEADSCALE_DERP_SERVER_VERIFY_CLIENTS=true
HEADSCALE_DERP_SERVER_PRIVATE_KEY_PATH=/data/derp_server_private.key
HEADSCALE_DERP_SERVER_STUN_LISTEN_ADDR=0.0.0.0:3478
HEADSCALE_DERP_SERVER_REGION_ID=999
HEADSCALE_DERP_SERVER_REGION_CODE=headscale
HEADSCALE_DERP_SERVER_REGION_NAME=Headscale Embedded DERP
HEADSCALE_DERP_SERVER_AUTOMATICALLY_ADD_EMBEDDED_DERP_REGION=true
```

`HEADSCALE_UNIX_SOCKET` niet vergeten; standaard staat die in `/var/run/`, wat een rootless
container niet kan aanmaken, en dan werken `headscale users list` en `preauthkeys create` niet.

`HEADSCALE_DERP_SERVER_VERIFY_CLIENTS=true` zorgt dat de relay alleen verkeer doorzet voor nodes
die headscale kent. Zonder die instelling kan een willekeurige derde onze relay als doorgeefluik
gebruiken. Dat is misbruik van middelen en een attributieprobleem, geen inbraakrisico: DERP ziet
alleen ciphertext, geeft geen tailnet-lidmaatschap en routeert niet naar RON.

`HEADSCALE_OIDC_EXPIRY` gaat over hoe lang een node die via SSO inlogde geautoriseerd blijft, dus
de SSO-sessie voor mensen. Een preauthkey-node (de gateway) verloopt daar niet van; dat is een
aparte verlooptijd op de sleutel zelf.

## DNS in de tunnel

Dit is de enige plek waar een bestand nodig is. `dns.extra_records` en `dns.nameservers.split`
zijn een lijst van objecten en een map, en die passen niet in een env-var: viper splitst de
waarde op komma's en probeert daar objecten van te maken. Dat is een **harde fout**, headscale
start dan niet.

De namen resolveren niet publiek (NOERROR zonder A-record), dus de tunnel moet ze aanleveren.

Oplossing: een **minimaal** `config.yaml` via de `attachments`-service, met alleen wat een env-var
niet kan. De scalairen blijven env-vars.

Projectniveau:

```yaml
- attachments:
    data:
      - id: headscale-config
        filename: config.yaml
        content: <AGE-versleuteld>
```

Op het component:

```yaml
- attachments:
    config:
      - reference: headscale-config
        provide-as: file
        path: /etc/headscale/config.yaml
```

De inhoud:

```yaml
dns:
  extra_records:
    - name: vlam-api.rijksweb.nl
      type: A
      value: <tailnet-IP van de gateway>
    - name: vlam-api.overheid-i.nl
      type: A
      value: <tailnet-IP van de gateway>
    - name: chat.rijksweb.nl
      type: A
      value: <tailnet-IP van de gateway>
    - name: chat.overheid-i.nl
      type: A
      value: <tailnet-IP van de gateway>
  nameservers:
    split:
      rijksweb.nl:
        - 100.100.100.100
      overheid-i.nl:
        - 100.100.100.100
```

Die split-route is de kern: zonder een route voor die suffix stuurt de client zijn
`rijksweb.nl`-vragen nooit naar de tunnel-resolver en blijven de extra records ongebruikt. Met
`override_local_dns: false` pusht headscale namelijk geen resolvers. Elke zone die je erbij neemt
heeft dus zowel een `extra_records`-regel als een eigen split-route nodig; vergeet je de tweede,
dan lekt de naam naar de publieke DNS en komt er niets terug.

**Dit werkt alleen voor wie de gewone Tailscale-client draait.** Via `bin/vonk` draait tailscaled
in userspace, zet het de DNS van de machine niet om, en zoekt de proxy namen op via de
systeemresolver. Die kent deze namen niet. Daar zijn dus regels in `/etc/hosts` nodig, en doen de
`extra_records` niets. De faalwijze is misleidend: je krijgt `CONNECT tunnel failed, response 500`
en dus geen certificaatfout of iets anders dat de richting wijst. Zie de `README.md` in
`~/IdeaProjects/vlam`.

Controleren of het aankomt, van binnenuit:

```
kubectl exec deploy/productie-vlam-gateway -- tailscale --socket=/tmp/tailscaled.sock dns status
```

Daar hoort te staan:

```
Split DNS Routes:
  - rijksweb.nl  ->  100.100.100.100
```

De gateway is zelf een tailnet-client en krijgt dezelfde configuratie als een laptop, dus dit is
een geldige proxy voor wat een gebruiker ziet.

**Niet mounten binnen `/data`**, dat is het PVC-mountpunt. En een subPath-Secret-mount wordt niet
automatisch bijgewerkt, dus een wijziging komt via een redeploy.

## Component 2: vlam-gateway

| | |
|---|---|
| image | `docker.io/tailscale/tailscale:v1.98.9` |
| ports | inbound geen, outbound `[443]` |
| services | `persistent-storage` (`/data`) |
| probe | `scheme: none` |

```
TS_USERSPACE=true
TS_EXTRA_ARGS=--login-server=https://<hostname>
TS_TAILSCALED_EXTRA_ARGS=--no-logs-no-support
TS_HOSTNAME=vlam-gateway
TS_KUBE_SECRET=
TS_STATE_DIR=/data
```

**`--no-logs-no-support` zet het uploaden van logs naar `log.tailscale.com` uit.** Dat staat bij
tailscaled standaard aan, en je moet er dus actief vanaf. Twee redenen. We willen geen metadata
over deze tunnel naar een externe logdienst sturen, en de pod kan niet bij internet, dus het
mislukt toch. Wat je in de logs zag als het niet uitstaat, eindeloos herhaald:

```
trying bootstrapDNS("derp2e.tailscale.com", ...) for "log.tailscale.com" ...
bootstrapDNS(...) for "log.tailscale.com" error: ... context deadline exceeded
```

Dat is de client die, nadat DNS voor `log.tailscale.com` faalt, terugvalt op een in het binair
meegebakken lijst DERP-servers om die naam alsnog te resolven. Die zijn net zo onbereikbaar. Het
kost niets aan functionaliteit: lokaal loggen naar stderr blijft werken, je verspeelt alleen
technische ondersteuning van Tailscale, en die gebruiken we niet want we draaien onze eigen
coördinatieserver.

De **preauthkey hoort niet in git als platte tekst**. Zet `TS_AUTHKEY` in `user-env-vars`, dan
staat hij AGE-versleuteld. Maak hem herbruikbaar met een ruime looptijd:

```
headscale preauthkeys create --user 1 --reusable --expiration 4380h
```

**`TS_DEST_IP` werkt niet.** Containerboot weigert die in userspace-modus, want dat is
destination-NAT en dat vraagt kernel-modus met `NET_ADMIN`. ZAD dropt alle capabilities, dus
kernel-modus is uitgesloten. Wat wel werkt is een serve-config met `TCPForward`: tailscaled zet
die verbinding zelf op. Geen hostmatching, en het blijft laag 4, dus de TLS-sessie gaat ongebroken
door.

Serve-config wil een bestand en ZAD kan die niet mounten, dus schrijven via `command`:

```yaml
command:
  - /bin/sh
  - -c
  - |
    cat > /tmp/serve.json <<'JSON'
    {
      "TCP": {"443": {"TCPForward": "productie-vlam-proxy:8080"}}
    }
    JSON
    export TS_SERVE_CONFIG=/tmp/serve.json
    exec /usr/local/bin/containerboot
```

Let op: **luisteren op 443, doorzetten naar 8080.** `TCPForward` neemt een volledig `host:poort`,
dus de gateway doet de poortvertaling. Daarmee werkt `https://naam/pad` zonder poortnummer, en is
de losse handmatige 443-Service niet meer nodig.

`TS_STATE_DIR=/data` op de PVC is niet optioneel. Containerboot wil zijn state anders in een
Kubernetes Secret bewaren en vraagt daar RBAC voor, wat ZAD per component niet kan verlenen;
`TS_KUBE_SECRET=` zet dat uit. Zonder persistente opslag verliest de gateway bij elke herstart
zijn identiteit, **krijgt hij een nieuw tailnet-adres** en blijft de oude node als dode entry
achter. Dan verlopen ook de DNS-records bij elke deploy.

Opruimen van dode nodes:

```
headscale nodes delete --identifier <id> --force
```

## Component 3: vlam-proxy

| | |
|---|---|
| image | `docker.io/library/haproxy:lts-alpine` |
| ports | inbound `[8080]`, outbound `[443, 8080]` |

Geen standaard `nginx`: dat image bindt poort 80 en schrijft zijn pid als root.

```yaml
command:
  - /bin/sh
  - -c
  - |
    cat > /tmp/haproxy.cfg <<'CFG'
    global
      maxconn 2000
    defaults
      timeout connect 5s
      timeout client  1m
      timeout server  1m
    frontend ron_in
      bind :8080
      mode tcp
      tcp-request inspect-delay 5s
      acl is_vlam req.ssl_sni -i vlam-api.rijksweb.nl
      acl is_chat req.ssl_sni -i chat.rijksweb.nl
      tcp-request content reject unless is_vlam or is_chat
      use_backend chat_out if is_chat
      default_backend vlam_out
    backend vlam_out
      mode tcp
      server vlam vlam-api.rijksweb.nl:443
    backend chat_out
      mode tcp
      server chat chat.rijksweb.nl:443
    CFG
    exec haproxy -f /tmp/haproxy.cfg -db
```

**Bestemmingen op een allowlist, gekozen op SNI.** De client kiest de bestemming niet; hij kiest
alleen welke van de toegestane namen hij aanroept, en alles daarbuiten wordt geweigerd voordat er
verbinding is. Frontend en backend mogen sinds HAProxy 3.3 niet dezelfde naam hebben.

Het fragment hierboven toont de vorm, niet de huidige inhoud. Sinds 2026-09-01 staan er vier namen
op de lijst: de twee rijksweb-namen en hun overheid-i-tegenhangers, elk met een eigen backend. Elke
naam die je toevoegt heeft drie dingen nodig, en het vergeten van een ervan geeft drie verschillende
storingen: een `acl`, vermelding in de `tcp-request content reject unless`-regel, en een
`use_backend`. Zonder de acl wordt het verkeer geweigerd, zonder de reject-vermelding staat de deur
te ver open, en zonder de use_backend beland je stil op de default. De actuele configuratie staat in
`inrichting.md`.

**`maxconn` is niet optioneel.** Zonder die regel leidt HAProxy zijn maximum af van de fd-limiet
van de container en schaalt hij zijn interne structuren daarop, wat in rust al 196Mi kostte. In
`mode tcp` houdt hij ongeveer 32Kb aan buffers per verbinding aan, dus 2000 verbindingen is zo'n
64Mb. Dat is ruim voor vijftig gelijktijdige gebruikers, en loop je er toch tegenaan dan zie je
wachtende verbindingen in plaats van een OOMKill. Vastgezet op requests 64Mi/25m en limits
256Mi/500m.

## Component 4: vlam-proxy-intern

Voor afnemers BINNEN het cluster (RC-142). Een eigen component naast `vlam-proxy`, niet een tweede
frontend erop: de twee hebben andere gebruikers en een ander wijzigingsritme, en een configuratiefout
in het ene pad mag het andere niet omvertrekken. HAProxy is stateless, dus de prijs is een kleine
pod erbij.

| | |
|---|---|
| image | `docker.io/library/haproxy:lts-alpine` |
| ports | inbound `[8081]`, outbound `[443]` |
| services | `attachments` (de Rijksdienst-CA-keten als bijlage, als bestand gemount) |
| probe | `scheme: http` op een `monitor-uri /healthz` |
| resources | `auto-tune-resources: false`, vast op 64Mi/256Mi |

Het verschil met component 3 is de terminatie. Deze proxy draait `mode http`, zet de TLS naar
`vlam-api.rijksweb.nl` zelf op en VERIFIEERT daarbij het certificaat tegen de meegeleverde
CA-keten:

```
server vlam vlam-api.rijksweb.nl:443 ssl sni str(vlam-api.rijksweb.nl) verify required \
  ca-file /etc/haproxy/rijksdienst-ca.pem resolvers dns init-addr last,libc,none
```

Daarmee is het CA-probleem uit "Valkuilen" een keer opgelost, op de proxy, in plaats van in elke
runtime van elke afnemer. De prijs staat er tegenover en is bewust aanvaard: **deze proxy ziet het
verkeer in platte tekst**. Dat blijft binnen ons eigen cluster en beheer, en de stap naar buiten is
versleuteld en geverifieerd.

Verder overgenomen uit component 3, om dezelfde redenen: `maxconn` expliciet, de `resolvers`-sectie
en `timeout server 10m` (taalmodellen antwoorden traag). De Host-header richting upstream zetten we
zelf (`http-request set-header Host vlam-api.rijksweb.nl`), zodat de binnenkomende Host de
bestemming niet bepaalt. Redirects worden teruggegeven, niet gevolgd.

De RON-egress werkt vanzelf mee: de `rig-ron`-annotatie staat op de NAMESPACE, niet op een
component.

**Sinds 2026-09-01 heeft dit component drie poorten**, niet één. 8081 termineert naar rijksweb,
8082 termineert naar overheid-i, en 8443 lust door zonder te termineren voor afnemers die zelf de
TLS-sessie met VLAM willen opzetten. Het nieuwe adres kreeg een eigen poort in plaats van een
vervanging van 8081, zodat afnemers in eigen tempo kunnen omzetten. De doorlus op 8443 blijft de
bestemming vastzetten via dezelfde SNI-allowlist, dus de eigenschap dat er geen netwerkpad naar RON
bestaat blijft overeind. Details in `inrichting.md`.

Let op dat het adres van overheid-i tijdelijk hardgecodeerd staat in plaats van de naam, omdat
ODC-Noord de DNS-forwardzone heeft moeten terugdraaien. Dat hoort terug zodra die zone er weer is;
de reden staat als comment bij de betreffende regels.

**Toegang.** Eenmalig, en daarna nooit meer per afnemer. In `vlam-wt8` staan
cross-domain-access-regels die de poorten van `vlam-proxy-intern` zonder projectlimiet
openzetten (hieronder de oorspronkelijke voor 8081; er staan er nu drie, ook voor 8082 en 8443):

```yaml
  - name: cross-domain-access
    schema-version: "1.1"
    config:
      inbound:
        - name: iedereen-in-het-cluster
          from: { project: "*" }        # geen projectlimiet, alleen deze poort
          to: { component: vlam-proxy-intern, port: 8081 }
```

Een afnemer heeft daarna genoeg aan de ZAD-dienst `vlam` (uitgaande regel plus
`VLAM_API_URL`). De autorisatie zit bij VLAM zelf, op de API-sleutel; de netwerkregel regelt
alleen nog de bereikbaarheid. De wildcard geldt alleen inkomend, alleen op die ene poort van
dat ene component, en `deployment`/`component` moeten er leeg blijven -- het model weigert
een wildcard die er toch een noemt.

**CA-rotatie.** De keten zit in een `subPath`-mount, en die wordt NOOIT vanzelf ververst: de pod
draait stil door op de oude inhoud, zonder foutmelding. Roteren is dus bijlage vervangen EN alleen
`vlam-proxy-intern` opnieuw uitrollen. Noteer bij het vervangen de vervaldatum van de nieuwe keten,
anders is de eerstvolgende storing een certificaat dat stilletjes verliep.

## Verificatie

De test die de hele keten dekt, vanaf een laptop:

```
curl -v https://chat.rijksweb.nl/health
  → HTTP 200, certificaatvalidatie geslaagd
```

Die geslaagde validatie is het bewijs dat de TLS-sessie ongebroken doorloopt, want dat certificaat
komt van chat zelf.

De negatieve test, minstens zo belangrijk:

```
curl -v https://google.com:8080/ --resolve google.com:8080:<tailnet-IP>
  → TLS connect error
```

De proxy weigert alles wat niet op de allowlist staat. Hij is geen doorgeefluik.

Verder te controleren: de RON-egress via de namespace-annotatie, de Keycloak-login met rolfilter
inclusief een account **zonder** de rol (moet geweigerd worden), de split-DNS die bij clients
aankomt, en een gateway-adres dat een herstart overleeft.

Voor het interne pad, vanuit een pod in `rig-prd-vlam-wt8`:

```
wget -qO- http://productie-vlam-proxy-intern:8081/v1/models
  → de modellenlijst van VLAM
```

En de negatieve test die erbij hoort: dezelfde aanroep met een afwijkende `Host`-header geeft
hetzelfde antwoord. De binnenkomende Host bepaalt de bestemming dus niet.

## Valkuilen

**Het certificaat van vlam-api komt van een interne CA.** Uitgever is `Rijksdienst Issuing CA2`
van SSC-ICT, en die zit in geen publieke bundel. Gemeten: met validatie `verify=19`, met `-k`
HTTP 200. Onze passthrough raakt dat certificaat niet aan en kan dat ook niet. Gebruikers moeten
die root vertrouwen; op beheerde laptops zit hij erin, op onbeheerde niet. Dat is een regel in de
gebruikersinstructie, en een handige splitsing bij support: faalt het op `verify`, dan is het de
CA; faalt het op `connect`, dan zijn wij het.

**Een tweede VPN die DNS globaal overneemt sloopt de split-route.** OpenVPN Connect had `dhcp-option DNS 10.200.1.2` in het profiel staan en maakte dat de
globale resolver van de Mac. Tailscale plaatst zijn route juist *scoped*, en verliest dan.

Het herkenbare symptoom: `nslookup naam` **werkt** (die leest `/etc/resolv.conf`, waar tailscale
in staat) en `curl naam` zegt `Could not resolve host` (die gebruikt `getaddrinfo`, en dus de
systeemconfiguratie waar de andere VPN de baas is). Eerste diagnosestap:

```
scutil --dns | grep -E "resolver #|domain|nameserver\[0\]"
```

Oplossing aan de clientkant, in het `.ovpn`-profiel:

```
#dhcp-option DNS 10.200.1.2
pull-filter ignore "dhcp-option DNS"
```

De ODCN-namen (`cluster-api.apps...quattro.rijksapps.nl`, de router) resolveren publiek, dus die
DNS is niet nodig; alleen de routes zijn dat. Moet je hem toch terug, dan scoped in plaats van
globaal via `/etc/resolver/<domein>`.

Voor de doelgroep speelt dit niet: een ontwikkelaar op een onbeheerde laptop heeft geen tweede
VPN naar ODCN. Maar wie er wél een van zijn eigen organisatie heeft, loopt hier tegenaan, en het
faalt onduidelijk. Dit hoort in de gebruikersdocumentatie.

**HAProxy lost een backend-hostnaam eenmalig op, bij het starten.** Zonder `resolvers`-sectie blijft hij dat ene adres gebruiken zolang de pod draait. Toen VLAM van IP wisselde stuurden wij het verkeer een dag lang naar het oude adres, en dat gaf `HTTP 500` met een LiteLLM-fout die de modelnaam netjes bij naam noemde. Dat leest als een storing bij de leverancier, en we stonden op het punt die bij hen te melden.

Wat het extra verraderlijk maakte: het oude adres was `chat.rijksweb.nl`. Onze eigen tweede bestemming dus. Die draait dezelfde software met dezelfde modelnamen erin, en gaf daarom een geloofwaardige maar verkeerde fout in plaats van een duidelijke afwijzing.

```
resolvers dns
  parse-resolv-conf
  hold valid 30s
  hold other 30s
  hold refused 30s
  hold nx 30s
  hold timeout 30s

backend vlam_out
  server vlam vlam-api.rijksweb.nl:443 resolvers dns init-addr last,libc,none
```

`init-addr last,libc,none` zorgt dat HAProxy ook opstart als DNS even niet meewerkt, in plaats van te weigeren te starten.

De diagnose die dit blootlegde is algemener bruikbaar: doe dezelfde aanroep één keer door de keten en één keer rechtstreeks vanuit de proxy-pod. Krijg je twee verschillende antwoorden op hetzelfde verzoek, dan praat je met twee verschillende bestemmingen. Vergelijk daarna wat DNS nu teruggeeft met waar de verbindingen feitelijk heen gaan:

```
kubectl exec deployment/productie-vlam-proxy -- /bin/sh -c 'netstat -tn | grep :443; nslookup vlam-api.rijksweb.nl'
```

**`timeout server` moet ruim staan voor taalmodellen.** Stond op `1m`, wat een model dat langer nadenkt middenin zijn antwoord afkapt. Nu `10m`. Dit had zich pas gewroken bij het eerste echte gebruik, en dan als een willekeurig afgebroken verbinding.

**Zonder ACL-beleid deelt headscale `FilterAllowAll` uit.** Dit is de belangrijkste valkuil van
allemaal, want je ziet er niets van: alles werkt, en ondertussen kan elke deelnemer bij elke
andere deelnemer op alle poorten. Met een groep ontwikkelaars op onbeheerde laptops is dat een
gedeeld netwerk waarin iedereen bij iedereen kan. In `hscontrol/policy/v2/filter.go`:

```go
if pol == nil || pol.ACLs == nil {
    return tailcfg.FilterAllowAll, nil
}
```

Eén regel beleid is genoeg. Die gaat als tweede bijlage mee via de `attachments`-service, naast
`config.yaml`, en wordt aangezet met `HEADSCALE_POLICY_MODE=file` en `HEADSCALE_POLICY_PATH`:

```json
{
  "hosts": { "gateway": "100.80.0.1/32" },
  "acls": [
    { "action": "accept", "src": ["*"], "dst": ["gateway:443"] }
  ]
}
```

Alles wat er niet in staat wordt geweigerd, en headscale snoeit bovendien de netmap per node, dus
deelnemers zien elkaar ook niet meer in `tailscale status`.

**Rol het in twee stappen uit.** In `file`-modus laadt headscale het beleid bij het opstarten, en
een ongeldig bestand betekent geen coördinatieserver meer en dus niemand die er nog bij kan, jij
incluis. Koppel het bestand eerst aan zonder `HEADSCALE_POLICY_PATH` te zetten, laat headscale
zelf oordelen, en zet het pas daarna aan:

```
headscale policy check -f /etc/headscale/acl.json
  → Policy is valid
```

Controleer na activering niet of het beleid *geladen* is maar of het *aankomt*, want dat is iets
anders. Het pakketfilter in de netmap van een node is de grond van waarheid:

```
tailscale --socket=/tmp/tailscaled.sock debug netmap
  → PacketFilter: Dsts 100.80.0.1/32 poort 443, Srcs 0.0.0.0/0
```

**De publieke hostnaam staat op twee plekken en beide moeten mee.** `HEADSCALE_SERVER_URL` staat
op `https://${PUBLIC_HOSTNAME}` en volgt de `subdomain` van de deployment automatisch, maar de
gateway is zelf óók een tailscale-client en heeft de naam letterlijk in zijn eigen instellingen
staan:

```
TS_EXTRA_ARGS=--login-server=https://<hostname>
```

Vergeet je die bij een omdoping, dan komt de gateway in een herstartlus met
`fetch control key: ... failed to resolve <oude naam>`, gevolgd door
`failed to auth tailscale: tailscale up failed: signal: killed`. Het verraderlijke is wat
gebruikers zien: DNS blijft werken uit de gecachte netmap, dus `curl` resolveert netjes naar het
gateway-adres en blijft daarna hangen op `Trying`. Bij een omdoping moet iedere client ook opnieuw
`tailscale up --login-server=<nieuwe naam>` doen, want de oude URL zit in zijn lokale staat.

Wat níet mee hoeft: `HEADSCALE_DNS_BASE_DOMAIN`. Dat is het MagicDNS-achtervoegsel binnen de
tunnel, staat niet in publieke DNS en niet in de certificate transparency logs, en is alleen
zichtbaar voor wie al is ingelogd.

De nodesleutel van de gateway staat op de PVC in `/data` en de database van headscale blijft
staan, dus na de omdoping meldt hij zich als dezelfde node en houdt hij zijn adres. Dat is nodig,
want de records in `config.yaml` wijzen naar dat adres. Gemeten na de omdoping naar
`vonk.rijksapp.dev`: node 6 nog steeds op `100.80.0.1`.

**Nodes heten `invalid-<willekeurig>` als de hostnaam van de laptop niet DNS-veilig is.** Een Mac
heet standaard iets als `MacBook Pro van Robbert`, en headscale weigert dat:

```
WRN Rejecting invalid hostname update from hostinfo
    error="hostname \"macbook pro van robbert\" contains invalid characters,
    only lowercase letters, numbers, hyphens and dots are allowed"
```

Dit is cosmetisch en niet functioneel: de node krijgt een adres en werkt. Het raakt ook de
administratie niet, want headscale koppelt elke node aan de OIDC-gebruiker. In
`headscale nodes list` staat naast het onleesbare `invalid-iujlqapo` het e-mailadres uit SSO Rijk,
dus toegang intrekken per persoon kan gewoon.

Headscale *heeft* saneringslogica, maar niet op het pad dat hier langskomt. `NormaliseHostname` in
`hscontrol/util/dns.go` maakt kleine letters, strookt ongeldige tekens en kapt af op 63 tekens, en
die wordt gebruikt bij registratie. Een hostnaam die daarna via hostinfo wordt *bijgewerkt* gaat
langs strikte validatie en wordt geweigerd, waarna de node zijn bestaande naam houdt. Zit een node
dus eenmaal op `invalid-*`, dan komt hij daar niet meer vanaf door de naam van de laptop te
wijzigen: elke reauth biedt hem opnieuw aan en krijgt opnieuw nul op het rekest. Een
configuratieoptie hiervoor bestaat niet, `config-example.yaml` van v0.28.0 kent niets over
hostnaambehandeling.

Twee manieren om het wel goed te krijgen. De gebruiker geeft een DNS-veilige naam mee, en die
passeert de strikte validatie wel:

```
tailscale up --login-server=https://<hostname> --hostname=voornaam-machine
```

Of jij doet het achteraf zelf met `headscale nodes rename -i <id> <naam>`.

**Kies een publieke naam die niets zegt.** Elke uitgegeven hostnaam staat in de certificate
transparency logs en is dus openbaar. Een functiewoord (`relay`, `vpn`, `gateway`, `agent`)
vertelt een scanner wat hij moet proberen, en de naam van het achterliggende systeem vertelt hem
waar het over gaat. Vandaar `vonk.rijksapp.dev`. Dat is geen beveiliging, alleen niet adverteren:
de bescherming zit in SSO Rijk plus de rol `allowed-user`, en achter de tunnel in de SNI-allowlist.

**Headscale past zonder configbestand géén enkele default toe.** Niet de prefixes, niet
`base_domain`, niet de DERP-instellingen, ook al staan die in `config-example.yaml`. Neem het
env-var-blok integraal over in plaats van fout voor fout te ontdekken.

**`configtest` leest geen env-vars.** Alleen `serve` doet dat. Als probe om te kijken of een
instelling aankomt is `configtest` dus ongeschikt; hij klaagt over precies de velden die je als
env-var hebt meegegeven.

**Het headscale-image is een ko-build zonder shell.** De truc waarmee we bij haproxy en tailscale
een bestand wegschrijven werkt daar niet, en `kubectl cp` ook niet (geen `tar`). Vandaar de
attachments-route.

**De alias-scoping-bug.** Op een OPI zonder de fix van 2026-07-28 worden deployment-brede
direct-aliases geresolved tegen de context van elk component. Een component zonder
`publish-on-web` of storage krijgt dan een lege context en de verwerking breekt af met
`Variable references not found in context: PUBLIC_HOSTNAME`. Omweg: zet de drie direct-sourced
waarden als env-var in plaats van als alias.

**Env-vars gaan per stuk.** Plak geen blok tekst in een waardeveld; dan krijg je één variabele met
die hele tekst erin. Het gevolg is een waarde als
`0.0.0.0:8080 HEADSCALE_DATABASE_TYPE = postgres ...`, en dan wijzen alle foutmeldingen daarna de
verkeerde kant op.

**Een portal-save kan een gelijktijdige git-wijziging overschrijven** op een OPI zonder de
compare-and-swap-fix. Werk niet tegelijk in de portal en in git aan hetzelfde project.

**NetworkPolicies worden afgedwongen, ook in de sandbox.** Een pod zonder het
`deployment=<naam>`-label krijgt time-outs, met dat label HTTP 200. Gemeten, niet afgeleid. Een
ontbrekende `ports.outbound` laat zich dus zien als een time-out en niet als een foutmelding.

**`security` op het component niet zetten.** ZAD zet zelf `runAsNonRoot` met UID, GID en fsGroup,
en dat overschrijft de root-user uit een image.

**Zet `auto-tune-resources: false` op deze componenten.** De tuner heeft headscale een keer op
`request == limit == 25Mi` gezet, afgeleid van één meting van 15Mi op een lege, net gestarte
instantie. De eerste keer dat er iets gebeurde volgde een OOMKill, en daarna een cirkel: gekilde
pod, clients die harder pollen, opnieuw gekild. Van buiten zag je alleen `HTTP 503`. Richtlijn hier:
headscale 128Mi/512Mi, en let ook op de proxy, want HAProxy alloceert buffers per verbinding en
klimt onder gebruik ruim boven de 100Mi. Zie `plans/oom-auto-tune-deployment-scoped.md`.

Staat nu uit op headscale en de proxy. Op `vlam-gateway` staat hij nog aan, met een door de tuner
gezette limiet van 68Mi. Dat is dezelfde vorm die headscale de kop kostte.

## Wat er op ODCN anders gaat dan in de sandbox

**De SCC kent de UID toe.** Per-component security-overrides worden genegeerd; admission
injecteert een UID uit het namespace-bereik.

**Sockets wijken uit naar `/tmp`.** UID uit het namespace-bereik mag niet in `/var/run/`
schrijven. De tailscale-CLI in de pod heeft daarom `--socket=/tmp/tailscaled.sock` nodig.

**`timeout-tunnel` op de Route.** De ingress-template zet `timeout: 300s` maar geen
`timeout-tunnel`. Voor de control-verbinding volstaat dat; loopt DERP-verkeer over dezelfde Route,
dan is `haproxy.router.openshift.io/timeout-tunnel` wel nodig.

**De egress-annotatie is handwerk.** Zie de RON-sectie.

## Instructie voor gebruikers

```
tailscale up --login-server=https://vonk.rijksapp.dev
```

Er opent een browser, inloggen met SSO Rijk, klaar. Daarna werkt `https://vlam-api.rijksweb.nl`
zonder verdere ingrepen: geen hosts-regel, geen poortnummer, geen `--resolve`.

`--accept-routes` is **niet** nodig; wij adverteren geen subnet-routes. `--force-reauth` alleen als
iemand een bestaande sessie omzet.

Vier dingen die erbij horen:

- **De rol `allowed-user` in onze Keycloak is vereist.** Zonder die rol mislukt het inloggen, en
  dat is de bedoeling.
- **Voor `vlam-api` moet de Rijksdienst-CA vertrouwd zijn**, zie valkuilen. Zonder die root volgt
  een certificaatfout, geen verbindingsfout.
- **Wie al Tailscale gebruikt, raakt zijn eigen tailnet kwijt.** De officiële client kan maar één
  tailnet tegelijk; `tailscale switch --list` toont de profielen om terug te gaan. Er zijn
  third-party clients die meerdere tailnets tegelijk kunnen, maar die zijn bewust buiten scope: dan
  vraag je gebruikers extra netwerksoftware te vertrouwen.
- **DNS accepteren moet aan staan** (standaard zo). Controleren met `tailscale dns status`; daar
  hoort `rijksweb.nl -> 100.100.100.100` bij de split-routes te staan.

## Open

- [ ] `egress.projectcalico.org/egressGatewayPolicy` in het projectbestand kunnen zetten, of een
      service die dat regelt (nu handmatig per namespace)
- [ ] `config-files` op een component in ZAD, zodat de `command`-heredocs voor haproxy en de
      serve-config weg kunnen
- [ ] Het juiste pad op `vlam-api.rijksweb.nl` bij VLAM navragen; `/health` bestaat daar niet
- [ ] Instructie voor gebruikers: de Rijksdienst-CA vertrouwen, en wat te doen bij een tweede VPN
- [ ] Doet de MetalLB-pool op ODCN UDP? Dan kan tailscale directe verbindingen maken in plaats
      van alles via de relay
