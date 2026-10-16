# Event-loop lag watchdog

OPI draait FastAPI, de TaskWorker (12 gelijktijdige taken) en alle schedulers op EEN
asyncio event loop. Elke synchrone blokkade raakt dus alles tegelijk: webverzoeken, alle
lopende taken, de schedulers. De watchdog maakt zo'n stilstand zichtbaar en wijst de regel
aan die hem veroorzaakt.

Aanleiding: op 15 oktober 2026 stond de loop in productie 14 seconden stil. Een GET naar
ArgoCD van een andere taak liep in die tijd af op zijn timeout van 10 s, die taak faalde,
en de gebruiker kreeg een fout te zien terwijl de wijziging wel was doorgevoerd. Het enige
spoor was de AFWEZIGHEID van logregels. De health-probes merken het bewust niet: die
draaien op een eigen draad (`opi/core/probe_server.py`).

## Wat je ziet

### De metrieken

```
opi_event_loop_lag_seconds_total      counter, seconden
opi_event_loop_lag_current_seconds    gauge, seconden
opi_event_loop_stalls_total           counter, aantal
```

`opi_event_loop_lag_seconds_total` is alle achterstand die de loop sinds het starten heeft
opgelopen. Niet de piek sinds de vorige scrape: `/metrics` staat in `SKIP_AUTH_EXACT` en
poort 8000 is open, dus een waarde die bij het lezen wegvalt zou door elke willekeurige
lezer (een scanner, een `curl`) weggenomen worden voordat Prometheus scrapet. Een counter
kun je zo vaak lezen als je wilt; het verschil over een periode haal je met `increase()`:

```promql
increase(opi_event_loop_lag_seconds_total[5m])
```

Dat is het aantal seconden dat de loop in die vijf minuten niet aan zijn werk toekwam. Een
gezonde loop blijft onder een paar honderd milliseconden per vijf minuten. Zie je er tien,
dan draait er een stap synchroon op de loop.

`opi_event_loop_lag_current_seconds` is de achterstand op het moment van de scrape. De
counter krijgt een stilstand pas als de loop weer draait, want de hartslag telt hem dan
op; staat de loop tijdens de scrape nog stil, dan is hij hier al te zien.

`opi_event_loop_stalls_total` telt hoe vaak de achterstand boven de drempel uitkwam, een
per stilstand, en komt uit dezelfde bron als de WARNING hieronder:

```promql
increase(opi_event_loop_stalls_total[1h])
```

### De waarschuwing

Boven de drempel komt er EEN WARNING per stilstand, met de stack van de hoofdthread:

```
De event loop staat 14.2s stil. Stack van de hoofdthread:
  File "/app/opi/manager/delete_project_manager.py", line 2806, in cleanup_removed_services_from_yaml_change
    was_used = file_handler.deployment_uses_service(previous_yaml, dep_name, svc_values)
  File "/app/opi/handlers/project_file_handler.py", line 878, in extract_value_by_path
    jsonpath_expr = compiled_jsonpath(path)
  ...
```

De ONDERSTE regels zijn waar de loop op dat moment staat; dat is de blokkerende stap. De
regels erboven zijn hoe hij daar kwam. Zoek van onderaf de eerste regel die een subprocess
start, een synchrone HTTP-client gebruikt, of een grote lus zonder `await` doorloopt.

Niet elke 100 ms opnieuw: zolang dezelfde stilstand duurt blijft het bij die ene regel. De
volgende waarschuwing komt pas als de loop er eerst weer onder is geweest.

De log-watcher pikt WARNING niet op als fout, dus hier komt geen melding van. Dat is
bewust: een stilstand is een signaal om naar te kijken, geen storing die wakker belt.

## De drempel

`EVENT_LOOP_LAG_WARNING_SECONDS` in `opi/core/config.py`, standaard `1.0` seconde. Te
zetten als env-var of in de ConfigMap, zoals elke andere instelling. De WARNING en
`opi_event_loop_stalls_total` hangen eraan; `opi_event_loop_lag_seconds_total` telt elke
achterstand, ook die van 0,2 s, zodat de trend zichtbaar blijft zonder log-ruis.

## Hoe het werkt

`opi/core/loop_watchdog.py`, gestart in de lifespan van `opi/server.py` direct na de
metrics, gestopt vlak voor het einde.

- Een **hartslagtaak** op de loop zet elke 100 ms een tijdstempel. Komt hij later aan de
  beurt dan dat, dan is het verschil boven de 100 ms de tijd dat de loop niet aan hem
  toekwam, en dat weet hij exact.
- Een **daemon-draad** leest dezelfde tijdstempel, ook elke 100 ms. Die moet een draad
  zijn: tijdens de stilstand is er geen taak die iets kan melden, en juist dan is de stack
  van de hoofdthread het antwoord.

Alleen de hartslag telt seconden op in de counter, zodat de bewaker dezelfde stilstand
niet een tweede keer meetelt. De bewaker verhoogt alleen de teller van stilstanden en
schrijft de WARNING.

## Verwant

- `opi/core/probe_server.py`: waarom de health-checks op een eigen draad staan.
- `tests/loop_meeloper.py`: het meetgereedschap waarmee tests toetsen dat een stap de loop
  niet vasthoudt, in plaats van te toetsen of `asyncio.to_thread` wordt aangeroepen.
