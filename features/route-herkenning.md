# Route-herkenning: elke route noemt hoe de aanroeper wordt herkend

## Wat het is

Een enumeratie van alle geregistreerde routes van OPI met hun herkenning, plus een test die
afdwingt dat er geen route zonder herkenning bij komt. De autorisatiemiddleware
(`opi/middleware/authorization.py`) geeft elk pad onder `/api/` meteen door naar de handler
en leest voor de rest `_requires_sso` met `False` als default. Zonder deze test kan een
nieuwe route zonder decorator open blijven.

Dit gaat over herkenning, niet over rol: dat de aanroeper herkend wordt, niet dat hij mag
wat hij vraagt. De rolgrens per route is fase 1 van
`features/futures/rechten-plan-van-aanpak.md`.

## De zes manieren waarop OPI een aanroeper herkent

Elke manier is een decorator, en elke decorator zet een markering op het endpoint, zodat de
eis uit de route zelf te lezen is in plaats van uit een lijst met paden.

| Decorator | Markering | Credential |
|---|---|---|
| `requires_sso` (`opi/core/auth_decorators.py`) | `_requires_sso` | SSO-sessiecookie |
| `validate_api_token` (`opi/api/endpoint_util.py`) | `REQUIRES_PROJECT_NAME` | `X-API-Key` van het project |
| `validate_user_token` (`opi/api/user_token_auth.py`) | `REQUIRES_USER_TOKEN` | `Authorization: Bearer` (SSO) |
| `validate_admin_api_key` | `REQUIRES_ADMIN_API_KEY` | `X-API-Key` = `ADMIN_API_KEY` |
| `validate_master_api_key` | `REQUIRES_MASTER_API_KEY` | `X-API-Key` = `MASTER_API_KEY` |
| `validate_metrics_scrape_token` | `REQUIRES_METRICS_SCRAPE_TOKEN` | `Authorization: Bearer` = `PROMETHEUS_METRICS_AUTH_TOKEN` |

## Gebruik

De helper staat in `opi/api/route_herkenning.py`:

```python
from opi.api.route_herkenning import route_herkenningen
from opi.server import app

for route in route_herkenningen(app):
    print(route.methode, route.pad, route.herkenningen)
```

`route_herkenningen(app)` geeft per route een `RouteHerkenning` met `methode`, `pad`,
`module` en `herkenningen`. Websocketroutes krijgen `WEBSOCKET` als methode.

Het staat in `opi/` en niet in de test, omdat meer dan een vraagsteller dezelfde
enumeratie wil: RC-322 (scopes per token) loopt de routes ook langs, en fase 1 wil er een
opstartcontrole op bouwen.

## De test

`tests/test_elke_route_heeft_een_herkenning.py` eist per route precies een herkenning, of
een regel in `PUBLIEKE_ROUTES`: een dict van `(methode, pad)` naar de reden waarom die route
publiek is. Precies een, want twee herkenningen betekent dat niet te zeggen is welke weigert.

Een route toevoegen zonder decorator maakt de test rood, en de enige manieren om hem weer
groen te krijgen zijn een decorator of een regel in `PUBLIEKE_ROUTES`, die een reviewer dan
ziet. Daarnaast toetst het bestand:

- dat geen route onder `/api/` op `_requires_sso` leunt, want de middleware leest die
  markering daar niet. Zo'n route is open en leest in de code alsof hij afgeschermd is;
- dat elke regel in de twee lijsten een route beschrijft die nog bestaat, zodat een
  uitzondering niet achterblijft en later een nieuwe route met hetzelfde pad dekt;
- dat de controle werkelijk rood wordt van een route zonder decorator (op een eigen kleine
  app), zodat een groene uitslag niet kan betekenen dat de controle niets vond.

### Handgeschreven controles

Acht routes controleren de aanroeper wel, maar met een eigen controle in de handler in
plaats van met een decorator. Daardoor is er niets uit de route te lezen en dus niets af te
dwingen. Ze staan in `HANDGESCHREVEN_CONTROLES` en zijn `xfail` met `strict=True`: komt er
een markering op, dan slaat de xfail om in een fout en hoort de regel weg.

Niet hier gerepareerd omdat RC-322 deze handlers al raakt en er een handhavingspunt aan
toevoegt; twee PR's die dezelfde functies herschrijven leveren een conflict op in plaats van
een grens.

## Afhankelijkheden

Geen. De helper leest `app.routes` en de markeringen op de endpoints; er is geen cluster,
database of dienst nodig.
