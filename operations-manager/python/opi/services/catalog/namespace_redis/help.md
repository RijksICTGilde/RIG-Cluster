# Namespace Redis Cache

De variant van de Redis-cache voor een eigen Redis per project, in plaats van de gedeelde Redis van het platform. Deze service staat niet in de servicekeuze: het platform kiest hem, jij kiest **Redis Cache**.

## Verschil met Redis Cache

Voor je applicatie is er geen verschil: je krijgt dezelfde variabelen en dezelfde manier van werken. Het verschil zit in waar Redis draait. Op dit moment valt deze service nog terug op de gedeelde Redis; een eigen instantie per namespace is nog niet uitgerold. Kies daarom gewoon **Redis Cache**.

## Wat wordt er ingesteld?

Dezelfde gebruiker, wachtwoord en sleutelprefix als bij de gedeelde Redis, met de variabelen **REDIS_HOST**, **REDIS_PORT**, **REDIS_USERNAME**, **REDIS_PASSWORD**, **REDIS_PREFIX** en **REDIS_URL**.
