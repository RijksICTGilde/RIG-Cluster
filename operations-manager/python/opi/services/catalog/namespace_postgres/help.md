# Namespace PostgreSQL Database

Een eigen PostgreSQL-cluster voor jouw project, in een eigen infrastructuurnamespace, in plaats van een database op de gedeelde server. Je deelt de server dan met niemand en bepaalt zelf de instellingen.

## Wanneer gebruik je dit?

- Je hebt extensies nodig die niet op de gedeelde server staan
- Je hebt SUPERUSER-rechten nodig, bijvoorbeeld voor migraties
- Je wilt een eigen PostgreSQL-versie, image of eigen resources
- Je wilt volledig gescheiden staan van andere projecten

## Verschil met PostgreSQL Database

**PostgreSQL Database** geeft je standaard een database op de gedeelde server: minder resources, minder beheer, en genoeg voor de meeste applicaties. Deze service geeft je een eigen cluster. Hetzelfde bereik je met de gewone service door **scope: project** te kiezen. Begin bij gedeeld en stap over als je tegen een grens aanloopt.

## Wat wordt er ingesteld?

Er wordt een PostgreSQL-cluster (CloudNativePG) aangemaakt in een eigen namespace, met een database, gebruiker en wachtwoord voor je project. Je stelt het aantal instanties en de opslaggrootte in; via het projectbestand kun je ook image, registry, rechten, resources en initialisatie-SQL opgeven. Je component krijgt dezelfde **DATABASE_\***-variabelen als bij de gedeelde database, zodat je applicatie niet hoeft te veranderen.
