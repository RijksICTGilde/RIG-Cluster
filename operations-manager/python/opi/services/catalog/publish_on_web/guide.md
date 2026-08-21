# Webadressen

Elk component met publish-on-web krijgt per deployment een webadres. Deze gids loopt de veelvoorkomende situaties langs: wat je instelt en wat je dan krijgt.

## Hoe een adres wordt opgebouwd

Een adres is een naamdeel plus een domein. **domain-format** bepaalt het naamdeel als combinatie van vaste bouwstenen: de componentnaam, de deploymentnaam, de projectnaam en een zelfgekozen **subdomain**. **base-domain** bepaalt het domein erachter; leeg betekent het standaarddomein van het cluster.

De zes combinaties, met streepjes tussen de delen:

- **component-deployment-project**: web-productie-mijnproject.rijksapp.nl (de standaard)
- **deployment-project**: productie-mijnproject.rijksapp.nl
- **component-deployment-subdomain**: web-productie-mijnapp.rijksapp.nl
- **deployment-subdomain**: productie-mijnapp.rijksapp.nl
- **component-subdomain**: web-mijnapp.rijksapp.nl
- **subdomain**: mijnapp.rijksapp.nl

Welke varianten je kunt kiezen hangt af van het domein. De streepjes-varianten kunnen op elk domein. Elke variant met meer dan één deel bestaat ook met punten, zoals web.productie.mijnproject.rijksapp.nl, maar die kunnen alleen op een domein dat losse subdomeinen ondersteunt; het standaarddomein van het cluster doet dat niet, daar zijn alleen de streepjes-varianten beschikbaar. Per domein staat dit in de clusterlijst. Zit de deploymentnaam in het format, dan krijgen productie en acceptatie vanzelf verschillende adressen.

## Een kort, vast adres

Kies domain-format **subdomain** en vul bij **subdomain** de naam in, bijvoorbeeld mijnapp. Een subdomein op een domein dat het platform beheert is een aanvraag: een beheerder keurt hem goed, zodat twee projecten niet dezelfde naam claimen. Tot die tijd is je applicatie bereikbaar op een standaardadres.

## Meerdere componenten achter één adres

Kies een format zonder componentdeel en geef elk component een eigen **path**, één of meer. Zo serveert je frontend op / en je API op /api, achter hetzelfde adres. Met **rewrite** herschrijf je het pad voordat het je container bereikt, bijvoorbeeld /api naar /.

## Elk component een eigen adres

Kies een format met componentdeel; elk component krijgt dan zijn eigen adres. Wil je bij een punt-variant ook een adres zonder componentdeel, wijs dan met **root-component** het component aan dat dat adres erbij krijgt, bijvoorbeeld je frontend op productie.mijnproject.rijksapp.nl.

## Een eigen domein

Je eigen domeinnaam laten uitkomen op je applicatie is een combinatie van drie velden:

- **base-domain**: je eigen domeinnaam, bijvoorbeeld domein.nl. Dit veld is geen gesloten lijst: schrijf de naam er zelf in.
- **domain-format**: een variant met subdomain, meestal subdomain.
- **subdomain**: het naamdeel, bijvoorbeeld mijn.

Samen geeft dat mijn.domein.nl. Het eigen domein zelf is een aanvraag: een beheerder keurt goed dat jouw project dat domein mag gebruiken. Tot die tijd is je applicatie bereikbaar op een standaardadres op het platformdomein. Het subdomein op je eigen domein hoeft niet goedgekeurd te worden: welke namen daar bestaan is aan jouw organisatie.

Op de domeinen die het cluster zelf beheert wordt de DNS voor je geregeld; welke dat zijn verschilt per cluster en staat in de clusterlijst. Bij een eigen domein ligt de DNS altijd bij je eigen organisatie: je DNS-beheerder zet een record dat jouw naam naar het platform laat wijzen.

Het certificaat is standaard een Let's Encrypt-certificaat dat het platform zelf aanvraagt en verlengt. Dat kan pas als het DNS-record staat en werkt, dus regel dat voordat je uitrolt. Een eigen certificaat kan ook, via de **tls**-instelling van het component; op een cluster dat geen certificaat voor een eigen domein kan aanvragen is dat de aangewezen weg, en dat meldt het platform bij het opslaan.

De details, welk record precies, de certificaatvoorwaarden en internet.nl, staan op [Eigen domein](/eigen-domein).

Paden werken hier ook: één eigen domein met /frontend en /api naar verschillende componenten is domain-format subdomain plus een eigen **path** per component.

## Het kale domein zelf

**expose-component-on-bare-domain** laat het kale eigen domein, dus domein.nl zonder naamdeel ervoor, ook een component serveren. Het is een extra adres naast de adressen uit domain-format, niet de manier om een adres als mijn.domein.nl te krijgen. Laat het veld weg als je dit niet nodig hebt.
