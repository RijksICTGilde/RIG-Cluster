# RON-koppeling op ODCN

Referentiedocument voor de netwerkkoppeling tussen ODCN en het RON. Wat we hebben, wat ingress en egress in deze context betekenen, en hoe je ze aanzet.

We gebruiken deze koppeling voor de VLAM-API van SSC-ICT en voor de mailserver. Het bouwverslag van de VLAM-gateway staat in `vlam.md`, de onderbouwing in `features/futures/vlam-api-vpn-proxy.md`. Dit document beschrijft alleen de koppeling zelf.

## Het netwerkblok

Bron: status-update actie 328, wijziging Quattro.

| Blok | Waarvoor |
|---|---|
| `145.21.227.136/29` | het volledige gekoppelde blok |
| `145.21.227.136/30` | ingress, `.136` hangt aan de ingresscontroller |
| `145.21.227.140/30` | egress |

## Ingress en egress, wie initieert wat

Beide termen zijn vanuit ODCN gezien, niet vanuit de tegenpartij.

**Ingress** is verkeer dat bij ons binnenkomt: de ander opent de verbinding, het bestemmingsadres ligt in `145.21.227.136/30`. Onze ingresscontroller luistert daar.

**Egress** is verkeer dat wij naar buiten initiëren: onze pods openen de verbinding, en het bronadres dat de tegenpartij ziet komt uit `145.21.227.140/30` (SNAT via de egressgateway, het pod-IP is aan de andere kant niet zichtbaar).

Praktische vertaling: **wat je aan een externe partij doorgeeft om te allowlisten hangt af van wie de verbinding opzet.** Verbinden wij naar hen, zoals bij de mailserver, dan is dat voor ons egress en heeft de tegenpartij `145.21.227.140/30` nodig als bron-IP. Het ingressblok is dan niet relevant en noem je beter niet, dat levert alleen verwarring op. Geef het hele /30 door in plaats van één adres, want welk adres uit de pool het SNAT-proces pakt ligt niet bij ons vast.

## Egress aanzetten: annotatie op de namespace

```yaml
apiVersion: v1
kind: Namespace
metadata:
  annotations:
    egress.projectcalico.org/egressGatewayPolicy: rig-ron
```

Toegestane waarden zijn `internet` of een klantgateway (`rig-*`). Andere sleutels of ongeldige waarden worden door Kyverno geweigerd, en bij een foutieve annotatie wordt de namespace onbruikbaar.

**`rig-ron` vervangt `internet`, het komt er niet bij.** Een namespace die zowel internet als RON nodig heeft, kan dit niet zelf oplossen: de annotatie neemt één waarde. Daarvoor moet je bij Quattro zijn.

OPI zet standaard `internet` (`operations-manager/python/manifests/namespace.yaml.jinja:10`). Wijzigen naar `rig-ron` is nu een handmatige stap per namespace. De wijziging overleeft een refresh, want OPI raakt bestaande annotaties niet aan, maar bij een nieuwe namespace moet je het opnieuw doen. Zie de openstaande punten onderaan `vlam.md`.

## Ingress over RON aanzetten: label op de Route of Ingress

```yaml
kind: Route
metadata:
  labels:
    customer.odc-noord.nl/ingress-controller: rig-ron
```

Let op het verschil met egress: dit is een **label op het Route- of Ingress-object**, niet een annotatie op de namespace.

**Het label moet aanwezig zijn op het moment dat het object wordt aangemaakt.** Achteraf toevoegen werkt niet, dan moet het object opnieuw. Voor ons betekent dat: het hoort in de manifest-generatie van OPI thuis en niet in een handmatige `kubectl label` achteraf.

## Links

- Egress: https://docs.rijksapps.nl/egress-internet-traffic/
- Ingress: https://docs.rijksapps.nl/ingress/?h=ron#option-1-using-the-odc-noord-provided-wildcard-certificate

## Openstaand

- ZAD kan de egress-annotatie nog niet vanuit het projectbestand zetten, dus RON-namespaces vragen een handmatige stap.
- Het ingresscontroller-label wordt nergens door OPI gezet. Zolang dat zo is, kan een RON-ingress niet via ZAD worden opgeleverd, want achteraf labelen werkt niet.
- Verifiëren welk adres uit `145.21.227.140/30` daadwerkelijk als SNAT-bron wordt gebruikt, en of dat per namespace verschilt.
- De eerdere bevinding dat ODCN geen route had naar `145.21.0.0/16` (VLAM en SMTP gaven "Network is unreachable") gaat over precies deze route. Na oplevering van het blok opnieuw testen vanuit een pod met `rig-ron`.
