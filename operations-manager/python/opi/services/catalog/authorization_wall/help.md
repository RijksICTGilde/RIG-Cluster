# Authorization Wall

De Authorization Wall zorgt ervoor dat alleen ingelogde gebruikers je applicatie kunnen bereiken. Bezoekers die niet zijn ingelogd worden automatisch doorgestuurd naar een inlogpagina.

## Wanneer gebruik je dit?

- Je applicatie is zichtbaar op het internet maar niet voor iedereen bedoeld
- Je wilt dat gebruikers eerst moeten inloggen
- Je applicatie heeft zelf geen inlogscherm

## Wat wordt er ingesteld?

Er wordt automatisch een inlogscherm gekoppeld aan je applicatie via Keycloak. Alle aanvragen naar je applicatie worden door een proxy gestuurd die afdwingt dat de gebruiker ingelogd is en geautoriseerd is.

**Let op:** deze service vereist dat ook **Keycloak Authentication** en **Publiceren op het web** zijn ingeschakeld. Deze worden automatisch mee geselecteerd.
