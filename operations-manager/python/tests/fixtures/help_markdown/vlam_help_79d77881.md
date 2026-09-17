# VLAM-API

Deze dienst geeft je applicatie toegang tot de VLAM-API, de taalmodel-API van SSC-ICT, vanuit je eigen pods en zonder VPN.

Je eigen VLAM-sleutel stuur je zelf mee vanuit je applicatie; zet die als eigen omgevingsvariabele (`user-env-vars`), bijvoorbeeld als `VLAM_KEY`. Die sleutel krijg je bij SSC-ICT, niet via ZAD.

## Twee wegen, en welke je kiest

Elk component van elke deployment krijgt allebei de adressen. Ze gaan naar dezelfde proxy, op twee verschillende poorten.

| | `VLAM_API_URL` | `VLAM_API_URL_DIRECT` |
|---|---|---|
| wat het is | onze proxy **termineert** de beveiligde verbinding | onze proxy **lust door** en raakt hem niet aan |
| wie controleert het certificaat | wij, op de proxy | **jij**, in je eigen applicatie |
| wat jij moet regelen | niets | de CA-bundel aan je HTTP-client meegeven |
| tussen jouw pod en de proxy | onversleuteld, binnen ons cluster | versleuteld tot aan VLAM |

**Kies `VLAM_API_URL`, tenzij je een reden hebt om dat niet te doen.** Het werkt zonder dat je iets instelt, en het verkeer dat onversleuteld is blijft binnen ons eigen cluster en beheer.

**Kies `VLAM_API_URL_DIRECT` als je versleuteling tot aan VLAM zelf nodig hebt** en die eis niet mag eindigen bij onze proxy. Je betaalt daarvoor met een stukje werk in je applicatie: het certificaat van VLAM komt van een interne uitgever die in geen enkele standaardbundel zit, dus zonder die bundel faalt elke verbinding op "unknown certificate authority".

## De CA-bundel

`VLAM_CA_BUNDLE_PATH` wijst naar een bestand in je pod, bijvoorbeeld `/etc/ssl/vlam/rijksdienst-ca.pem`. Het platform zet het daar neer; je hoeft er niets voor te uploaden.

Twee dingen die vaak verward worden:

* Het is **niet het certificaat van VLAM**, maar dat van de **uitgever** ervan. Daarmee controleer je of het certificaat dat VLAM je aanbiedt echt van VLAM is.
* Het is een **publiek** bestand. Er zit niets geheims in, en je mag het gerust downloaden om er lokaal mee te testen. De knop staat op de projectpagina, bij het blok VLAM-API.

### Zet hem ERNAAST, nooit ERVOOR IN DE PLAATS

Dit is de fout die je een halve dag kost, dus hij staat hier los.

Er bestaan omgevingsvariabelen die er verleidelijk uitzien: `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, `NODE_EXTRA_CA_CERTS`. De eerste drie **vervangen** de vertrouwensketen van je hele applicatie. Zet je er deze bundel in, dan vertrouwt je pod verder helemaal niets meer: geen publieke API, geen Keycloak, geen pakketspiegel. En die storing lijkt in niets op zijn oorzaak, want je VLAM-aanroep werkt dan juist prima.

Daarom zet de dienst die variabelen bewust niet zelf. Hij geeft je een **pad**; wat je ermee doet bepaal je in je eigen code, bij de verbinding die het nodig heeft.

### Per taal

**Python, `requests`** - per aanroep, dus de rest van je applicatie merkt er niets van:

```python
import os, requests

requests.get(f"{os.environ['VLAM_API_URL_DIRECT']}/v1/models",
             verify=os.environ["VLAM_CA_BUNDLE_PATH"])
```

**Python, `httpx` of `openai`** - een SSL-context die de standaardbundel houdt en deze bundel eraan toevoegt:

```python
import os, ssl, httpx

context = ssl.create_default_context()          # de standaardbundel
context.load_verify_locations(os.environ["VLAM_CA_BUNDLE_PATH"])  # en deze erbij
client = httpx.Client(base_url=os.environ["VLAM_API_URL_DIRECT"], verify=context)
```

De `openai`-client neemt zo'n `httpx.Client` over met `http_client=client`.

**Node.js** - een agent voor deze ene bestemming, met de ingebouwde wortels erbij:

```js
const fs = require("fs"), tls = require("tls"), https = require("https");

const agent = new https.Agent({
  ca: [...tls.rootCertificates, fs.readFileSync(process.env.VLAM_CA_BUNDLE_PATH)],
});
// geef `agent` mee aan fetch/axios/undici voor deze host
```

`NODE_EXTRA_CA_CERTS` mag hier ook: die variabele voegt juist wel toe in plaats van te vervangen. Hij geldt dan alleen voor je eigen proces, en je zet hem zelf als `user-env-var` met `$VLAM_CA_BUNDLE_PATH` als waarde.

**Go** - de systeempool ophalen en er een certificaat aan toevoegen:

```go
pool, _ := x509.SystemCertPool()
pem, _ := os.ReadFile(os.Getenv("VLAM_CA_BUNDLE_PATH"))
pool.AppendCertsFromPEM(pem)
client := &http.Client{Transport: &http.Transport{
    TLSClientConfig: &tls.Config{RootCAs: pool},
}}
```

**Java** - Java leest geen PEM-bestand, alleen een truststore. Maak in je image een KOPIE van de standaardtruststore, voeg deze bundel eraan toe, en wijs `javax.net.ssl.trustStore` naar de kopie:

```
keytool -importcert -noprompt -alias vlam-ca \
  -file /etc/ssl/vlam/rijksdienst-ca.pem \
  -keystore /app/truststore.jks -storepass changeit
```

Kopieer eerst `$JAVA_HOME/lib/security/cacerts` naar `/app/truststore.jks`; begin je met een lege store, dan heb je dezelfde vervangingsfout gemaakt, alleen met een ander gereedschap.

**curl, om te proberen** - `--cacert` geldt alleen voor die ene aanroep:

```
curl --cacert "$VLAM_CA_BUNDLE_PATH" "$VLAM_API_URL_DIRECT/v1/models"
```

## Wat de dienst nog meer doet

* Het netwerkverkeer van je pods naar de proxy wordt opengezet, op beide poorten. Zonder de dienst heeft je pod geen weg naar die namespace.
* Voor het doorlus-pad zet de dienst een regel in `/etc/hosts` van je pod, die de VLAM-naam naar onze proxy wijst. Dat moet, want de naam moet in de URL blijven staan: je controleert het certificaat op die naam. Je hoeft daar zelf niets voor te doen.

Verwacht je bibliotheek een andere naam dan `VLAM_API_URL`, gebruik dan een alias op je component.
