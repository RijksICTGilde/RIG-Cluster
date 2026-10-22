# Herkomst van de meegeleverde bibliotheken

Deze bestanden komen uit npm en staan hier in eigen beheer, zodat `script-src` en
`style-src` geen externe host meer hoeven toe te staan. Een host in de CSP laat ELKE
bibliotheek van die host toe, en zonder uitgaand verkeer laadt er niets.

Bijwerken: haal de nieuwe versie van dezelfde URL, werk de versie hieronder bij, en haal
het bijbehorende `*.LICENSE.txt` mee als het bestand ernaar verwijst.

| bestand | pakket | versie | licentie | bron |
|---|---|---|---|---|
| `js/chart.min.js` | chart.js | 4.5.1 | MIT | `https://cdn.jsdelivr.net/npm/chart.js@4.5.1/dist/chart.umd.min.js` |
| `js/chartjs-plugin-annotation.min.js` | chartjs-plugin-annotation | 3.1.0 | MIT | `https://cdn.jsdelivr.net/npm/chartjs-plugin-annotation@3.1.0/dist/chartjs-plugin-annotation.min.js` |
| `js/mermaid.min.js` | mermaid | 10.9.8 | MIT | `https://cdn.jsdelivr.net/npm/mermaid@10.9.8/dist/mermaid.min.js` |
| `swagger-ui/swagger-ui-bundle.js` | swagger-ui-dist | 5.33.3 | Apache-2.0 | `https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.33.3/swagger-ui-bundle.js` |
| `swagger-ui/swagger-ui.css` | swagger-ui-dist | 5.33.3 | Apache-2.0 | `https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.33.3/swagger-ui.css` |
| `redoc/redoc.standalone.js` | redoc | 2.5.4 | MIT | `https://cdn.jsdelivr.net/npm/redoc@2.5.4/bundles/redoc.standalone.js` |

De versies zijn de versies die de verwijzingen hiervoor oplosten: `npm/chart.js` en
`npm/chartjs-plugin-annotation` zonder pin, `mermaid@10`, en de defaults van FastAPI
(`swagger-ui-dist@5`, `redoc@2`). Mermaid levert geen licentiebestand in zijn npm-pakket;
MIT komt uit `package.json`.

`js/htmx.min.js` (1.9.12) en `js/codemirror-bundle.js` staan om dezelfde reden hier, maar
zijn ouder dan dit document.
