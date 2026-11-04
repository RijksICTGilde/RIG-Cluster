# Metrics: graceful degradation en herstel op de achtergrond

## Wat het is

De metingen in het portaal komen van een metrics-backend: in productie en op staging via
Grafana (`METRICS_BACKEND=grafana`), op de sandbox rechtstreeks van Prometheus. Die backend
kan wegvallen, en als dat gebeurt mag het de rest van OPI niets kosten. Dit document
beschrijft wat er dan gebeurt: wat de gebruiker ziet, wanneer de connector zichzelf
onbeschikbaar noemt, en hoe hij vanzelf terugkomt.

De aanleiding is de storing van 4 november 2026. Een node viel om, de connector zette
zichzelf op "niet verbonden", en daarna kwam hij niet meer terug: de herstel-task riep een
async functie aan zonder `await`, logde vals succes, en gaf het bovendien na tien pogingen
op. Herstel gebeurde alleen nog als er een request binnenkwam, met de volle timeouts in dat
request en een nieuwe HTTP-client per poging.

## Wat de gebruiker ziet

Zonder metingen blijft elke pagina werken; de meetblokken zeggen wat er aan de hand is.

| Plek | Melding |
|---|---|
| Resourcegebruik op het tabblad Project | "Resourcegebruik is niet op te halen" met "Metrics zijn op dit moment niet beschikbaar." |
| Meters op het dashboard | "Resourcegebruik niet beschikbaar" |
| Grafieken bij een deployment | "Metingen zijn niet op te halen" |
| Gedeelde diensten (resources, opslag, databases) | per blok de meetfout, de pagina zelf blijft staan |

Nooit nullen of een lege grafiek waar een meetwaarde hoort: een getal dat op een meting
lijkt terwijl er niets gemeten is, is erger dan geen getal.

Jobs die metingen gebruiken (OOM-watcher, auto-tuning, capacity watcher, de kaart) slaan
hun ronde over en loggen dat. Ze handelen nooit op ontbrekende data: de auto-tuner laat een
component ongemoeid, de capacity watcher stuurt geen melding.

## Hoe de status werkt

`is_connected` op de connector betekent: de laatste poging met de backend lukte.

Wel een verbindingsfout, dus de status gaat om:

- een transportfout (DNS, connect, timeout);
- HTTP 5xx;
- HTTP 401 of 403 (het service-accounttoken);
- geen prometheus- of mimir-datasource in Grafana.

Geen verbindingsfout, dus de status blijft staan:

- HTTP 400 of een foute PromQL. Dat is een fout in de query, niet in de backend.

Zolang de status "niet beschikbaar" is, doet geen querymethode nog een request: ze gooien
meteen een `GrafanaConnectionError` of geven leeg terug. Dat is het punt: anders wacht elke
projectpagina en elke job de client-timeouts (10s connect, 30s read) opnieuw uit.

Alleen de OVERGANG komt in de log: een warning bij het wegvallen met de reden, een info bij
herstel met hoelang het duurde. De reden bevat een statuscode of een fouttype, nooit
headers of het token. Mislukte pogingen daarna staan op debug.

## Hoe het herstel werkt

- Bij de opstart start OPI één probe op de achtergrond. De opstart WACHT daar niet op: de
  connector begint als niet-beschikbaar, en een pagina vlak na de start meldt dat eerlijk.
- Slaat de status om naar niet-beschikbaar, dan start een herstel-task, hoogstens één
  tegelijk. Die probeert met backoff van 5s, 10s, 20s, 40s en daarna elke 60 seconden, en
  GEEFT NIET OP. Slaagt de probe, dan zet hij de status terug en stopt hij.
- Is alles gezond, dan draait er geen periodieke probe. Het blijft een systeem per request:
  een gewone query bouwt geen verbinding op, en een mislukte query start het herstel.
- Bij het afsluiten wordt de herstel-task gecanceld en de HTTP-client gesloten.
- Er is één `httpx.AsyncClient` voor het hele proces. Een client per poging lekte tijdens
  een storing een connection pool per request.

## Readiness

Metingen tellen NIET mee in readiness of liveness. `ReadinessState`
(`opi/core/readiness.py`) kent alleen database, Keycloak, OAuth en projectbestanden. Een
Grafana die weg is mag de pod nooit uit de rotatie halen.

## Configuratie

Er komt voor dit gedrag geen instelling en geen endpoint bij. De bestaande instellingen
blijven: `METRICS_BACKEND`, `GRAFANA_URL`, `GRAFANA_TOKEN` en `GRAFANA_DATASOURCE_UID` (leeg
betekent: zelf opzoeken). De backoff staat als `RECOVERY_DELAYS` in
`opi/connectors/grafana_prometheus.py`.

## De directe PrometheusConnector

Op de sandbox draait `PrometheusConnector`. Die is stateless: `is_connected` is altijd
`True` en er is geen status om bij te houden. Een mislukte query komt er als
`PrometheusQueryError` of `PrometheusConnectionError` uit, en de aanroepers tonen daarvoor
dezelfde melding.

## Afhankelijkheden

- `opi/connectors/grafana_prometheus.py`: de status, de probe en het herstel
- `opi/connectors/prometheus.py`: `get_metrics_connector()` en `close_metrics_connector()`
- `opi/core/startup.py`: start de eerste probe
- `opi/server.py`: sluit de connector bij shutdown
- `tests/test_grafana_degradatie_en_herstel.py`: de metingen hierop, met een
  `httpx.MockTransport` in plaats van een echte Grafana
