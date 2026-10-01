# argocd-server herstart elke 40 minuten

Zie je in de OPI-logs `Cannot connect to host argocd-server:80 [Connect call failed ...]`, dan is dit vermoedelijk de oorzaak. Deze notitie staat er zodat de volgende die het tegenkomt niet opnieuw hoeft te graven.

## Wat er gebeurt

`argocd-cm` heeft een `dex.config` die naar een secretwaarde wijst (`clientSecret: $oidc.dex.clientSecret`). De gitops-operator herschrijft die waarde in `argocd-secret`. argocd-server watcht dat secret, ziet een wijziging en herstart daarop zijn API-server in-process:

```
dex config modified. restarting
API Server received signal: GracefulRestartSignal
Graceful shutdown of httpS initiated
Graceful shutdown timeout. Exiting...
argocd v3.5.1-rig2 serving on port 8080
```

De pod herstart niet, `kubectl get pod` blijft `Running` met 0 restarts. Alleen de listener in de pod is even weg, dus de service heeft geen endpoint en een verbinding wordt geweigerd binnen milliseconden. Het is dus geen timeout en geen crash, en er staat niets over in de events.

## Hoe vaak

Gemeten in productie over 48 uur (24 en 25 september 2026): 69 herstarts, interval 40m16s (66 gemeten intervallen, min 2412s, max 2459s). Meestal is de herstart binnen dezelfde seconde klaar en merkt niemand er iets van. Twee van de 69 liepen in de graceful-shutdown deadline van 20 seconden, en dan is er 20 seconden geen luisteraar. Dat is ongeveer 1 op de 35.

## Waarom de client hem uitzit

De echte bron is de churn van 40 minuten zelf, maar die zit bij de gitops-operator. Via de capsule-proxy is alleen `rig-prd-operations` zichtbaar en komt de operator-namespace niet in `kubectl get ns` voor. Buiten ons bereik dus. Wat we beheren is de client.

Meer replicas van argocd-server helpt niet: ze watchen hetzelfde secret en herstarten dus tegelijk.

De statuspolls uit de UI herstellen zichzelf bij de volgende poll, dus daar is de fout cosmetisch. De gevaarlijke route is de wachtlus in `opi/manager/argo_manager.py`. `get_application_status` verpakt elke fout in een `RuntimeError`, en de wachtlus gooit `RuntimeError` juist door, omdat een terminale toestand (degraded, sync failed) er niet van te onderscheiden is. Valt argocd-server tijdens een wachtlus 20 seconden weg, dan sneuvelt de hele deployment-task.

Daarom zit de retry in `_retry_on_connect_error` in `opi/connectors/argo.py`. Budget 30 seconden, backoff 1, 2, 4, 8 en dan het restant van het budget, en alleen op `aiohttp.ClientConnectorError`. Waarom juist op die fout en niet op een read-timeout staat in de docstring van die functie.

Na uitputting gaat de oorspronkelijke fout onaangeroerd door, dus wanneer ArgoCD echt weg is verandert er aan de buitenkant niets.

## De uitzondering: alles wat een gebruiker laat wachten

Een interactieve aanroeper bouwt zijn connector met `connect_retry_seconds=0`. Dat zijn `_connect_status_backend` in `opi/api/v2/router.py`, en `dashboard` en `argocd_status_fragment` in `opi/web/router.py`. Een pagina of HTMX-poll die de herstart uitzit hangt voor de gebruiker en laat polls zich opstapelen, terwijl hij niets extra oplevert: de volgende poll toont het antwoord toch. Op /dashboard komt dat budget er bovendien per deployment bij, sequentieel in een enkel verzoek, waar die pagina eerder in milliseconden faalde met "Unknown" in de kolom. Budget 0 betekent ook: niet wachten op de gedeelde login. De token-cache is procesbreed en de login die hem vult staat achter één lock, en die login zit de herstart zelf uit, dus zonder dat zou een interactieve aanroeper alsnog het budget van een achtergrondtaak uitzitten. Loopt er zo'n login, dan haakt hij af en degradeert hij net als bij een login die mislukt. Is de lock vrij, dan krijgt hij hem meteen, want budget 0 weigert het wachten en niet het inloggen. Dat venster is bereikbaar bij processtart en na elke 401, want die maakt de procesbrede cache leeg. Bouw je een nieuwe aanroeper die interactief is, geef hem dan ook budget 0.

Een aanroeper op het standaardbudget wacht wel op die login, zonder eigen grens erop. Zijn eigen budget is daar de verkeerde maat: de houder doet zijn pogingen op t=0, 1, 3, 7, 15 en 30, en de login die lukt kost daarna nog ongeveer 700ms bcrypt, dus een grens van 30 verloopt net voordat de token landt. Afhaken levert dan een 401 en daarmee de `RuntimeError` die de deployment-task sloopt, precies de schade waarvoor de retry er staat. Onbegrensd is het in de praktijk niet: de houder komt binnen zijn eigen budget plus één login (`ClientTimeout` total=30) van de lock af, en het `finally` eromheen geeft hem ook bij een fout vrij.

Op een renderpad staat `opi/services/argocd_overview.py` wel op het standaardbudget, maar die zit in een eigen `asyncio.timeout` van 5 seconden, dus daar ligt de grens al lager dan budget 30 ooit komt.

## Als je hem zelf wil zien

```bash
kubectl logs -n rig-prd-operations deployment/argocd-server | grep -E "restarting|Graceful shutdown"
kubectl get secret argocd-secret -n rig-prd-operations -o json | jq '.metadata.managedFields[].time'
```

De tijd van de schrijfactie op `argocd-secret` valt samen met de `restarting`-regel in de log.
