# Component capability boundaries: besluit per grens

**Status**: Besluitdocument (werkpakket 5 uit `services-audit-en-herstelplan.md`)
**Datum**: 2026-07-28

Het bouwen van de VLAM-gateway liep tegen zes grenzen van wat een component kan. Elke
grens is tijdens die sessie omzeild, maar de omwegen staan nu in een projectbestand en
dat is geen houdbare plek. Dit document legt per grens een besluit vast: wordt het een
productfunctie (eigen vervolgtaak) of blijft het expliciet buiten scope.

Deze grenzen zijn **nieuwe componentfuncties**, geen onderdeel van de lees-/presentatie-/
formulierintegriteit-herstelacties. Ze horen elk in een eigen taak thuis; ze hier bouwen
zou scope-creep zijn. Het besluit hieronder bepaalt welke taken de moeite waard zijn.

| Grens | Omweg die nu in gebruik is | Besluit |
|---|---|---|
| 1. Geen configbestand te mounten | `command`-override die het bestand schrijft en dan `exec` doet | **Productfunctie (aanbevolen, goedkoop).** Werkt alleen bij images met een shell; het headscale-image is een ko-build zonder shell, dus de omweg is fundamenteel beperkt. |
| 2. Geen extra containers per component | serve-config met `TCPForward` i.p.v. een sidecar | **Buiten scope voorlopig.** De sidecar was de schone vorm, maar de omweg werkt en een generiek sidecar-model raakt manifestgeneratie, resources en netwerk breed. Heroverwegen als een tweede geval opduikt. |
| 3. Geen RBAC per component | Kubernetes-state-opslag uit, state in `/tmp` | **Productfunctie (aanbevolen).** Zonder is identiteit vluchtig: gaat verloren bij elke herstart en het adres verschuift. Vereist een per-component ServiceAccount + Role/RoleBinding; past in het bestaande manifest-contributiemodel. |
| 4. `service.yaml.jinja` zet `port` altijd gelijk aan `targetPort` | losse handmatige Service | **Productfunctie (klein, aanbevolen).** De handmatige Service staat buiten GitOps, dus ArgoCD kent hem niet. Een `service-port != target-port` in de template is een kleine, geïsoleerde wijziging. |
| 5. `env-vars` (deployment-niveau, plat) is bestand-only | via git bewerken | **Productfunctie (aanbevolen, goedkoop).** Onzichtbaar en onbewerkbaar in de portal. De plat-env-var-laag bestaat al in het bestand; er ontbreekt alleen een portal-sectie (editable + visualizer + detailweergave). |
| 6. Ingress-template zet geen `timeout-tunnel` | nog niet nodig geweest | **Buiten scope tot het nodig is.** Wordt pas relevant zodra langlevende streams over dezelfde route lopen. Een `nginx.ingress.kubernetes.io/proxy-*-timeout`-annotatie is dan een kleine template-toevoeging; nu speculatief (YAGNI). |

## Aanbevolen volgorde voor de vervolgtaken

De goedkoopste met de meeste kans om vaker nodig te zijn, eerst:

1. **Grens 5 -- `env-vars` in de portal.** De datalaag bestaat al; alleen een
   portal-sectie ontbreekt. Kleinste inspanning, direct zichtbaar nut.
2. **Grens 1 -- `config-files` op een component.** Een generieke "mount dit bestand"
   vervangt de shell-afhankelijke `command`-omweg; nodig voor shell-loze images.
3. **Grens 4 -- `service-port != target-port`.** Kleine template-wijziging, haalt een
   handmatige out-of-GitOps-Service weg.
4. **Grens 3 -- RBAC per component.** Grotere wijziging (ServiceAccount + Role), maar
   nodig voor stateful componenten met een stabiele identiteit.

Grenzen 2 en 6 blijven bewust buiten scope tot een tweede concreet geval ze rechtvaardigt.
