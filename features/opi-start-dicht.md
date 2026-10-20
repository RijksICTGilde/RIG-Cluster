# OPI start dicht

Zonder instelling draait OPI in productiestand: geen debugger, geen bestandsbewaking, geen
proefroutes en geen publieke API-documentatie. Een vergeten instelling valt naar de veilige
kant (BIO2 8.09 configuratiebeheer, 8.31 scheiding van omgevingen).

## Wat er dicht staat

| onderdeel | zonder instelling | waar het aan hangt |
|---|---|---|
| `ENVIRONMENT` | `production`: geen proef- en testroutes | `opi/core/config.py` |
| `DEBUG_MODE` | `production`: geen debugpy, geen hot-reload | `opi/core/config.py` |
| debugpy | luistert op `127.0.0.1:5678` | `opi/server.py`, `__main__` |
| poort 5678 | niet in de image en niet op de Service | `operations-manager/Dockerfile`, `base/service.yaml` |
| LOTC-proefopstelling (`/lotc/...`) | niet geregistreerd | `opi/server.py` |
| testpagina's (`/test-hero`, `/forms/formulier`, `/test-template-variables`, `/example`) | niet geregistreerd | `dev_router` in `opi/web/router.py` |
| `/docs` en `/redoc` | vragen een sessie | `opi/server.py` |
| `/forms/wizard/help/<...>` | rendert alleen helpteksten | `opi/web/router_wizard.py` |

`/openapi.json` blijft publiek: `zad-cli` haalt het document live op zonder token en heeft
er een kopie van in zijn eigen publieke repo, dus afschermen beschermt niets en breekt de
CLI. `/version` en `/static/lotc/...` blijven ook publiek.

## Hoe je de dev-stand aanzet

De proef- en testroutes bestaan alleen als `ENVIRONMENT` `local` of `sandboxed-local` is
(`settings.is_development_environment`). Zonder de variabele staat er `production`, dus elke
omgeving die de dev-stand wil, zet hem zelf: de sandbox-overlay zet `sandboxed-local`,
`docker-compose.dev.yaml` zet `local`, de basisdeployment zet `production`, en
`tests/conftest.py` zet `local` voor de testrun. `tests/test_start_dicht.py` borgt de
default, de basisdeployment en de dev-compose.

Draai je OPI met de hand, zet hem dan mee:

```bash
cd operations-manager/python && ENVIRONMENT=local uv run python -m opi
```

Hot-reload en de debugger zet je met `DEBUG_MODE`:

- `docker-compose.dev.yaml` zet `reload`;
- de sandbox-overlay zet `reload` (`patches/enable-hot-reload.yaml`);
- `task sandbox:skaffold-debug` gebruikt de overlay `sandboxed-local-debug`, die
  `DEBUG_MODE=debug` zet en poort 5678 aan de Service en de pod toevoegt
  (`patches/enable-debug.yaml` plus `patches/enable-debug-service.yaml`). De debugger
  luistert in de pod op localhost; `kubectl port-forward` en dus ook de port-forward van
  Skaffold komen daar bij.

## Tests

`tests/test_start_dicht.py`: de default van `DEBUG_MODE`, de registratie van de proef- en
testroutes per `ENVIRONMENT`, de sessie op `/docs` en `/redoc`, het publieke
`/openapi.json`, de helpteksten die de help-route wel en niet rendert, en de
`ENVIRONMENT`-waarde in de basisdeployment.
