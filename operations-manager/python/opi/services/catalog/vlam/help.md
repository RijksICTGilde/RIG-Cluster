# VLAM-API

Deze dienst geeft je applicatie toegang tot de VLAM-API, de taalmodel-API van SSC-ICT, vanuit je eigen pods en zonder VPN.

Je eigen VLAM-sleutel stuur je zelf mee vanuit je applicatie; zet die als eigen omgevingsvariabele (`user-env-vars`), bijvoorbeeld als `VLAM_KEY`. Die sleutel krijg je bij SSC-ICT, niet via ZAD.

## Welke weg je kiest

Elk component van elke deployment krijgt `VLAM_API_URL`: het adres van onze proxy, die de beveiligde verbinding met VLAM **termineert** en zelf het certificaat controleert. Daar hoef je niets voor in te stellen.

En op een cluster dat het **doorlus-pad** aanbiedt daarnaast `VLAM_API_URL_DIRECT` en `VLAM_CA_BUNDLE_PATH`. Dat tweede adres gaat naar dezelfde proxy op een andere poort, maar daar wordt de verbinding **doorgelust**: hij loopt versleuteld door tot aan VLAM, en jij controleert het certificaat in je eigen applicatie.

Staan die twee variabelen niet in je pod, dan biedt dit cluster de doorlus niet aan en is `VLAM_API_URL` de enige weg. Schrijf je applicatie dus niet vast op een variabele die je er niet in ziet staan.

**Kies `VLAM_API_URL`, tenzij je een reden hebt om dat niet te doen.** Het werkt zonder dat je iets instelt, en het stuk verkeer dat onversleuteld is blijft binnen ons eigen cluster en beheer.

**Kies `VLAM_API_URL_DIRECT` als je versleuteling tot aan VLAM zelf nodig hebt** en die eis niet mag eindigen bij onze proxy. Je betaalt daarvoor met een stukje werk in je applicatie: het certificaat van VLAM komt van een interne uitgever die in geen enkele standaardbundel zit, dus zonder die bundel faalt elke verbinding op "unknown certificate authority".

## De CA-bundel

`VLAM_CA_BUNDLE_PATH` wijst naar een bestand in je pod, bijvoorbeeld `/etc/ssl/vlam/vlam-ca.pem`. Het platform zet het daar neer; je hoeft er niets voor te uploaden.

Twee dingen die vaak verward worden:

- Het is **niet het certificaat van VLAM**, maar dat van de **uitgever** ervan. Daarmee controleer je of het certificaat dat VLAM je aanbiedt echt van VLAM is.
- Het is een **publiek** bestand. Er zit niets geheims in, en je mag het gerust downloaden om er lokaal mee te testen. De knop staat op de projectpagina, bij het blok VLAM-API; dat blok verschijnt op de clusters die het doorlus-pad aanbieden.

## Zet de bundel ERNAAST, nooit ERVOOR IN DE PLAATS

Dit is de fout die je een halve dag kost, dus hij staat hier los.

Er bestaan omgevingsvariabelen die er verleidelijk uitzien: `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, `NODE_EXTRA_CA_CERTS`. De eerste drie **vervangen** de vertrouwensketen van je hele applicatie. Zet je er deze bundel in, dan vertrouwt je pod verder helemaal niets meer: geen publieke API, geen Keycloak, geen pakketspiegel. En die storing lijkt in niets op zijn oorzaak, want je VLAM-aanroep werkt dan juist prima.

Daarom zet de dienst die variabelen bewust niet zelf. Hij geeft je een **pad**; wat je ermee doet bepaal je in je eigen code, bij de verbinding die het nodig heeft.

## Per taal, de regel die je nodig hebt

- **Python met `requests`**: geef het pad mee als `verify=` op de aanroep zelf, dus `requests.get(url, verify=os.environ["VLAM_CA_BUNDLE_PATH"])`. De rest van je applicatie merkt er niets van.
- **Python met `httpx` of `openai`**: maak een context met `ssl.create_default_context()`, dat is de standaardbundel, en voeg deze bundel eraan toe met `context.load_verify_locations(os.environ["VLAM_CA_BUNDLE_PATH"])`. Die context geef je als `verify=` aan `httpx.Client`; de `openai`-client neemt zo'n client over met `http_client=`.
- **Node.js**: maak een `https.Agent` waarvan `ca` de ingebouwde `tls.rootCertificates` bevat **plus** de inhoud van het bundelbestand, en geef die agent mee aan fetch, axios of undici voor deze host. `NODE_EXTRA_CA_CERTS` mag hier ook: die variabele voegt juist wel toe in plaats van te vervangen. Hij geldt dan alleen voor je eigen proces, en je zet hem zelf als `user-env-var` met `$VLAM_CA_BUNDLE_PATH` als waarde.
- **Go**: haal de systeempool op met `x509.SystemCertPool()`, voeg het bestand toe met `pool.AppendCertsFromPEM(pem)`, en zet die pool als `RootCAs` in de `tls.Config` van je transport.
- **Java**: Java leest geen PEM-bestand, alleen een truststore. Kopieer in je image `$JAVA_HOME/lib/security/cacerts` naar bijvoorbeeld `/app/truststore.jks`, voeg de bundel eraan toe met `keytool -importcert -noprompt -storepass changeit -alias vlam-ca -file /etc/ssl/vlam/vlam-ca.pem -keystore /app/truststore.jks`, en wijs `javax.net.ssl.trustStore` naar die kopie. Begin je met een lege store, dan heb je dezelfde vervangingsfout gemaakt, alleen met een ander gereedschap.
- **curl, om het te proberen**: `curl --cacert "$VLAM_CA_BUNDLE_PATH" "$VLAM_API_URL_DIRECT/v1/models"` -- dat geldt alleen voor die ene aanroep.

## Wat de dienst nog meer doet

- Het netwerkverkeer van je pods naar de proxy wordt opengezet: naar de poort van `VLAM_API_URL`, en op een cluster met het doorlus-pad ook naar die van `VLAM_API_URL_DIRECT`. Zonder de dienst heeft je pod geen weg naar die namespace.
- Voor het doorlus-pad zet de dienst een regel in `/etc/hosts` van je pod, die de VLAM-naam naar onze proxy wijst. Dat moet, want de naam moet in de URL blijven staan: je controleert het certificaat op die naam. Je hoeft daar zelf niets voor te doen.

Verwacht je bibliotheek een andere naam dan `VLAM_API_URL`, gebruik dan een alias op je component.
