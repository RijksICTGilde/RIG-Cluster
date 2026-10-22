# Redis Cache

Een snelle sleutel-waardeopslag die je project deelt met andere projecten op de gedeelde Redis van het platform. Je krijgt een eigen gebruiker en een eigen prefix, zodat je alleen bij je eigen sleutels kunt.

## Wanneer gebruik je dit?

- Je wilt zware berekeningen of trage antwoorden cachen
- Je gebruikt een taakwachtrij, bijvoorbeeld Celery
- Je wilt sessies delen tussen meerdere replica's

## Verschil met Namespace Redis Cache

Deze service gebruikt de gedeelde Redis en is voor vrijwel iedereen de juiste keuze. **Namespace Redis Cache** is bedoeld voor een eigen Redis per project; die variant kies je niet zelf, het platform bepaalt wanneer hij van toepassing is.

## Wat wordt er ingesteld?

Er wordt een Redis-gebruiker met een eigen wachtwoord en een sleutelprefix aangemaakt. Je component krijgt **REDIS_HOST**, **REDIS_PORT**, **REDIS_USERNAME**, **REDIS_PASSWORD**, **REDIS_PREFIX** en **REDIS_URL**.

Bouw je sleutels en kanalen als **prefix:naam**: je toegang is beperkt tot sleutels die met jouw prefix beginnen. Redis is een cache, geen bewaarplaats; ga ervan uit dat gegevens kunnen verdwijnen.

## Kan je applicatie geen prefix gebruiken?

Je kunt de sleutelbeperking uitzetten, maar dat is een aanvraag: een platformbeheerder beslist erover, omdat je gebruiker dan bij elke sleutel in de gedeelde Redis kan. Tot die beslissing houdt je gebruiker het prefix, dus je applicatie blijft werken binnen haar eigen sleutels. Een eigen Redis per project is er nog niet, dus deze aanvraag is de enige weg.
