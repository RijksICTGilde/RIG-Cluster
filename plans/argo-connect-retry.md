# Retry op connectiefouten naar ArgoCD

## Aanleiding

Op 2026-09-25 om 10:43:04 CEST gaf OPI in productie vier keer achter elkaar deze fout, op `bouwm-6gn-main`:

```
Cannot connect to host argocd-server:80 [Connect call failed ('172.30.234.18', 80)]
```

Geen timeout: de calls faalden binnen 2 tot 3 ms, de service had simpelweg geen endpoint. De pod `argocd-server-5f8ddfdf8d-jvnjn` draaide op dat moment 2 dagen met 0 restarts, dus er ging niets om. Wat wel gebeurde staat in de containerlog:

```
08:43:04Z  dex config modified. restarting
08:43:04Z  API Server received signal: GracefulRestartSignal
08:43:04Z  Graceful shutdown of httpS initiated
08:43:24Z  Graceful shutdown timeout. Exiting...
08:43:24Z  argocd v3.5.1-rig2 serving on port 8080
```

argocd-server herstart zijn API-server in-process. De aanleiding is dat `dex.config` in `argocd-cm` naar een secretwaarde wijst (`clientSecret: $oidc.dex.clientSecret`) en de operator die waarde in `argocd-secret` herschrijft. De managedFields-tijd van die schrijfactie is exact `2026-09-25T08:43:04Z`.

Dat gebeurt structureel: 69 keer in 48 uur, met een interval van 40m16s (66 gemeten intervallen, min 2412s, max 2459s). Normaal is de herstart binnen dezelfde seconde klaar. Twee van de 69 liepen in de shutdown-deadline van 20 seconden (24-09 19:16:47Z en 25-09 08:43:04Z), en dan is er 20 seconden geen luisteraar. Ongeveer 1 op de 35.

De echte bron is die churn van 40 minuten, maar die zit bij de gitops-operator. Via de capsule-proxy is alleen `rig-prd-operations` zichtbaar, de operator-namespace komt niet in `kubectl get ns` voor. Buiten ons bereik dus. Wat we wel beheren is de client.

## Waarom dit meer is dan cosmetiek

Vandaag waren alle vier de fouten `req-*`, statuspolls vanuit de UI. Die herstellen zichzelf bij de volgende poll. De gevaarlijkere route is de wachtlus in `opi/manager/argo_manager.py`:

- `get_application_status` vangt alles af met `except Exception` en gooit het door als `RuntimeError`.
- De wachtlus heeft op regel 1337 `except RuntimeError, TimeoutError: raise`.
- Die clausule staat er voor terminale toestanden (degraded, sync failed), maar een connectiefout is niet van een terminale toestand te onderscheiden en wordt dus net zo hard doorgegooid.

De generieke `except Exception as e: ... retrying` daaronder wordt daardoor nooit bereikt voor dit geval. Valt argocd-server tijdens een wachtlus 20 seconden weg, dan sneuvelt de hele deployment-task. Dat is nu nog niet gebeurd, maar het is dezelfde dobbelsteen.

## Scope

Retry op connectiefouten in `operations-manager/python/opi/connectors/argo.py`. Verder niets.

## Ontwerpbeslissingen

**Alleen `aiohttp.ClientConnectorError`.** Dat is precies de fingerprint hierboven: de TCP-verbinding is nooit tot stand gekomen, dus het verzoek heeft de server niet bereikt. Daarmee is opnieuw proberen veilig voor elke methode, ook voor de POST van `sync_application`. Read-timeouts en `ServerDisconnectedError` worden bewust niet meegenomen: daar kan het verzoek al verwerkt zijn en is een retry niet idempotent.

**Budget van 30 seconden.** Het gat wordt aan de bovenkant begrensd door argocd-server zelf: de graceful-shutdown deadline is 20 seconden, en beide waargenomen gaten waren precies dat. 30 seconden dekt dat met marge. Een korter budget van een seconde of vijf zou de meting niet halen en dan is de wijziging theater.

**Backoff 1, 2, 4, 8, 16.** Zes pogingen, samen 31 seconden slaap. Connect-fouten zelf kosten milliseconden, dus dat is ook ongeveer de wandkloktijd.

**De UI faalt snel.** `_connect_status_backend` in `opi/api/v2/router.py` bouwt zijn connector met budget 0. Zonder dat zou een HTMX-statuspoll 30 seconden hangen in plaats van een foutje te tonen dat bij de volgende poll weg is, en polls gaan zich opstapelen. De fetch zelf is al concurrent (`asyncio.gather`), dus de worst case is 30 seconden, niet N maal 30, maar voor een interactieve poll is dat nog steeds de verkeerde afweging.

**Niet in `_perform_login`.** Die synchrone login draait `requests.post` blokkerend op de event loop. Daar 30 seconden slapen legt het hele proces plat. De aanroeper degradeert al netjes: `auth_token` blijft None en `_connect_status_backend` geeft een 503. Blijft zoals het is.

**Na uitputting de oorspronkelijke fout.** De laatste `ClientConnectorError` wordt onaangeroerd doorgegooid, zodat `get_application_status` hem net als nu in een `RuntimeError` verpakt. Geen gedragsverandering aan de buitenkant wanneer ArgoCD echt weg is.

## Taken

1. **Retryhelper in `opi/connectors/argo.py`.** Modulconstante voor het budget, plus een kleine helper die een awaitable opnieuw probeert bij `ClientConnectorError` met de backoff hierboven en bij uitputting de laatste fout doorgooit.
   Verifieer: `uv run pyright` schoon.

2. **Toepassen op de twee async-paden.** Om het verzoek in `_make_authenticated_request` en om de login-post in `login()`. De bestaande 401-recursie blijft ongemoeid en erft het gedrag vanzelf.
   Verifieer: alle 16 call sites lopen via `_make_authenticated_request`, dus geen aanroeper hoeft mee te veranderen.

3. **Budget instelbaar per connector.** Constructor-parameter `connect_retry_seconds: float = 30.0`, doorgegeven via `create_argo_connector`.
   Verifieer: `uv run pyright` schoon, bestaande aanroepers ongewijzigd.

4. **UI faalt snel.** `_connect_status_backend` in `opi/api/v2/router.py` bouwt met `connect_retry_seconds=0`.
   Verifieer: precies één poging, geen slaap.

5. **Tests in `tests/test_argo_connect_retry.py`.** In de stijl van `tests/test_argo_token_cache.py`, met gepatchte slaap zodat de suite snel blijft.
   - connect-fout gevolgd door succes levert het succes
   - budget uitgeput gooit de oorspronkelijke `ClientConnectorError` door, en `get_application_status` maakt daar een `RuntimeError` van
   - budget 0 doet precies één poging en slaapt niet
   - een POST wordt ook opnieuw geprobeerd
   - een niet-connect-fout wordt niet opnieuw geprobeerd
   Verifieer: `uv run pytest tests/test_argo_connect_retry.py tests/test_argo_token_cache.py tests/test_argo_manager.py tests/test_argocd_wait_outcome.py tests/test_fetch_argocd_status.py -x -q`

6. **Korte notitie in `docs/`.** De churn van 40 minuten en waarom de client hem moet uitzitten, zodat de volgende die dit in de logs ziet niet opnieuw gaat graven.
   Verifieer: geen em dashes, alinea's op één regel.

## Buiten scope

- De operator die `argocd-secret` elke 40 minuten herschrijft. Andere namespace, niet zichtbaar, niet van ons.
- De `except RuntimeError, TimeoutError` op `argo_manager.py:1337`. Die clausule is op zichzelf juist voor terminale toestanden. Met de retry in de connector bereikt een connectiefout hem in de praktijk niet meer. Losser maken zou echte fouten gaan maskeren, dus dat laat ik staan.
- Meer replicas van argocd-server. Ze watchen dezelfde secret en herstarten tegelijk, dus dat helpt hier niet.

## Validatie voor de PR

```
cd operations-manager/python
uv run ruff check . --fix
uv run ruff format .
uv run pyright
uv run pytest tests/test_argo_connect_retry.py tests/test_argo_token_cache.py tests/test_argo_manager.py tests/test_argocd_wait_outcome.py tests/test_fetch_argocd_status.py -x -q
```
