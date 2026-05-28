# ODCN Ingress Controller

> Source: https://docs.rijksapps.nl/ingress/ (VPN-only)

## Per-customer IngressController

Elke klant krijgt een eigen IngressController met een eigen publiek IP. Voor RIG is dat `rig` (zichtbaar als `routerName=rig` op Routes).

Aanmaken van extra IngressControllers gaat via Topdesk-ticket bij ODCN.

## NetworkPolicy: traffic vanaf de Router toestaan

Router-pods draaien in namespace **`openshift-ingress`**. Pods van een specifieke klant-router zijn herkenbaar aan label `ingresscontroller.operator.openshift.io/deployment-ingresscontroller: <customername>`.

Voor RIG:

```yaml
kind: NetworkPolicy
apiVersion: networking.k8s.io/v1
metadata:
  name: allow-ingress-from-odcn-router
spec:
  podSelector:
    # <selector op tenant-pods>
  policyTypes:
    - Ingress
  ingress:
    - from:
        - podSelector:
            matchLabels:
              ingresscontroller.operator.openshift.io/deployment-ingresscontroller: rig
          namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: openshift-ingress
      ports:
        - protocol: TCP
          port: 80
        - protocol: TCP
          port: 443
```

Belangrijk: `podSelector` en `namespaceSelector` zitten onder hetzelfde `from`-item (AND), niet als losse items (OR).

## IP-allowlist op Routes / Ingress

Twee mechanismes:

1. **`haproxy.router.openshift.io/ip_whitelist` annotatie** op Route of Ingress object. Default: VPN-IP is automatisch whitelisted — als safety-switch. Overschrijven met `0.0.0.0/0,::/0` voor publieke toegang.

2. **NetworkPolicy op de IngressController zelf** — beheerd door ODCN. Default alleen VPN-IP. Wijzigen via Topdesk-ticket.

Voor IPv6: voeg IPv6-adres toe aan de `ip_whitelist` annotatie.

## TLS-certificaten

Drie opties:
- **Standaard wildcard**: `*.(customer).prd1.gn2.quattro.rijksapps.nl` — door ODCN beheerd, geen actie nodig
- **Eigen wildcard**: bij ODCN aanleveren via Topdesk + Secure Transfer
- **Eigen domein + cert**: zelf DNS-A-record + cert-manager of handmatig K8s Secret

Voor RIG primair: `*.rig.prd1.gn2.quattro.rijksapps.nl` (default) + `*.rijksapp.nl` etc. (custom).

## Multiple IngressControllers (transit networks)

Als een klant meerdere routers heeft (bv. internet + VPN-only via BKN/RON transit), specificeer welke via label op Route/Ingress:

```yaml
metadata:
  labels:
    customer.odc-noord.nl/ingress-controller: <customername-transittype>
```
