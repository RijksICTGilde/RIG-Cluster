# Statische bestanden met een hash in de URL

## Wat het is

Elke verwijzing naar een eigen statisch bestand (`/static/js/wizard.js`, `/static/css/wizard.css`,
afbeeldingen) krijgt een content-hash in de URL: `/static/js/wizard.js?v=f67a9b53`. Verandert de
inhoud van het bestand, dan verandert de hash en dus de URL. Een browser kan een vervangen bestand
daarmee niet meer uit zijn cache serveren, want het is een andere URL.

Dit lost een klasse bugs op, geen snelheidsprobleem: "bij mij werkt het wel" doordat iemands browser
een oude `wizard.js` vasthoudt is duur om te vinden - het symptoom zit in de UI, de oorzaak in een
HTTP-header. ETags stonden er al, dus over de lijn scheelde het weinig; wat ontbrak was de *opdracht*
aan de browser om te hervragen.

## Hoe je het gebruikt

In een template, in plaats van een kaal pad:

```jinja
<link rel="stylesheet" href="{{ static_url('css/wizard.css') }}">
<script src="{{ static_url('js/wizard.js') }}"></script>
<img src="{{ static_url('cloud.jpg') }}" alt="...">
```

`static_url()` is een Jinja-global (`opi/core/templates.py`). Het pad is relatief aan
`operations-manager/python/static/`, met of zonder leidende slash.

Staat de aanroep in een attribuut dat zelf al enkele quotes gebruikt - zoals het ROOS-attribuut
`additionalJs` in `base.html.j2` - resolve de URL dan eerst met `{% set %}` en verwijs naar de
variabele. Een `static_url('...')` inline zou daar de attribuutwaarde vroegtijdig afsluiten.

Een testwacht (`tests/test_static_references_versioned.py`) faalt op elke kale `src="/static/` of
`href="/static/` in `opi/templates/`, zodat een vergeten verwijzing meteen zichtbaar is.

## Cache-header

De `/static`-mount is `CacheControlledStaticFiles` (`opi/core/static_files.py`):

| Verzoek | `Cache-Control` |
|---|---|
| met `?v=<hash>` | `public, max-age=31536000, immutable` |
| zonder `?v=` (of leeg) | `no-cache` |

De header hangt bewust aan de **parameter** en niet aan het pad. `immutable` is een belofte van een
jaar en mag alleen op een URL die zijn eigen inhoud identificeert. Zo valt een vergeten verwijzing
terug op `no-cache`: hooguit suboptimaal, nooit een bestand dat een jaar vastzit bij iedereen die het
al opgehaald heeft. `no-cache` betekent overigens niet "niet bewaren" maar "altijd hervragen" - de
bestaande ETag levert dan gewoon een 304 op.

## Uitzondering: de ROOS-assets

`/static/roos/dist` is een **aparte mount** (`opi/server.py`) en die URL's stuurt ROOS zelf uit; wij
hebben ze niet in de hand en kunnen er geen hash in zetten. Die mount blijft daarom ongewijzigd:
gewone `StaticFiles`, zonder `Cache-Control`, precies zoals het was.

Dit is een bewuste uitzondering, geen vergeten geval. Repareer het niet als "inconsistentie" door
`immutable` op de hele `/static`-boom te zetten - dan zet je juist de ROOS-bestanden een jaar vast,
inclusief de bestanden die in de ontwikkellus live gesynct worden.

## Ontwikkellus

De hash wordt gecached op `(mtime_ns, grootte)` van het bestand. Skaffold synct `static/**/*` naar de
pod, waardoor de mtime verspringt, de hash opnieuw berekend wordt en de URL verandert - zonder
herstart van de applicatie. Eén `os.stat` per verwijzing per render.

## Afhankelijkheden

Geen. Alleen de standaardbibliotheek (`hashlib`) en Starlette's `StaticFiles`.
