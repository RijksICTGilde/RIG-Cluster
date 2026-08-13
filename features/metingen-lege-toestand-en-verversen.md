# Metingen: lege toestand, eenheden en zichzelf verversen

Drie dingen op de metingenkaarten die op een groene test niet te zien zijn: er stond geen
melding als er niets gemeten was, de balken zeiden niet wat ze toonden, en het blok haalde
zich een keer op en daarna nooit meer.

## Geen metingen is een toestand, geen leegte

Het blok `Metingen - <deployment>` (`/projects/<naam>/metrics`) toonde zes lege grafieken
als er niets te meten was. Dat is niet te onderscheiden van iets dat stuk is, terwijl het
vlak na een start het normale geval is.

Er staan nu twee verschillende meldingen boven de grafieken, want het zijn twee
verschillende gevallen:

| Toestand | Melding |
|---|---|
| Prometheus antwoordt niet (niet verbonden, of de bevraging liep stuk) | "Metingen zijn niet op te halen" - een storing van de meting, niet van de deployment |
| Prometheus antwoordt, maar zonder waarden | "Nog geen metingen" - hoort zo vlak na een start, of er draait niets om te meten |

De route (`GET /projects/details/{project}/metrics/{deployment}`) geeft het sjabloon
daarvoor twee vlaggen mee:

- `prometheus_bereikbaar` - `prom.is_connected`, en `False` zodra een bevraging faalt;
- `metingen_leeg` - `not _heeft_metingen(metrics, pvc_storage)`.

`_heeft_metingen()` telt alleen echte REEKSEN (`cpu`, `memory`, `network_in`,
`network_out`, `disk_read`, `disk_write`, en de PVC-waarden). Een limiet telt niet mee:
die komt uit de deploymentdefinitie, niet uit een meting.

Dezelfde keuze staat op het dashboard: `Network Traffic` zonder meetpunten tekende een
grafiek met een streepje op de as en twee nullen, en zegt nu wat er aan de hand is.

## De kaart Resourcegebruik zegt wat de balk toont

`Resourcegebruik (heel project)` had na het hertekenen alleen twee balkjes met het woord
"CPU" en "Geheugen" ernaast. De vorm die er hoort te staan bestond al en draait op
productie; die is teruggehaald:

```
2 deployment(s), 3 pod(s) op dit cluster
[icoon] CPU                          0m / 3.0 cores (0%)
[balk]
[icoon] Geheugen (in gebruik)     24 MiB / 1.1 GiB (2%)
[balk]
```

Zonder limiet vervalt het percentage en de balk: er is dan geen bovengrens om tegen af te
zetten, en een balk zonder schaal is geen meting.

De schrijfwijze zit in twee macro's in `bg/_resource-usage.html.j2`: onder een core in
millicores (`30m`), daarboven in cores met een decimaal (`22.5`); geheugen onder 0,1 GiB in
MiB, daarboven in GiB.

De balk krijgt `value-display="tooltip"`, anders zet het thema het percentage er nog een
tweede keer naast.

Dezelfde legenda ontbrak op `Verdeling over projecten` (dashboard) en staat er weer bij:
`0.030 cores (12.3%)` per project.

### De kaart staat op Overzicht

Deze kaart gaat over het HELE project - alle deployments delen een namespace, dus de
blokken op het tabblad Metrics tellen nooit op tot deze cijfers. Hij staat daarom op
`/projects/<naam>/details`, tussen `Acties` en `Deployments`, en niet op het tabblad
Metrics (dat per deployment is). Verplaatst, niet gekopieerd.

## Het blok ververst zichzelf elke minuut

De grafieken gaan over de tijd; met alleen `hx-trigger="intersect once"` moest je F5
gebruiken om te zien of het aantrok.

Het tabblad Metrics rendert ALLE deployments en verbergt er alle op een na met `is-hidden`
(switchDeployment wisselt ertussen zonder de pagina te herladen). Een `every 60s` van htmx
zou dus ook de verborgen blokken peilen - bij vier deployments vier bevragingen per minuut
om er drie weg te gooien.

Daarom:

- het blok luistert op `hx-trigger="intersect once, zad-metingen-ververs"`;
- een klok in `bg/project-tabs.html.j2` stuurt die gebeurtenis elke 60 seconden naar
  `.deployment-section:not(.is-hidden) [id^="metrics-content-"]`, dus alleen naar wat in
  beeld staat;
- staat het browsertabblad op de achtergrond (`document.hidden`), dan gebeurt er niets; bij
  terugkomen wordt er meteen een ronde gedaan.

Geen htmx-triggerfilter (`every 60s [conditie]`): htmx bouwt zo'n filter met de
`Function`-constructor, en de Content-Security-Policy van deze applicatie staat geen
`unsafe-eval` toe - de conditie zou stil nooit waar worden.

Gemeten op de sandbox: een project met twee deployments doet in 68 seconden twee
bevragingen, allebei voor de zichtbare deployment.

## Waar het staat

| Bestand | Wat |
|---|---|
| `opi/templates_lotc/bg/_deployment-metrics.html.j2` | de meldingen, en een stack om de knoppenbalk en de kaarten (die plakten tegen elkaar) |
| `opi/templates_lotc/bg/_resource-usage.html.j2` | de kaart Resourcegebruik |
| `opi/templates_lotc/bg/_dashboard-usage.html.j2` | Network Traffic en de verdeling over projecten |
| `opi/templates_lotc/bg/project-tabs.html.j2` | de plaats van de kaart, en de minuutklok |
| `opi/web/router.py` | `_heeft_metingen()` en de twee vlaggen |
| `tests/test_lotc_metingen.py` | de poorten |
