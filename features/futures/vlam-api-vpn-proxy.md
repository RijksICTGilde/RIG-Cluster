# VLAM-API ontsluiten via VPN en een vastgezette reverse proxy

**Status**: Ontwerp, proof of concept nog te bouwen
**Datum**: 2026-07-28
**Scope**: ODI-ontwikkelaars toegang geven tot de VLAM-API van SSC-ICT, die via het RON
bereikbaar wordt gemaakt door het ODCN-platformteam

## Waar dit over gaat

SSC-ICT ontsluit een VLAM-API. Het ODCN-platformteam legt de netwerkkoppeling over het RON
aan, waarna die API vanuit pods op ODCN op een IP-adres bereikbaar is. Wij moeten die ene API
beschikbaar maken voor ontwikkelaars op eigen, onbeheerde machines, zonder daarmee het RON te
ontsluiten.

De beveiligingsclaim, in één zin, bruikbaar richting CISO en platformteam:

> Gebruikers komen via een geauthenticeerde VPN uit op een reverse proxy die uitsluitend naar
> één vastgezet adres kan verbinden, waardoor er geen netwerkpad van de gebruiker naar RON
> bestaat: we lussen één API door, we ontsluiten geen netwerk.

## Waarom een VPN, en niet iets lichters

Dit is bewust afgewogen, want een VPN is niet onze standaardlijn (zie
`features/bio-network-access-no-vpn-compliance.md`).

De consumers van de VLAM-API zijn willekeurige HTTPS-clients: curl, SDK's, bestaande tooling.
Er zit geen browser in de keten en we kunnen de clients niet aanpassen. Daarmee vallen de
lichtere opties af:

- De bestaande ZAD-service `authorization-wall` (oauth2-proxy) werkt met een cookie-sessie en
  een sign-in-pagina. Zonder browser kom je daar niet doorheen.
- Een bearer-token via de device-code-flow werkt wel, maar vereist dat de client een header
  meestuurt. Dat kunnen we niet afdwingen.
- Interactieve OAuth werkt met vrijwel geen enkele generieke HTTP-tool. Dat is geen gebrek in
  die tools; het is applicatielogica die iemand bewust moet inbouwen.

Een VPN is het enige mechanisme dat de interactieve Keycloak-login naar een **ander kanaal**
verplaatst. Je logt eenmalig in bij het opzetten van de tunnel, met SSO Rijk, MFA en de
rolfilter, en daarna is het requestpad kaal HTTPS waar elke tool ongewijzigd mee overweg kan.

Dat is de eigenlijke functie van de VPN hier. De netwerkgrens is een prettige bijvangst, niet
de reden.

## Architectuur

```
  eigen laptop                   ODCN, eigen namespace                       RON
+--------------+          +----------------------------------+          +----------+
| tailscale    |          |  headscale        (HTTPS, route) |          | VLAM API |
| client       |--login-->|  control server + Keycloak-OIDC  |          |          |
|              |          +----------------------------------+          |          |
|              |                                                        |          |
|              |  wireguard over DERP     +---------------------+       |          |
|  curl / SDK  |------------------------->| vlam-gateway        |       |          |
+--------------+                          | tailscale userspace |       |          |
                                          +----------+----------+       |          |
                                                     | ClusterIP        |          |
                                          +----------v----------+       |          |
                                          | vlam-proxy          |------>|          |
                                          | nginx, upstream     |       +----------+
                                          | hardcoded           |
                                          +---------------------+
```

Twee losse garanties, door verschillende dingen afgedwongen. Ze zijn allebei nodig:

1. **De client krijgt geen IP-pad naar RON.** Tailscaled draait in userspace-modus en is geen
   router: hij zet verkeer op applicatieniveau door naar één geconfigureerde bestemming en kan
   verder niets. Er is dus geen forwarding om langs te glippen, ook niet als iemand op zijn
   eigen machine routes bijstelt.
2. **De client kan de proxy niet als tussenstap gebruiken.** Dat doet de vastgezette upstream.

Zonder de tweede zou je het IP-pad hebben dichtgezet maar een applicatie-hop hebben
opengelaten.

## Reverse proxy, geen forward proxy

Dit is de plek waar dit soort ontwerpen in de praktijk fout gaat, dus expliciet:

- **Geen variabele in de upstream.** `proxy_pass https://$iets` in nginx schakelt
  runtime-DNS-resolutie in en maakt de bestemming beïnvloedbaar. Hardcode adres en poort.
- **Niet routeren op de Host-header.** Eén vhost, en de Host richting upstream zetten we zelf.
- **Geen CONNECT.** Standaard doet nginx dat niet; het moet ook nooit aangezet worden.
- **Redirects niet volgen namens de client.** Geef ze terug. Wijst een redirect naar een andere
  RON-host, dan loopt de client daarop stuk omdat die geen pad heeft. Dat is zelfbegrenzend.

Filteren op paden en methodes kan, maar alleen als het API-oppervlak stabiel is. Anders wordt
het een onderhoudslast die bij elke VLAM-wijziging breekt. Host en poort vastzetten is het
niet-onderhandelbare deel.

## Waarom headscale, en wat er is afgevallen

De keuze volgt uit een dwangketen, niet uit voorkeur:

| Optie | Waarom niet (nu) |
|---|---|
| Kale WireGuard | Geen login, alleen een statische sleutel in een bestand. Even zwak als een API-key, en dus geen antwoord op de vraag waarom we een VPN bouwen. |
| OpenVPN + `openvpn-auth-oauth2` | Serieuze kandidaat. Bekende client, native TCP 443, pad naar OpenVPN-NL. Vereist alleen een custom SCC (`NET_ADMIN` plus `/dev/net/tun`), en de SSO-flow werkt alleen met WebAuth-capable clients zoals OpenVPN Connect, niet met de kale CLI. |
| OpenVPN-NL | Alleen relevant als de rubricering het eist. Gecontroleerd distributiekanaal, goedkeuring hangt aan het inzetadvies (een zelfgebouwde container valt daar vermoedelijk buiten), en het vereist PKIoverheid-certificaten. Zodra je die toch uitgeeft, levert mTLS op de proxy hetzelfde vertrouwensanker zonder VPN. |
| NetBird | Goed alternatief: WireGuard met native Keycloak en een eigen client, dus geen conflict met Tailscale-gebruikers. Grotere stack (management, signal, relay, dashboard). |
| SSH met kortlevende OIDC-certificaten | Het minste bouwwerk van allemaal: browserlogin levert een certificaat van enkele uren, daarna `ssh -L`. Waard om te heroverwegen als "netwerkgrens" niet letterlijk de eis blijkt. |

Headscale wint vandaag op één praktisch punt: het vraagt niets van het platformteam. Geen SCC,
geen nieuwe capabilities. Daarmee kunnen we vandaag beginnen terwijl aanvragen parallel lopen.

Bekende kosten van headscale, met open ogen:

- De officiële Tailscale-client kan **één tailnet tegelijk**. Wie Tailscale al zakelijk
  gebruikt, moet kiezen. Workarounds bestaan alleen op Linux of via third-party tooling.
- Headscale is community-onderhouden en loopt op onderdelen achter op de officiële server.
- Intrekking werkt pas bij sleutelverloop, niet direct. Daarom `node.expiry` kort zetten
  (24 uur), zodat dagelijks opnieuw inloggen het intrekkingsmechanisme is.
- Zonder inkomende UDP loopt al het verkeer via een DERP-relay. Publieke relays zien alleen
  ciphertext, maar voor een partnernetwerk verwachten we dat een eigen DERP een eis wordt.

## Wat ZAD al levert

| Nodig | ZAD biedt |
|---|---|
| Keycloak met SSO Rijk en rolfilter | aanwezig |
| PostgreSQL voor headscale | `services: postgresql` |
| Persistente opslag voor de headscale-sleutels | `services: persistent-storage` |
| Publieke HTTPS-route | `publish-on-web`, incl. `haproxy.router.openshift.io/timeout: 300s` |
| Env-vars, ook versleuteld | `user-env-vars`, AGE-versleuteld |
| Component-naar-component verkeer | `uses-components` levert Service en netwerktoestemming |
| Geen probe op een poortloze container | `probe: scheme: none` |
| Willekeurige images | `image:` per deployment-component |

## Het projectbestand

Opzet voor de proefopstelling. De dummy-backend wordt later vervangen door het VLAM-adres.

```yaml
schema-version: 2
name: vlam-gateway
description: "VPN-toegang tot de VLAM-API via een vastgezette reverse proxy"

clusters:
  - sandboxed-local

services:
  - publish-on-web
  - persistent-storage
  - postgresql

components:
  - name: headscale
    type: deployment
    ports:
      inbound: [8080]
      outbound: [443, 5432]
    services:
      - publish-on-web
      - reference: persistent-storage
        config:
          - name: keys
            size: 1Gi
            mount-path: /var/lib/headscale
      - postgresql
    probe:
      scheme: http
      liveness-path: /health

  - name: vlam-gateway
    type: deployment
    ports:
      outbound: [443]
    uses-components: [vlam-proxy]
    probe:
      scheme: none          # tailscaled in userspace heeft geen luisterende poort

  - name: vlam-proxy
    type: deployment
    ports:
      inbound: [8080]
      outbound: [443]
    uses-components: [dummy-backend]

  - name: dummy-backend
    type: deployment
    ports:
      inbound: [8080]

deployments:
  - name: poc
    cluster: sandboxed-local
    namespace: vlam-gateway
    repository: main-repo
    components:
      - reference: headscale
        image: "docker.io/headscale/headscale:latest"   # pin op een versie voor productie
      - reference: vlam-gateway
        image: "docker.io/tailscale/tailscale:latest"
      - reference: vlam-proxy
        image: "docker.io/library/nginx:alpine"
      - reference: dummy-backend
        image: "nginxdemos/hello"
```

De env-vars per component, met de secrets AGE-versleuteld:

- **headscale**: `HEADSCALE_SERVER_URL`, `HEADSCALE_DATABASE_TYPE=postgres` plus de
  verbindingsgegevens uit de ZAD-database, `HEADSCALE_OIDC_ISSUER`, `HEADSCALE_OIDC_CLIENT_ID`,
  `HEADSCALE_OIDC_CLIENT_SECRET`, `HEADSCALE_OIDC_SCOPE`, `HEADSCALE_NODE_EXPIRY=24h`.
- **vlam-gateway**: `TS_AUTHKEY` (versleuteld), `TS_USERSPACE=true`,
  `TS_EXTRA_ARGS=--login-server=https://<headscale-host>`, en de bestemming naar de
  ClusterIP van `vlam-proxy`.
- **vlam-proxy**: geen, de configuratie zit in het configbestand (zie gaten hieronder).

## Keycloak

Eén nieuwe OIDC-client, confidential, met redirect-URI
`https://<headscale-host>/oidc/callback`.

Kritiek punt om vóór de bouw uit te zoeken: **zit de rolfilter in de authenticatieflow van de
realm, of is hij per client gekoppeld?**

- In de realm-flow: geldt automatisch ook voor deze nieuwe client.
- Per client: dan moet hij daar expliciet aan gehangen worden, anders staat de VPN open voor
  iedereen in de realm.

Dat is precies het gat dat je niet wilt ontdekken nadat het live staat. De negatieve test in
stap 2 hieronder is er om dit aan te tonen, niet om aan te nemen.

Daarnaast zetten we in headscale ook `oidc.allowed_groups` aan, zodat er een vereiste groep in
het token moet zitten. Twee onafhankelijke controles in plaats van één, zodat de toegang niet
aan één stukje Keycloak-configuratie hangt.

## Vier gaten in ZAD

Geen blokkades voor de proefopstelling, wel dingen om te weten.

1. **Een component kan geen eigen configbestand meekrijgen.** Er is een configmap-template,
   maar geen schemaveld om er een te mounten. Nginx heeft een `nginx.conf` nodig en headscale
   mogelijk een `config.yaml`.
   *Voor de PoC*: `command`-override die de config wegschrijft en dan het proces start.
   *Daarna*: een `config-files`-veld op component-niveau is een kleine, op zichzelf nuttige
   toevoeging. Dit gaat niet de laatste keer zijn dat iemand dit wil.
2. **Of headscale zich volledig via `HEADSCALE_`-env-vars laat configureren is niet bevestigd.**
   De documentatie beschrijft de config als YAML-bestand. Te verifiëren in stap 1; zo niet, dan
   valt dit samen met gat 1.
3. **De NetworkPolicy van ZAD werkt op poorten, niet op bestemmingen.** `ports.outbound: [443]`
   staat 443 naar alles toe. Voor de vangnetlaag "egress alleen naar het VLAM-adres" is een
   handgeschreven policy nodig, of een uitbreiding van het schema.
4. **De ingress-template zet `timeout: 300s` maar geen `timeout-tunnel`.** Voor de
   control-verbinding volstaat dat waarschijnlijk. Zodra we een eigen DERP achter de route
   zetten, draagt die verbinding echt dataverkeer en is `timeout-tunnel` wel nodig.

## Stappenplan

De sandbox draait op Kind en kent geen SCC's, dus alles hieronder kan zonder aanvraag bij een
ander team.

1. **headscale draaiend met database en route.**
   → verify: `headscale users list` werkt in de pod en de HTTPS-URL antwoordt.
2. **Keycloak-client plus OIDC aan, `node.expiry` op 24 uur.**
   → verify: `tailscale up --login-server=...` opent de browser en de node verschijnt in
   `headscale nodes list`. **Negatieve test**: een account zónder de vereiste rol wordt
   geweigerd. Dit is de belangrijkste test van het geheel.
3. **vlam-gateway in userspace-modus, bestemming naar vlam-proxy.**
   → verify: de node staat in headscale en is bereikbaar vanaf de client over het tailnet.
4. **vlam-proxy met hardcoded upstream naar dummy-backend.**
   → verify: `curl http://<tailnet-adres>/` geeft de dummy-pagina. Plus drie negatieve tests:
   CONNECT wordt geweigerd, een andere Host-header verandert de bestemming niet, en andere
   pod-IP's zijn onbereikbaar via de tunnel.
5. **NetworkPolicy: egress alleen naar de dummy-backend.**
   → verify: de proxy bereikt de backend en verder niets.
6. **Naar ODCN in een eigen namespace.**
   → verify de drie aannames: start de tailscale-pod onder `restricted-v2` zonder
   SCC-uitzondering, blijft de control-verbinding staan door een OpenShift-Route, en werkt een
   eigen DERP achter die Route.
7. **Dummy-backend vervangen door het VLAM-adres.** Eén regel, zodra het platformteam de
   RON-koppeling heeft opgeleverd.

## Openstaande vragen buiten ons

- **Is de RON-route cluster-breed of gescopeerd op onze namespace?** Als elke pod op ODCN bij
  VLAM kan, is onze NetworkPolicy het enige dat daartussen staat, en dat is een bredere
  blootstelling dan dit project. Ontwerpbeslissing van het platformteam, maar we willen weten
  welke het is.
- **Doet de MetalLB-pool ook UDP?** Bij ja kan tailscale directe verbindingen maken in plaats
  van alles via een relay, wat fors scheelt in latency.
- **Welke VPN-client heeft de doelgroep, en draait er iemand Tailscale?** Twee vragen die samen
  bepalen of headscale het eindstation is of een tussenstap naar OpenVPN.
- **Wat is de rubricering van het VLAM-verkeer?** Alleen bij Dep-V of hoger is OpenVPN-NL een
  eis in plaats van een keuze.
- **Accepteert SSC-ICT dat al onze gebruikers als één bron-IP binnenkomen?** Zij verliezen
  daarmee herleidbaarheid naar een persoon, tenzij wij die identiteit meesturen of onze
  logging kunnen overleggen.

## Nog te doen bij oplevering

De VPN-uitzondering opnemen in `features/bio-network-access-no-vpn-compliance.md`. Die notitie
legt vast dat ZAD bewust géén VPN gebruikt voor de gebruikersflow. Dit is een andere flow
(toegang tot een partnernetwerk, niet tot onze eigen applicaties), dus het is geen
tegenspraak, maar het moet als bewuste uitzondering vastliggen. Anders leest het bij een audit
als inconsistent beleid.
