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

## Wanneer is goedkeuring nodig?

Publiceren op het web aanzetten vraagt geen goedkeuring, en het standaardadres ook niet. Een beheerder keurt alleen goed in twee gevallen:

- Je gebruikt een eigen domein: een beheerder keurt goed dat jouw project dat domein mag gebruiken.
- Je kiest zelf een subdomein op een domein van het platform dat namen bewaakt: zo claimen twee projecten niet dezelfde naam.

Tot de goedkeuring er is, is je applicatie bereikbaar op het standaardadres. Een subdomein op je eigen domein hoeft niet goedgekeurd te worden: welke namen daar bestaan is aan jouw organisatie.

## Je eigen domein gebruiken

In plaats van een adres van het platform kun je een eigen domeinnaam laten uitkomen op je applicatie. Op [Eigen domein](/eigen-domein) staat welk DNS-record je daarvoor laat zetten, wat het platform daarna zelf regelt en welke twee punten bij je eigen organisatie blijven.

## Meer scenario's

Alle manieren om een adres samen te stellen, met wat je daarvoor instelt (een eigen domein, meerdere componenten achter één adres, paden), staan in [Webadressen: de scenario's](/forms/wizard/help/publish_on_web/guide.md).
