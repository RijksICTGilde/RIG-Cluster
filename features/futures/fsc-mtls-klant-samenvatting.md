# FSC met eigen certificaat op ZAD

**Het kan.** FSC vereist end-to-end mTLS: de versleutelde verbinding moet ononderbroken tot in de pod komen, zodat het client-certificaat de inway bereikt (certificate-bound tokens, RFC 8705). We hebben op het ODCN-productiecluster aangetoond dat dat werkt — voor beide FSC-poorten, met jóuw eigen certificaat en niet het platform-certificaat.

Eén ding ontbreekt nog: ZAD heeft **nog geen functie om een certificaat via de UI/API toe te voegen**. Dat is geen technische blokkade, maar een schone uitbreiding die met prioriteit op de backlog kan zodra er behoefte is.

## Wat we hebben aangetoond

Een pod die zelf TLS termineert met een eigen, herkenbaar certificaat (`O=RIG-FSC-TEST`), via twee externe paden:

| Poort | Gebruik | Mechanisme | Resultaat |
|---|---|---|---|
| **443** | dataverkeer (Outway → Inway) | OpenShift Route met `passthrough` | client krijgt **ons** cert terug |
| **8443** | managementverkeer (Manager-mesh) | MetalLB `LoadBalancer`, eigen publiek IP | client krijgt **ons** cert terug |

Contrastbewijs: op exact dezelfde router gaf een normale (edge-)route het platform-wildcard van Let's Encrypt terug. Bij passthrough termineert de router aantoonbaar níet — de pod is het TLS-eindpunt. Dat is precies wat FSC nodig heeft.

| FSC-eis | Aangetoond |
|---|---|
| Pod is zélf het TLS-eindpunt (mTLS tot in de pod) | ✅ |
| Geen terminatie/re-encrypt onderweg (cert-binding intact) | ✅ |
| Twee externe poorten: 443 (data) + 8443 (management) | ✅ |
| Eigen certificaat i.p.v. platform-certificaat | ✅ |

## Belangrijke randvoorwaarden

1. **Alleen `passthrough` werkt.** De standaard `edge`-terminatie en `reencrypt` breken de certificate-binding (de router wordt dan zelf het TLS-eindpunt). Het client-cert als HTTP-header doorgeven werkt evenmin.
2. **NetworkPolicy verplicht.** Het cluster kent een default-deny; verkeer naar de pod moet expliciet worden toegestaan — op beide paden.
3. **Publieke IP-adressen zijn schaars.** De 8443-LoadBalancer krijgt elk een eigen publiek IP uit een eindige MetalLB-pool — dat schaalt niet naar honderden/duizenden endpoints. De 443-Route schaalt wél: die deelt één router-IP en onderscheidt endpoints op hostnaam (SNI). Bij het ontwerp dus meewegen hoeveel FSC-inways/managers we realistisch kunnen hosten.
4. **PKIoverheid-certificaat.** FSC-peers vertrouwen alleen PKIoverheid — een Let's Encrypt-certificaat is hiervoor niet bruikbaar. Levering en geldigheid van het certificaat liggen bij de afnemer.

> Internet-exposure (de ingress publiek zetten, IPv4 én IPv6) doet ZAD al standaard — dat is geen extra stap. De MetalLB-service krijgt sowieso een publiek IP; aannemelijk dat ook die direct bereikbaar is (te bevestigen).

## Hoe je certificaat in ZAD komt (ontwerp)

Een generiek `attachments`-blok in het projectbestand, naar analogie van het bestaande `user-env-vars`:

Je upload via UI of API een bestand; ZAD slaat het **encrypted** op (zelfde mechanisme als bestaande secrets) en mount het read-only als bestand in je pod. Je applicatie verwijst er vervolgens zelf naar. Nog te bouwen.

## Openstaande punten

- **IP-adressen zijn beperkt.** Elk 8443-endpoint vraagt een eigen publiek IP uit de eindige MetalLB-pool; we kunnen er niet onbeperkt (denk: duizenden) aanmaken. Aandachtspunt voor het ontwerp: hoeveel FSC-endpoints willen/kunnen we hosten, en kunnen we IP's delen (bijv. één IP per project i.p.v. per component).
- **SNI-hostnames** afstemmen op de DNS van ZAD. SNI (*Server Name Indication*) is de hostnaam die een client in zijn eerste TLS-bericht in leesbare vorm meestuurt. Bij `passthrough` ziet de router het versleutelde verkeer niet, dus die hostnaam is het énige waarop hij kan bepalen naar welke pod hij routeert — elke FSC-inway heeft dus een eigen, unieke en stabiele hostnaam nodig die bij het certificaat past. We beheren via ZAD al een aantal domeinen, dus die kunnen we koppelen aan de specifieke IP-adressen — alleen hebben we daar nu nog geen mechanisme voor: een deployment heeft op dit moment maar één publiek webadres, terwijl dit er mogelijk meerdere vereist (bijv. per component per deployment). (Voor de 8443-LoadBalancer speelt SNI niet; die routeert op IP.)
- **Bouwwerk in ZAD**: het `attachments`-blok + passthrough-Route, LoadBalancer en NetworkPolicy in de manifest-generatie.
