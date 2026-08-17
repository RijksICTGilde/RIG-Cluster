# Authorization Wall

De Authorization Wall zorgt ervoor dat alleen ingelogde gebruikers je applicatie kunnen bereiken. Bezoekers die niet zijn ingelogd worden automatisch doorgestuurd naar een inlogpagina.

## Wanneer gebruik je dit?

- Je applicatie is zichtbaar op het internet maar niet voor iedereen bedoeld
- Je wilt dat gebruikers eerst moeten inloggen
- Je applicatie heeft zelf geen inlogscherm

## Wat wordt er ingesteld?

Er wordt automatisch een inlogscherm gekoppeld aan je applicatie via Keycloak. Alle aanvragen naar je applicatie worden door een proxy gestuurd die afdwingt dat de gebruiker ingelogd is en geautoriseerd is.

**Wat een niet-ingelogde aanvraag terugkrijgt: HTTP 403 met de inlogpagina in het antwoord, geen 302 naar elders.** Dat is precies wat een browser nodig heeft en wat een controle op afstand verrast: wie met curl of een healthcheck wil aantonen dat je applicatie leeft en op 200 controleert, ziet 403 en concludeert dat het stuk is. Achter deze muur is 403 het teken dat de muur staat, niet dat je applicatie eronder ligt.

**Let op:** deze service vereist dat ook **Keycloak Authentication** en **Publiceren op het web** zijn ingeschakeld. Deze worden automatisch mee geselecteerd.
