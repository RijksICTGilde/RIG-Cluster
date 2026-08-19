# Publiceren op het web

Hiermee krijgt je component een webadres en is het bereikbaar via het internet. Zonder deze service draait je component wel, maar kan niemand er van buiten de cluster bij.

## Wanneer gebruik je dit?

- Je component is een website, portaal of een API die van buiten benaderd wordt
- Je wilt collega's een link kunnen sturen naar je omgeving
- Je hebt dit nodig voor Keycloak-inloggen of de Authorization Wall

Sla dit over voor een component dat alleen door andere componenten in hetzelfde project wordt aangeroepen, bijvoorbeeld een achterliggende worker.

## Wat wordt er ingesteld?

Er komt een ingress met een hostnaam en een geldig certificaat. Het webadres stel je per deployment in bij Webadres; het platform regelt standaard zelf het certificaat. Je kunt ook je eigen certificaat op de ingress laten aanbieden of het verkeer ongemoeid naar je pod laten doorlopen (passthrough), bijvoorbeeld voor mTLS.

Je component krijgt de variabelen **PUBLIC_HOST** en **PUBLIC_HOSTNAME** met het adres waarop het bereikbaar is.

## Je eigen domein gebruiken

In plaats van een adres van het platform kun je een eigen domeinnaam laten uitkomen op je applicatie. Welk DNS-record je daarvoor laat zetten, wat het platform daarna zelf regelt en welke twee punten bij je eigen organisatie blijven, staat op [Eigen domein](/eigen-domein).
