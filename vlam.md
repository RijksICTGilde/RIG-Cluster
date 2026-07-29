# VLAM-gateway: runbook

Werkende opzet van de proefopstelling in de sandbox (`vlam-wt8`, 2026-07-28), plus de valkuilen
die we onderweg zijn tegengekomen en wat er op ODCN anders gaat.

De onderbouwing (waarom een VPN, waarom headscale, wat er is afgevallen) staat in
`features/futures/vlam-api-vpn-proxy.md`. Dit document is het bouwverslag.

## Wat we bouwen

```
client → https://vlam-api.rijksweb.nl:443
       → (DNS gepusht via de tunnel) tailnet-IP van de gateway :443
       → tailscaled userspace, doorzetten naar vlam-proxy
       → HAProxy mode tcp, luistert op 8080
       → de echte VLAM-API :443
```

TLS wordt **niet** getermineerd. HAProxy kopieert bytes, dus de TLS-sessie loopt van de laptop
tot aan VLAM met VLAM's eigen certificaat. Wij hebben nooit een sleutel en kunnen het verkeer
niet lezen.

In de proefopstelling staat de HAProxy-upstream op **headscale zelf**, dat speelt voor nep-VLAM.

## Services op projectniveau

| Service | Waarvoor |
|---|---|
| `publish-on-web` | publieke HTTPS-route voor headscale |
| `postgresql-database` | database van headscale |
| `persistent-storage` | sleutels en unix socket, mount op `/data` |
| `keycloak` | OIDC-client, inclusief de rolfilter |

```yaml
restrict-access:
  enabled: true
  realm-role: allowed-user
```

Dit staat al als preset in `opi/configs/presets/keycloak-config.yaml`. De rolfilter wordt **per
client** toegepast, niet op de authenticatieflow van de realm; zonder dit blok kan iedereen in
de project-realm zich als node registreren.

Redirect-URI's hoef je niet op te geven: `opi/connectors/keycloak.py:483` zet per ingress-host
een wildcard (`https://{host}/*`), dus het callback-pad van headscale valt daar al onder.

## Component 1: headscale

| | |
|---|---|
| image | `ghcr.io/juanfont/headscale:v0.28.0` |
| ports | inbound `[8080]`, outbound `[443, 5432]` |
| services | `publish-on-web`, `postgresql-database`, `persistent-storage` (`/data`), `keycloak` |
| probe | `scheme: http`, `liveness-path: /health` |
| security | **niets zetten**, zie valkuilen |

**`command` is verplicht.** Het image heeft entrypoint `/ko-app/headscale` en geen CMD, dus
zonder subcommando print het zijn help en stopt. ZAD kent geen `args:`, alleen `command:` (dat
de entrypoint overschrijft), dus het volledige pad moet mee:

```yaml
command:
  - /ko-app/headscale
  - serve
```

**`aliases`** op het component, voor waarden die ZAD injecteert:

```yaml
aliases:
  HEADSCALE_SERVER_URL: https://${PUBLIC_HOSTNAME}
  HEADSCALE_DATABASE_POSTGRES_HOST: ${DATABASE_SERVER_HOST}
  HEADSCALE_DATABASE_POSTGRES_PORT: ${DATABASE_SERVER_PORT}
  HEADSCALE_DATABASE_POSTGRES_NAME: ${DATABASE_DB}
  HEADSCALE_DATABASE_POSTGRES_USER: ${DATABASE_SERVER_USER}
  HEADSCALE_DATABASE_POSTGRES_PASS: ${DATABASE_PASSWORD}
  HEADSCALE_OIDC_ISSUER: ${OIDC_URL}/realms/${OIDC_REALM}
  HEADSCALE_OIDC_CLIENT_ID: ${OIDC_CLIENT_ID}
  HEADSCALE_OIDC_CLIENT_SECRET: ${OIDC_CLIENT_SECRET}
  HEADSCALE_NOISE_PRIVATE_KEY_PATH: ${DATA_PATH}/noise_private.key
  HEADSCALE_UNIX_SOCKET: ${DATA_PATH}/headscale.sock
```

`HEADSCALE_UNIX_SOCKET` niet vergeten. Standaard staat die op `/var/run/headscale/`, wat een
rootless container niet kan aanmaken. Dan draait de server wel, maar kunnen
`headscale users list` en `headscale preauthkeys create` er niet mee praten.

**`env-vars`** op de deployment-component (plat, geen geheimen, en zo direct leesbaar in git):

```yaml
env-vars:
  HEADSCALE_LISTEN_ADDR: 0.0.0.0:8080
  HEADSCALE_DATABASE_TYPE: postgres
  HEADSCALE_PREFIXES_V4: 100.64.0.0/10
  HEADSCALE_PREFIXES_V6: fd7a:115c:a1e0::/48
  HEADSCALE_DNS_OVERRIDE_LOCAL_DNS: 'false'
  HEADSCALE_DNS_BASE_DOMAIN: vlam.internal
  HEADSCALE_DERP_SERVER_ENABLED: 'true'
  HEADSCALE_DERP_SERVER_PRIVATE_KEY_PATH: /data/derp_server_private.key
  HEADSCALE_DERP_SERVER_STUN_LISTEN_ADDR: 0.0.0.0:3478
  HEADSCALE_DERP_SERVER_REGION_ID: '999'
  HEADSCALE_DERP_SERVER_REGION_CODE: headscale
  HEADSCALE_DERP_SERVER_REGION_NAME: Headscale Embedded DERP
  HEADSCALE_DERP_SERVER_AUTOMATICALLY_ADD_EMBEDDED_DERP_REGION: 'true'
```

Neem dit blok integraal over. Elke ontbrekende waarde kost een deploy-ronde, zie valkuilen.

`base_domain` mag niet samenvallen met het domein van `server_url`, vandaar `vlam.internal`.

## Component 2: vlam-gateway

| | |
|---|---|
| image | `docker.io/tailscale/tailscale:v1.98.9` |
| ports | inbound geen, outbound `[443]` |
| uses-components | `[vlam-proxy]` |
| probe | `scheme: none` (userspace tailscaled luistert nergens op) |

Plat in `env-vars` op de deployment-component, want dit zijn geen geheimen:

```yaml
env-vars:
  TS_USERSPACE: 'true'
  TS_EXTRA_ARGS: --login-server=https://<headscale-hostname>
  TS_HOSTNAME: vlam-gateway
  TS_KUBE_SECRET: ''
  TS_STATE_DIR: /tmp/tailscale
```

De **preauthkey hoort niet in git**. Zet `TS_AUTHKEY` via de portal in `user-env-vars`, dan
staat hij AGE-versleuteld.

**`TS_DEST_IP` werkt niet.** Containerboot weigert die in userspace-modus
(`TS_DEST_IP is not supported with TS_USERSPACE`), want dat is destination-NAT en dat vraagt
kernel-modus met `NET_ADMIN` en een tun-device. ZAD dropt alle capabilities, dus kernel-modus is
daar sowieso uitgesloten.

Wat wél werkt is een **serve-config met `TCPForward`**: tailscaled zet die verbinding zelf op, en
dat mag userspace prima. Geen hostmatching (anders dan bij een HTTP-handler, die op de Host van
de request matcht en dus 404 geeft als je met een IP verbindt), en het blijft laag 4, dus de
TLS-sessie gaat straks ongebroken door naar VLAM. Het is dus niet alleen een omweg maar de
productievorm.

Serve-config wil een bestand en ZAD kan die niet mounten, dus schrijven via `command`:

```yaml
command:
  - /bin/sh
  - -c
  - |
    cat > /tmp/serve.json <<'JSON'
    {
      "TCP": {"8080": {"TCPForward": "productie-vlam-proxy:8080"}}
    }
    JSON
    export TS_SERVE_CONFIG=/tmp/serve.json
    exec /usr/local/bin/containerboot
```

`TCPForward` neemt een `host:poort`, dus de **DNS-naam van de Service**. Geen ClusterIP
hardcoden, die is niet stabiel als de Service opnieuw wordt aangemaakt.

**`TS_STATE_DIR` op `/tmp` is een wegwerpkeuze.** Containerboot wil zijn state standaard in een
Kubernetes Secret bewaren en vraagt daar RBAC voor, wat ZAD per component niet kan verlenen.
`TS_KUBE_SECRET: ''` zet dat uit. Gevolg: bij elke herstart raakt de gateway zijn identiteit
kwijt, registreert opnieuw met de herbruikbare preauthkey en **krijgt een nieuw tailnet-adres**,
terwijl de oude node als dode entry achterblijft. Voor productie hoort daar `persistent-storage`
op, anders verwijst je DNS-record naar een adres dat bij de volgende deploy verdwijnt.

Zet dit component op `disabled: true` tot headscale draait en er een preauthkey is. Anders
crashloopt hij en vervuilt hij de logs.

## Component 3: vlam-proxy

| | |
|---|---|
| image | `docker.io/library/haproxy:lts-alpine` |
| ports | inbound `[8080]`, outbound `[443, 8080]` |
| uses-components | `[headscale]` in de test, later het VLAM-adres |

ZAD kan nog geen configbestand mounten, en een handmatige ConfigMap helpt niet omdat ArgoCD de
Deployment beheert en de volumeMount terugdraait. Tussenoplossing: het alpine-image heeft een
shell, dus schrijf de config in het commando.

```yaml
command:
  - /bin/sh
  - -c
  - |
    cat > /tmp/haproxy.cfg <<'CFG'
    defaults
      timeout connect 5s
      timeout client  1m
      timeout server  1m
    frontend vlam_in
      bind :8080
      mode tcp
      default_backend vlam_out
    backend vlam_out
      mode tcp
      server vlam <UPSTREAM>:443
    CFG
    exec haproxy -f /tmp/haproxy.cfg -db
```

Frontend en backend mogen sinds HAProxy 3.3 **niet dezelfde naam** hebben.

Voor productie kan de SNI-controle erbij, die weigert niet-TLS-verkeer meteen en documenteert
zichzelf. In de test met headscale als upstream moet die eruit, want dan klopt de SNI niet:

```
tcp-request inspect-delay 5s
tcp-request content reject unless { req.ssl_sni -i vlam-api.rijksweb.nl }
```

Geen standaard `nginx` gebruiken: dat image bindt poort 80 en schrijft zijn pid als root.

## Met de hand aanplakken

**Extra Service voor vlam-proxy, 443 naar 8080.** `manifests/service.yaml.jinja` zet `port` en
`targetPort` altijd gelijk, dus ZAD kan die mapping niet maken, en rootless kan HAProxy geen
443 binden. Dus een losse Service met `port: 443`, `targetPort: 8080`, en `TS_DEST_IP` wijst
naar díe ClusterIP.

## Stappen

1. **Alleen headscale uitrollen**, gateway op `disabled`. Controleer:
   ```
   curl https://<hostname>/health          → {"status":"pass"}
   kubectl exec deploy/productie-headscale -- /ko-app/headscale users list
   ```
   De tweede bewijst dat de unix socket klopt.
2. **Logintest** vanaf een laptop: `tailscale up --login-server=https://<hostname>`. Er hoort een
   browser open te gaan met de Keycloak-login van de project-realm.
3. **Negatieve test** met een account zonder de rol `allowed-user`. Dat moet geweigerd worden.
   Dit is het eigenlijke bewijs dat de VPN dicht zit; stap 2 alleen zegt niets.
4. **Infra-gebruiker en preauthkey** aanmaken. De gateway kan geen browser openen, dus die heeft
   een preauthkey nodig, en die hangt in headscale altijd aan een gebruiker:
   ```
   headscale users create infra
   headscale preauthkeys create --user 1 --reusable --expiration 24h
   ```
   Bewust een niet-menselijke gebruiker: hangt de gateway aan een persoon, dan valt hij om zodra
   die persoon vertrekt of zijn toegang wordt ingetrokken.

   **Een preauthkey hoort bij één headscale-instantie en reist niet mee.** Kopieer je een project
   naar een ander cluster, dan draait daar een andere headscale met een eigen database, en de
   meegereisde sleutel geeft `handling register with auth key: auth-key not found`. Maak op het
   doelcluster dus altijd een nieuwe gebruiker en sleutel aan.
5. **Gateway aanzetten** met de key en `TS_DEST_IP`, `disabled` eraf. Test:
   `curl http://<tailnet-IP>/` geeft de headscale-pagina terug via haproxy.
6. **DNS pushen** (`extra_records` met `vlam-api.rijksweb.nl` naar het tailnet-IP van de gateway)
   en de upstream vervangen door het echte VLAM-adres.

## Valkuilen

Alles hieronder heeft ons in de sandbox tijd gekost. Op productie zijn ze te vermijden.

**Headscale past zonder configbestand géén enkele default toe.** Niet de prefixes, niet
`base_domain`, niet de DERP-instellingen, ook al staan die allemaal in `config-example.yaml`. Je
bouwt zijn defaults dus zelf na met env-vars, en je merkt dat één fout per deploy-ronde. Neem
het `env-vars`-blok hierboven integraal over in plaats van te wachten op de foutmeldingen.

**Het headscale-image is een ko-build zonder shell.** De truc om een configbestand via `command`
weg te schrijven werkt daar dus niet. Dat kan, omdat env-vars volstaan, maar als dat ooit
verandert is een echte config-mount nodig.

**Env-vars gaan per stuk, één sleutel en één waarde.** Dat werkt gewoon, maar het loopt mis als
je een heel blok tekst in het waardeveld plakt: dan krijg je één variabele met die hele tekst
als waarde. Zo werd `HEADSCALE_LISTEN_ADDR` letterlijk
`0.0.0.0:8080 HEADSCALE_DATABASE_TYPE = postgres HEADSCALE_PREFIXES_V4 = ...`, en dat kostte
enkele deploy-rondes aan foutmeldingen die de verkeerde kant op wezen (ontbrekende prefixes,
ontbrekend database-type). Schrijf specificaties dus niet als uitgelijnd `KEY = value`-blok, en
zet niet-geheime waarden liever plat in `env-vars` in het projectbestand.

**Een portal-save kan een gelijktijdige wijziging stil overschrijven.** Het aanroeppad in
`opi/core/task_handlers_project.py` maakt een verse ProjectManager en geeft geen
compare-and-swap-basis mee, dus het is last-writer-wins. Werk niet tegelijk in de portal en in
git aan hetzelfde project. (Fix in behandeling.)

**Comments in het projectbestand overleven een portal-bewerking niet.** Elke YAML-round-trip
strijkt ze weg. De waarden zelf blijven wel staan.

**`security` op het component niet zetten.** ZAD zet op Kind zelf `runAsNonRoot: true` met
`runAsUser`, `runAsGroup` en `fsGroup` op 1001 (`manifests/deployment.yaml.jinja:56-64`). Dat
overschrijft de root-user uit het headscale-image en maakt het volume schrijfbaar. Een eigen
`fs-group` is niet nodig.

**Een uitgeschakeld component wordt gemeld als "pods worden aangemaakt".** De statusmelding leest
0 van 0 replicas als een wachtstatus in plaats van als eindtoestand. Verwarrend, niet kapot.

## Wat er op ODCN anders gaat

**De SCC kent de UID toe.** Op `restricted-v2` worden per-component security-overrides genegeerd
en injecteert admission zelf een UID uit het namespace-bereik. Het headscale-image heeft `User:
0` in zijn metadata, maar dat wordt overschreven. Als statische Go-binary zou het met een
willekeurige UID overweg moeten kunnen, maar dat is op ODCN nog niet aangetoond.

**NetworkPolicies worden afgedwongen, ook in de sandbox.** Dat is anders dan lang is aangenomen.
ZAD zet een `<deployment>-tenant-baseline-network-policy` op pods met label
`deployment=<naam>`; een pod zonder die labels krijgt time-outs, met die labels HTTP 200. Op
`sandboxed-local` gemeten, niet afgeleid.

Gevolg: `ports.outbound` is functioneel, geen cosmetiek. Zorg dat headscale 443 en 5432 uit mag,
de gateway 443, en de proxy 443 plus de poort van de upstream. Een ontbrekende outbound-poort
laat zich zien als een **time-out**, niet als een duidelijke foutmelding, en dat kost uren als
je het niet verwacht.

**MetalLB voor DERP.** Zonder inkomende UDP loopt al het tunnelverkeer via een relay. Wil je
directe verbindingen, dan heeft de DERP-server een eigen publiek IP met UDP 3478 nodig. Vraag
het platformteam of hun pool UDP aankan.

**`timeout-tunnel` op de Route.** De ingress-template zet `timeout: 300s` maar geen
`timeout-tunnel`. Voor de control-verbinding volstaat dat; zodra DERP-verkeer over dezelfde
Route loopt is `haproxy.router.openshift.io/timeout-tunnel` wel nodig, anders breken
overdrachten af.

**Node-verlooptijd niet op de gateway toepassen.** Voor mensen willen we een korte `node.expiry`
(24 uur) als intrekkingsmechanisme. Geldt die ook voor de gateway, dan valt de tunnel elke dag
om. Die node moet uitgezonderd worden.

**Beeldsignaturen.** ODCN weigert bepaalde images. Eerder bleken UBI-gebaseerde images te falen
en docker.io-images te passeren. `ghcr.io` is nog niet getoetst.

## Aangetoond op 2026-07-28

End-to-end gemeten vanaf een laptop, niet afgeleid:

```
curl -v http://<tailnet-IP van de gateway>:8080/health
  → HTTP 200, {"status":"pass"}
```

De volledige keten: laptop, WireGuard over de DERP-relay (`netcheck: UDP is blocked, trying
HTTPS`), gateway in userspace met `TCPForward`, haproxy met vastgezette backend, headscale als
nep-VLAM. Dat is dezelfde vorm als productie, met alleen de backend vervangen.

Ook aangetoond: headscale ondersteunt serve-config prima, en de login loopt via Keycloak met
node-registratie in headscale.

## Open

- [ ] Negatieve test: een account zonder `allowed-user` moet geweigerd worden
- [ ] `persistent-storage` op de gateway, zodat het tailnet-adres stabiel blijft
- [ ] `node.expiry` kort zetten voor mensen, met de gateway uitgezonderd
- [ ] DNS-push met `extra_records` beproeven
- [ ] `config-files` op een component in ZAD, zodat de haproxy-omweg weg kan
- [ ] Meerdere containers per component in ZAD, zodat de gateway naast de proxy kan draaien en
      `TS_DEST_IP` gewoon `127.0.0.1` wordt
- [ ] De gehardcodeerde ClusterIP vervangen voordat dit richting productie gaat
- [ ] Doet de MetalLB-pool op ODCN UDP?
