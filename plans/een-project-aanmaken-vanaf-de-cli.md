# Een project aanmaken vanaf de CLI

Status: plan, 7 augustus 2026. Niet gebouwd. Aanleiding: een project aanmaken kan alleen via de UI, omdat het SSO vereist. Daardoor kan een agent of script geen project opzetten, en dat is precies wat `zad-cli` mogelijk zou moeten maken.

## Wat er nu is, gemeten

**De API-authenticatie is structureel per project.** `validate_api_token` in `opi/api/endpoint_util.py` zegt het zelf:

> *"This decorator requires project-specific API key via X-API-Key header. ALWAYS validates that the API key matches the project_name from the route. Returns 401 if project_name is missing."*

Bij het aanmaken bestaat het project nog niet. Er is dus geen sleutel en geen projectnaam om tegen te valideren. **Dit is geen ontbrekend endpoint maar een ontbrekend authenticatiepad.** Dat is het echte werk in dit plan.

**Wat er wel al is, en dat scheelt veel:**

- Het taaktype `create_project` bestaat en wordt door de wizard gebruikt (`create_async_task`), dus de aanmaakweg zelf is er.
- Er staat een uitgecommentarieerd `POST /projects` in `api/router.py` uit de zelfbedieningstijd; het idee is eerder bedacht en uitgezet.
- SSO bestaat, maar aan de webkant: dertig routes dragen `@requires_sso`, sessie-gebaseerd.
- **De taak kan het projectbestand zelf opbouwen.** `handle_create_project` kent twee paden: een kant-en-klare YAML (die de wizard levert), of losse projectgegevens waaruit hij het bestand genereert met `generate_self_service_project_yaml`. Dat tweede pad is precies wat de CLI nodig heeft.
- De projectsleutel bestaat al als `config.api-key` in het projectbestand.

## De basis moet erin, en die bestaat al

Een projectbestand met alleen een naam is schemageldig (`required: ["name"]`, gemeten), maar daar kan het systeem niets mee. Wat de UI-weg schrijft en wat er dus ook uit de CLI-weg moet komen:

```yaml
name, display-name, description
clusters:     [cluster]
services:     [...]
config:       {...}          # met de api-key
repositories:
  - name: main-repo
    url / username / password / branch   # uit de settings
    path: "."
```

Zonder dat `repositories`-blok heeft ArgoCD geen bron. Het wordt opgebouwd uit `PROJECT_REPO_URL`, `PROJECT_REPO_USERNAME` en `PROJECT_REPO_BRANCH` plus een wachtwoord, in `generate_self_service_project_yaml`.

**Dat is dus geen nieuw werk maar hergebruik**, en dat is belangrijker dan het lijkt: `main-repo` staat vandaag op **vijf** plekken hardgecodeerd (`project_file.py`, `router_wizard.py`, en drie keer in `project_utils.py`). Een CLI-weg die zijn eigen basis opbouwt wordt de zesde, en dan lopen ze uiteen zodra er een verandert. Dat is precies de klasse fout die vandaag vier keer boven kwam.

**Wat er expliciet NIET in hoeft:** een deployment. Het gaat om de basis waarmee het project bestaat en zijn repo kent; wat erin draait richt je daarna via de CLI in.

## Er liggen twee ontwerpen, en dat moet eerst beslist worden

`zad-cli/TODO.md` bevat al een volledig uitgewerkt ontwerp voor dit doel, inclusief de valkuilen. Maar het is een **ander** ontwerp dan hierboven beschreven, en het verschil bepaalt de omvang.

**Ontwerp A, uit de CLI-TODO: het formulier in de browser.** De CLI opent `/projects/new?cli=<nonce>`, de gebruiker logt in en vult het bestaande formulier in, en het portaal post na afloop projectnaam en API-sleutel terug naar een loopback-listener van de CLI. Wat RIG-Cluster dan moet leveren is klein: het portaal accepteert `cli_callback` plus `state`, en er komt een endpoint dat de sleutel uitgeeft aan de ingelogde eigenaar. Geen nieuw authenticatiepad, want het portaal is al SSO-beveiligd.

**Ontwerp B, de vraag van vandaag: aanmaken vanaf de CLI zelf.** `zad project add --name "Test" --description "Nog een test"` maakt het project echt aan; de browser komt alleen langs om in te loggen, en het token dat daaruit komt gaat naar een API-endpoint. Dat vraagt wel een tweede authenticatiepad in de API: een token van een gebruiker, naast de bestaande sleutel per project.

**Het verschil dat telt:** bij A vult een mens een formulier in, bij B niet. Voor agentisch werken is A geen oplossing, want er zit een handmatige stap in het midden. B is duurder en is het enige dat het gestelde doel haalt.

**Besloten op 7 augustus: B, met de SSO-popup voorlopig erbij.** Inloggen blijft via het scherm dat de CLI opent; wat daarna gebeurt loopt via de API in plaats van via een formulier.

**En A niet weggooien.** De loopback-opzet, de nonce en de opslagregels uit de CLI-TODO gelden voor allebei; alleen wat er in de browser gebeurt verschilt. Bouw B, en leen de beveiligingskeuzes die daar al staan.

## Hoeveel werk is het

Vier stukken, en het middelste is het echte werk.

1. **Het endpoint zelf: klein.** Naam en omschrijving in, een `create_project`-taak eruit met het genereer-pad, zodat de basis (repositories, config, api-key) uit de bestaande opbouw komt en niet uit een zesde kopie.
2. **Het tokenpad: het grootste stuk, maar begrensd.** Zie de afbakening hieronder; het gaat om een token verifiëren, niet om een inlogflow bouwen.
3. **Het antwoord: klein maar bepalend.** Projectnaam plus API-sleutel terug, zodat de CLI zijn context kan zetten. Dat is het hele punt van de exercitie.
4. **De CLI-kant: al ontworpen, niet gebouwd.** Loopback, nonce, opslag met 0600. Staat in `zad-cli/TODO.md` en hoeft niet opnieuw bedacht te worden.

## De afbakening: de API is een resource server, geen identity provider

Vastgelegd op 7 augustus. De API **voorziet niets van SSO**: geen inlogflow, geen callback, geen sessiebeheer, geen redirects. Het endpoint moet bereikbaar zijn met een **geldig SSO-token**, en waar dat token vandaan komt is niet de zorg van de API.

Dat is de standaardrolverdeling en die moet ook op de standaardmanier gebouwd worden, zoals elk ander systeem het bij een vergelijkbare oplossing doet:

- **`Authorization: Bearer <token>`**, zoals RFC 6750 voorschrijft. Niet in een query string, niet in een eigen header.
- **Verifiëren tegen de JWKS van de realm**: handtekening, uitgever, doelgroep en geldigheidsduur. Niet zelf ontleden, niet vertrouwen op wat er in het token staat zonder de handtekening te controleren.
- **Identiteit uit de claims**, autorisatie bij ons: dat het token geldig is zegt wie iemand is, niet dat hij een project mag aanmaken. Dat tweede is onze beslissing en hoort expliciet.

**Het gereedschap staat er al**, gemeten: `authlib.jose` voor JWKS-verificatie, en `python-keycloak` met `decode_token` en `public_key`. De JWKS-url wordt elders in de code al samengesteld (`connectors/keycloak.py`). Dit is dus aansluiten op wat er is, geen nieuwe machinerie.

**Wat daarmee vervalt** ten opzichte van een eerdere lezing van dit plan: er hoeft geen tweede aanmeldweg gebouwd te worden. De bestaande sessie-gebaseerde SSO voor de webkant blijft ongemoeid; hier komt alleen een tweede manier bij om een aanroeper te *herkennen*, naast de sleutel per project.

## Voorstel

1. **Beslis eerst tussen A en B**, en leg de reden vast. Zonder die keuze bouwt de een een callback in het portaal en de ander een tokenpad in de API.
2. **De tokenverificatie als eerste**, want daar hangt de rest aan. Eén manier om een gebruiker te herkennen, langs de standaardweg, en de bestaande sleutel per project blijft ongemoeid voor alles wat een project al heeft.
3. **Daarna het endpoint**, met naam en omschrijving als enige verplichte velden en de rest op de standaarden die de wizard ook gebruikt.
4. **Het antwoord vastleggen als contract**: projectnaam en sleutel, en niets meer dan dat.
5. **Verwijderen expliciet buiten scope.** Aanmaken via een token is een ding; verwijderen met hetzelfde token is een tweede besluit, en het hoort niet meegenomen te worden omdat het toevallig in dezelfde route past.

## Waar op te letten

**De sleutel nooit in een URL.** Uit de CLI-TODO, en het is de belangrijkste regel: een sleutel in de query string belandt in browsergeschiedenis, proxy-logs en referrers. Alleen in een antwoordbody.

**Een tweede authenticatiepad is een vergroting van het aanvalsoppervlak.** De API kent nu precies één manier binnen te komen, en die is per project begrensd. Een gebruikerstoken is dat niet: die spreekt namens een persoon over alle projecten. Wat dat token mag, hoort net zo scherp begrensd te zijn als de sleutel dat is, en het hoort een securityreview te krijgen voordat het live gaat.

**Een leeg project is geldig maar misschien niet verwerkbaar.** Het schema keurt een project met alleen een naam goed; of `process_project` daar ook mee omgaat is niet gemeten. Doe dat vroeg, want als er een minimale inhoud nodig is, verandert dat het contract van het endpoint.

**Dit staat op twee TODO-lijsten.** Punt 6 van onze eigen lijst verwijst al naar `zad-cli/TODO.md` en noemt drie dingen die bij ons liggen, waaronder dit endpoint. Werk vanuit die lijst en laat de twee niet uiteenlopen.
