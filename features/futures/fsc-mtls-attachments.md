# FSC met eigen certificaat op ZAD — haalbaarheid + bewijs

> **Status:** haalbaarheidsanalyse met praktijkbewijs (getest op `odcn-rig-production`, 2026-06-22). De netwerk-randvoorwaarde (ongetermineerde mTLS tot in de pod) is **aangetoond werkend**. De ZAD-kant om je certificaat te uploaden (het `attachments`-blok) is **ontworpen, nog niet gebouwd**.

## Antwoord aan de klant

**Het kan.** FSC vereist end-to-end mTLS — de versleutelde verbinding moet ononderbroken tot in de pod komen zodat het client-certificaat de inway bereikt (certificate-bound tokens, RFC 8705). We hebben op het productiecluster aangetoond dat dat op ODCN werkt, voor **beide** FSC-poorten, met óns eigen certificaat (niet het platform-/Let's Encrypt-certificaat).

Eén ding ontbreekt nog aan ZAD-kant: er is **nog geen functie om een certificaat via de UI/API aan een project toe te voegen**. Dat is geen technische blokkade — het is een schone uitbreiding op het bestaande `user-env-vars`-mechanisme (zie §3) en kan **met prioriteit op de backlog** zodra er concrete behoefte is.

---

## 1. Bewijs — wat we hebben opgezet en aangetoond

Opstelling: een pod die zélf TLS termineert op poort 8443 met een **eigen, herkenbaar self-signed certificaat** (`O=RIG-FSC-TEST`), in namespace `rig-prd-example`. Daarvoor twee onafhankelijke externe paden gelegd en bij elk geverifieerd welk certificaat de client terugkrijgt.

### Poort 443 — dataverkeer (Outway → Inway), via OpenShift Route `passthrough`

| Onderdeel | Opzet |
|---|---|
| Route | `tls.termination: passthrough`, **geen** `tls.certificate`/`key`, **geen** cert-manager-annotatie |
| NetworkPolicy | ingress op pod-poort 8443 toegestaan vanaf `namespaceSelector kubernetes.io/metadata.name: openshift-ingress` |
| `haproxy.router.openshift.io/ip_whitelist` | op `0.0.0.0/0,::/0` gezet (default = alleen VPN-IP) |

**Resultaat:** via het router-IP (`147.181.48.71`, dezelfde router die elders edge-routes bedient) krijgt de client **ons** certificaat terug:

```
subject = O=RIG-FSC-TEST, CN=passthrough-test.rig.prd1.gn2.quattro.rijksapps.nl
issuer  = O=RIG-FSC-TEST, CN=passthrough-test.rig.prd1.gn2.quattro.rijksapps.nl
```

Contrastbewijs op exact dezelfde router-IP: een edge-route (`argocd.rig…`) geeft het Let's Encrypt-wildcard `*.rig.prd1…` terug. De router termineert dus bij `passthrough` aantoonbaar **niet** — de pod is het TLS-eindpunt.

### Poort 8443 — managementverkeer (Manager-mesh), via MetalLB `LoadBalancer`

De gedeelde router luistert alleen op 80/443, dus 8443 gaat **niet** via de router maar via een eigen LoadBalancer-IP:

| Onderdeel | Opzet |
|---|---|
| Service | `type: LoadBalancer`, `externalTrafficPolicy: Local`, poort 8443 |
| MetalLB | annotatie `metallb.io/address-pool: public` → IP automatisch toegekend |
| NetworkPolicy | ingress op 8443 toegestaan (extern verkeer komt mét echt client-IP binnen door `Local`, niet vanaf `openshift-ingress`) |

**Resultaat:** MetalLB kende automatisch een publiek IP toe (`147.181.48.77`, los van het router-IP; `metallb.io/ip-allocated-from-pool: public`). Via dat IP op 8443 krijgt de client opnieuw **ons** certificaat terug (`O=RIG-FSC-TEST`). Rauwe TCP rechtstreeks naar de pod, geen terminatie.

### Sluit dit aan op wat FSC wil?

| FSC-eis | Aangetoond |
|---|---|
| Inway-pod is zélf het TLS-eindpunt (mTLS ononderbroken tot in de pod) | ✅ pod presenteert eigen cert via router (443) én LoadBalancer (8443) |
| Geen terminatie/re-encrypt onderweg (cert-binding intact) | ✅ contrast met edge-route bewijst dat passthrough niet termineert |
| Twee externe poorten: 443 (data) + 8443 (management) | ✅ beide extern bereikbaar aangetoond, elk met eigen mechanisme |
| Eigen certificaat, niet het platform-certificaat | ✅ client krijgt `O=RIG-FSC-TEST`, niet het Let's Encrypt-wildcard |

**Randvoorwaarden/aandachtspunten:**
1. **NetworkPolicy verplicht.** Er is een cluster-brede default-deny (beheerd door ODC-Noord, niet als namespaced object zichtbaar). Zonder expliciete allow naar de pod-poort komt er geen verkeer binnen — op geen van beide paden.
2. **Publieke IP-adressen zijn schaars.** Elke 8443-`LoadBalancer` verbruikt een eigen IP uit de eindige MetalLB-`public`-pool → schaalt niet naar honderden/duizenden endpoints. Het 443-Route-pad schaalt wél: het deelt één router-IP en onderscheidt endpoints op hostnaam (SNI). Aandachtspunt: hoeveel FSC-endpoints kunnen we hosten, en kunnen we IP's delen (bijv. per project i.p.v. per component).

> Internet-exposure (de ingress publiek, IPv4+IPv6) zet ZAD standaard al open — geen extra stap. De default-VPN-only-`ip_whitelist` uit de generieke ODCN-docs wordt in de ZAD-flow dus al verruimd; de MetalLB-service krijgt sowieso een publiek IP (directe bereikbaarheid nog te bevestigen).

> Praktijknoten voor herhaling: UBI-images (`registry.access.redhat.com`) vallen op ODCN's image-signature-policy (`SignatureValidationFailed`) — gebruik `docker.io`-images. En serveer een test-HTTP-response uit een ConfigMap-bestand (`socat … SYSTEM:cat /www/response.http`); inline `printf "…\r\n…"` verhakkelt de escapes via de YAML/JSON-laag.

---

## 2. Waarom alleen `passthrough` werkt

Een OpenShift `Route` kent drie TLS-modi; alleen de laatste behoudt de certificate-binding:

| `tls.termination` | Wat er gebeurt | Werkt voor FSC? |
|---|---|---|
| `edge` (default) | Router termineert TLS, plaintext naar de pod | ❌ client-cert opgegeten |
| `reencrypt` | Router termineert én zet een **nieuwe** TLS-sessie op naar de pod | ❌ outway-cert gaat verloren, binding breekt |
| `passthrough` | Router termineert **niet**; routeert op **SNI** (L4) en zet de stream ongewijzigd door | ✅ pod ziet de outway-cert |

Het client-certificaat als HTTP-header doorgeven (het gangbare "mTLS-op-ingress"/XFCC-patroon) werkt **niet** voor certificate-bound tokens — de chain verandert dan. Omdat passthrough op **SNI** routeert, heeft elke inway/peer een eigen hostname nodig (relevant i.c.m. de per-PR-DNS van ZAD — zie §6).

---

## 3. Hoe je certificaat in ZAD komt (`attachments`-blok) — nog te bouwen

Voorstel: een generiek **`attachments`-blok op componentniveau**, naar analogie van `user-env-vars`. Via UI of API upload je een bestand; wij slaan het **encrypted** op in de projectdefinitie met een identifier en de originele bestandsnaam.

```yaml
components:
  - name: api
    attachments:
      - id: mtls-client-keystore
        filename: keystore.p12
        content: <age-encrypted>
        mount-path: /etc/tls/keystore.p12
```

- **Opslag:** bestandsbytes base64 → AGE-versleuteld met de eigen public key van het project (exact het `user-env-vars`-mechanisme; geen nieuwe crypto). Bij deploy ontsleuteld met de project-private-key.
- **Naar de pod:** elke attachment wordt een **SOPS-encrypted Secret**, read-only gemount op een instelbaar `mount-path` (bestaande volume/volumeMount-machinerie). Aanbevolen: `/etc/tls/`, modus `0400` voor de private key, `fsGroup` = app-gebruiker.
- **KISS:** format-agnostisch — we slaan rauwe bytes op en converteren niets. PKCS12 nodig? Upload een `.p12` (of bouw die in een initContainer).

---

## 4. Wat je Java-applicatie zelf doet

De JVM consumeert een **PKCS#12** keystore/truststore (default sinds Java 9), geen losse PEM-bestanden:

- **keystore** = je eigen cert + private key (je mTLS-identiteit)
- **truststore** = de CA-certificaten die je vertrouwt

Eenvoudigste contract voor een **ongewijzigde JAR** — mount `keystore.p12` + `truststore.p12` op `/etc/tls/` en zet via `JAVA_TOOL_OPTIONS`:

```
-Djavax.net.ssl.keyStore=/etc/tls/keystore.p12
-Djavax.net.ssl.keyStorePassword=$TLS_KEYSTORE_PASSWORD
-Djavax.net.ssl.keyStoreType=PKCS12
-Djavax.net.ssl.trustStore=/etc/tls/truststore.p12
-Djavax.net.ssl.trustStorePassword=$TLS_TRUSTSTORE_PASSWORD
-Djavax.net.ssl.trustStoreType=PKCS12
```

Het wachtwoord komt als env-var uit een secret, nooit hardcoded. Werkt **zonder codewijziging**.

> Spring Boot 3.1+ leest PEM direct via SSL bundles (`spring.ssl.bundle.pem.*`) met hot reload — optioneel, prettiger voor rotatie, niet nodig voor de basis.

---

## 5. Certificaatrotatie

De JVM cachet de SSL-context bij opstart en herlaadt niet vanzelf bij een bestandswijziging:

| Aanpak | Wanneer | Effect |
|---|---|---|
| **Stakater Reloader**-annotatie op de Deployment | Standaard, ongewijzigde JAR | Rolling restart zodra het secret wijzigt |
| **Spring Boot 3.1+ PEM bundle** + `reload-on-update: true` | App is Spring Boot 3.1+ | Zero-downtime hot reload |
| Handmatige rolling update | Langlevende certificaten | Restart op eigen moment |

---

## 6. Openstaande punten

1. **IP-schaarste (8443).** De MetalLB-`public`-pool is eindig; één publiek IP per 8443-endpoint schaalt niet. Bepalen hoeveel FSC-endpoints we hosten en of IP's gedeeld kunnen worden (per project i.p.v. per component). Het 443-pad schaalt wél via gedeeld router-IP + SNI. (Internet-exposure zelf is geen open punt — ZAD zet de ingress standaard publiek; MetalLB-bereikbaarheid nog kort te bevestigen.)
2. **SNI-hostnames.** Hoe verhoudt de per-inway hostname-eis zich tot de per-PR-DNS van ZAD?
3. **Bouwwerk in ZAD.** `attachments`-blok (schema, upload-endpoint, encryptie, secret-volume mount) + passthrough-Route, LoadBalancer-Service en NetworkPolicy in de manifest-generatie. Dit document is het ontwerp.
4. **PKIoverheid-certificaat.** Levering, geldigheid en rotatiecadans liggen bij de afnemer; ZAD bewaart en mount, beheert niet. Let op: voor FSC moet het een **PKIoverheid**-cert zijn — zie §7.

---

## 7. Let's Encrypt vs. "ons eigen certificaat"

Twee dingen die makkelijk door elkaar lopen:

- **Wie termineert / houdt de key aan de rand?** Bij **edge** doet de **router** dat met het ODCN-/Let's Encrypt-wildcard — daar levert het platform het cert dat de client ziet. Bij **passthrough** (en bij de TCP-LoadBalancer) presenteert de rand **geen** cert; alleen jouw pod termineert met jouw cert. Voor FSC verplicht.
- **Wie geeft het cert uit (de CA)?** Het ODCN-wildcard is door Let's Encrypt uitgegeven, maar in de **FSC-trust** telt dat niet — FSC-peers vertrouwen alleen **PKIoverheid**. Een Let's Encrypt-cert is voor FSC onbruikbaar, ook al staat het op je eigen domein.

Kortom: op één verbinding sluiten Let's Encrypt en "ons eigen cert" elkaar uit. Het Let's Encrypt-wildcard is het router-default voor edge; kies je passthrough/LoadBalancer, dan is jouw PKIoverheid-cert het enige dat de client ziet.
