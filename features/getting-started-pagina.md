# Getting started-pagina

Een korte pagina voor wie nieuw is op ZAD: hoe het in elkaar zit, hoe je een hello world neerzet, hoe je code een service gebruikt en hoe je een nieuwe image uitrolt.

## Gebruik

De pagina staat op `/getting-started`, in de zijkolom onder Platform, en is ook te bereiken vanaf `/introductie`. Hij is publiek, net als de introductie, omdat hij juist bedoeld is voor wie nog geen toegang heeft.

De pagina vermeldt dat een webadres later te wijzigen is. Dat klopt omdat de redirect URI's van de Keycloak-client (`KeycloakManager`, via `get_deployment_hostnames`) en de callback van de Authorization Wall bij elke verwerking uit de actuele hostnamen volgen. Zelf ingevoerde `additional_redirect_uris` veranderen niet mee.

## Configuratie

Geen. De pagina heeft geen gegevens en geen instellingen. De tekst staat in `opi/templates_lotc/bg/_getting-started-blokken.html.j2`.

## Voorbeeld

De kernboodschap van de pagina: je richt een service niet zelf in, je leest zijn env vars uit.

```python
psycopg.connect(os.environ["DATABASE_SERVER_FULL"])
```

## Wat er bewust niet staat

Alles wat ergens anders thuishoort: alle opdrachten van zadctl, alle invoerwaarden van de Actions en de env vars van elke service. De pagina linkt daarnaar, zodat hij niet veroudert zoals eerdere handleidingen in de portal. `tests/test_getting_started_pagina.py` controleert dat de genoemde env vars en de genoemde service in de catalogus bestaan.

## Afhankelijkheden

`/services` en de repositories `zad-cli` en `zad-actions` voor de verdieping, en het publieke image `ghcr.io/minbzk/base-images/hello-world`, dat op poort 8080 luistert.
