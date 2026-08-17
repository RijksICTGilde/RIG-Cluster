# `rewrite` naast `path` in de component-API

## Wat het is

Het add- en update-component-endpoint kennen naast `path` een `rewrite`. Allebei losse
strings; samen worden ze de padregel van het component in het projectbestand:

```yaml
components:
  - name: api
    path:
      - match: /api
        rewrite: /
```

Zonder `rewrite` gaat het pad ongewijzigd door naar de container - het gedrag dat er
altijd al was. Met `rewrite` haalt de ingress de prefix eraf voordat het verzoek de
container bereikt. Dat is precies wat een image dat je niet zelf schrijft nodig heeft:
`--path /api` op een applicatie die intern op `/` luistert.

## Hoe te gebruiken

Een component dat extern onder `/api` hangt en intern op de wortel luistert:

```bash
curl -X POST "https://zad.example.nl/api/v2/projects/mijn-project/components" \
  -H "X-API-Key: <projectsleutel>" -H "Content-Type: application/json" \
  -d '{"name": "api", "image": "ghcr.io/org/api:v1", "path": "/api", "rewrite": "/",
       "deployment_names": ["main"]}'
```

Resultaat in het projectbestand: `path: [{match: /api, rewrite: /}]`, en in het
gegenereerde ingress-manifest de regel
`rewrite "^/api/?(.*)$" "/$1" break;`. Het verzoek `https://<host>/api/status` komt bij
de container binnen als `/status`.

Laat je `rewrite` weg, dan blijft de sleutel uit het bestand en genereert de ingress geen
herschrijfregel: `https://<host>/api/status` komt aan als `/api/status`.

`match` en `rewrite` zijn los bij te werken. Een PATCH met alleen `rewrite` laat de
`match` staan, en andersom:

```bash
curl -X PATCH ".../components/api" -H "X-API-Key: <projectsleutel>" \
  -H "Content-Type: application/json" -d '{"rewrite": "/"}'
```

## Configuratie

| Veld | Verplicht | Beschrijving |
|---|---|---|
| `path` | Nee (standaard `/`) | Padprefix waarop het component extern antwoordt |
| `rewrite` | Nee (geen standaard) | Pad waar de ingress `path` naartoe herschrijft |

Geen standaardwaarde is een bewuste keuze: een impliciete `/` zou bestaande componenten
van gedrag laten veranderen, en voor een component dat zijn eigen prefix afhandelt is dat
verkeerd. Een lege waarde telt als afwezig, want in het sjabloon zou die als een echte
herschrijving naar de wortel uitpakken.

Beide velden lopen door dezelfde validatie als het formulier
(`COMPONENT_PATH_MATCH_EDITABLE` en `COMPONENT_PATH_REWRITE_EDITABLE`): een pad moet met
`/` beginnen en mag geen spaties bevatten. Een afgekeurde waarde levert een 422 met
dezelfde melding als in de wizard.

## Beperkingen

Eén padregel per aanroep. De samengestelde vorm - meerdere `match`/`rewrite`-paren per
component - kan het projectbestand al (zie `multi-path-ingress.md`), maar de API schrijft
er één.

## Afhankelijkheden

- `publish-on-web` op het component; zonder ingress doet een pad niets
- Schema `$defs/component-path` in `opi/schemas/project_v2.json`
- Sjabloon `manifests/ingress.yaml.jinja` voor de nginx-snippet
