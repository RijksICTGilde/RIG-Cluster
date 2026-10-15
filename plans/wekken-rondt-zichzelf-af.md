# Wekken rondt zichzelf af, en wacht niet op een gezondheid die er nooit komt

## Wat er misgaat

Gemeten op productie op 12-10-2026, `wies/pr-689` in `rig-prd-wies`. Iemand wekte de deployment om 11:37. De app-pods (`pr-689-frontend`, `pr-689-worker`) stonden binnen een minuut op 1/1. Toch toonde ZAD een half uur lang "Deze deployment wordt gewekt uit de slaapstand", ArgoCD-health `Progressing`, en "Health-check gefaald" op `frontend-waker`. De wektaak eindigde om 11:42:59 met `Timed out after 300s waiting for 'wies-pr-689' (pr-689) to sync` en `ArgoCD sync completed with 1 failure(s)`.

De oorzaak zit in het ontwerp van de overgang, niet in de app:

1. De wekker deelt de Service van het app-component (`opi/services/catalog/sleep_mode/manifests.py`, docstring bovenaan). Om uit die Service te stappen zodra de app terug is, keert hij zijn readiness om: `/__zad/ready` geeft dan 503. In de wekkerlog staat dat letterlijk: `app wies/pr-689 is back; waker stepping out of traffic`. Dat is bedoeld en blijft zo (`features/sleep-mode.md`, rond regel 241).
2. Daardoor is de wekker-Deployment niet available zolang hij bestaat. Vóór de app terug is, is de app niet ready, en daarna de wekker niet. De ArgoCD-app wordt in de `waking`-toestand dus **nooit** `Healthy`.
3. `flow.wake` commit `waking` en roept `trigger_reprocessing` aan. Die wacht in `ProjectManager` (`_refresh_and_wait`, `wait_for_application_synced(timeout=300)`) op Synced plus Healthy. Dat loopt dus altijd af op de time-out van 300 s.
4. Pas de sweeper (`scheduler.py`, actie `CHECK_AWAKE`, elke `SLEEP_MODE_SWEEP_MINUTES` = 30 min) zet de toestand op `awake`. Daarbij verdwijnt de wekker en wordt de app `Healthy`. Tot die tijd liegt de UI tot een half uur lang.

## Doel

Een wekactie eindigt binnen enkele seconden nadat de app ready is in `awake`, met de wekker weg, en de taak slaagt. De sweeper blijft het vangnet, maar is niet meer het normale pad.

## Beslist

* **Wekker-readiness blijft omgekeerd.** Die 503 is het mechanisme waarmee de wekker uit de gedeelde Service stapt. Dat los je hier niet op en dat raak je niet aan; `images/zad-waker/` blijft ongewijzigd.
* **Afronden gebeurt door OPI zelf, direct na het wekken**, niet door een kortere sweep-interval. Een kortere interval maakt het venster kleiner, maar de wektaak blijft dan op de time-out lopen.
* **De health-check krijgt geen uitzondering voor een niet-ready wekker.** Dat verbergt het symptoom, en de toestand blijft dan `waking`. Na deze fix bestaat de niet-ready wekker nog maar seconden.
* **Geen dubbele gereedheidscontrole.** `SleepModeScheduler._app_is_ready` bestaat al en is precies de vraag die gesteld moet worden. Die verhuist naar een plek die flow en scheduler allebei gebruiken (voorstel: `flow.py` of een kleine module in het pakket), en de scheduler gebruikt hem daarvandaan. Niet kopiëren.

## De stappen

### 1. De eerste reprocess van een wekactie wacht niet op ArgoCD-health

**Waar**: `opi/services/resource_tuning_service.py` (`trigger_reprocessing`), `opi/manager/project_manager.py` (`process_project_from_git` en de doorgifte naar het ArgoCD-wachtblok rond `_refresh_and_wait`), `opi/services/catalog/sleep_mode/flow.py` (`wake`).

Voeg een parameter toe die het wachten op Synced/Healthy overslaat, standaard aan (dus bestaand gedrag ongewijzigd voor elke andere aanroeper). Volg het pad dat `argocd_resources_changed` al aflegt: `trigger_reprocessing`, `process_project_from_git`, verder naar het wachtblok. Naam is een voorstel: `wait_for_healthy: bool = True`.

Staat hij uit, dan gebeurt nog steeds: manifests genereren, committen en pushen, `refresh_application`, en de render-controle (`get_application_manifests` plus `_looks_like_render_failure`), zodat een kapotte kustomization nog steeds direct faalt. Alleen `wait_for_application_synced` wordt overgeslagen, en de deployment telt niet als sync-fout.

`flow.wake` geeft hem uit mee. Reden in één regel commentaar bij de aanroep: in `waking` wordt de app nooit Healthy, want vóór de app terug is, is de app niet ready, en daarna de wekker niet.

**Verify**: een test op `process_project_from_git` met de vlag uit: `wait_for_application_synced` wordt niet aangeroepen, `refresh_application` en de render-controle wel, en er komt geen sync-fout in het resultaat. Met de vlag aan (standaard) blijft het bestaande gedrag gelijk; de bestaande tests daarvoor blijven groen.

### 2. `finish_wake`: wacht op de app, zet `awake`, ruim de wekker op

**Waar**: `opi/services/catalog/sleep_mode/flow.py` (nieuw), `scheduler.py` (`_app_is_ready` verhuist hiernaartoe of naar een gedeelde module), `service.to_awake` (bestaat al).

Nieuwe functie, naam is een voorstel: `finish_wake(project_name, deployment_name, *, progress=None) -> WakeResult`.

1. Poll de gereedheid van de app (de verhuisde `_app_is_ready`) met een korte interval (voorstel 5 s), tot de app ready is of de `expires_at` van de `waking`-toestand voorbij is. Die deadline staat al in het projectbestand: `begin_wake` zet hem op nu plus `SLEEP_MODE_WAKING_TIMEOUT_MINUTES` (10 min). Gebruik die, geen nieuwe instelling.
2. Is de app ready: laad het projectbestand opnieuw via het mutatiepad (`ProjectManager.get_contents()`, net als `wake`, niet de cache), roep `service.to_awake(project_data, deployment_name, now, config.sleep_after_wake_delta)` aan, commit, en `trigger_reprocessing` voor die ene deployment **met** het normale wachten. De wekker is dan weg, dus de app kan wel Healthy worden; dat wachten is hier juist de bevestiging.
3. Geeft `to_awake` `False` (de sweeper of een tweede wekactie was eerder, of de toestand is intussen niet meer `waking`), dan is er niets te doen: geen commit, uitkomst `changed=False` met de huidige toestand.
4. Is de app niet ready voor de deadline: niets wijzigen. De sweeper doet daarna `REVERT`, zoals nu. De taak meldt het met een duidelijke zin (voorstel: "De applicatie kwam niet binnen 10 minuten terug; de slaapstand-sweeper zet de deployment terug op wakker") en eindigt als mislukt, want het wekken is werkelijk niet gelukt.

Een `ConflictError` bij het opslaan wordt niet ingeslikt maar ook niet opnieuw geprobeerd: de sweeper rondt het af. Log het op info, zoals de sweeper dat ook doet.

Stappen benoemen via de bestaande `_Steps`-helper, zodat de taakpagina "Wachten tot de applicatie opgestart is", "Wektoestand afronden in git" en de reprocess-stappen toont.

De sweeper gebruikt na de verhuizing dezelfde gereedheidsfunctie. Het gedrag van `CHECK_AWAKE` en `REVERT` blijft gelijk.

**Verify**: zie stap 4.

### 3. Beide wekpaden roepen `finish_wake` aan

**Waar**: `opi/services/catalog/sleep_mode/task.py` (`handle_sleep_transition`) en `opi/services/catalog/sleep_mode/router.py` (de `wake`-route die de wekker-pod aanroept).

* **Taak (knop in de UI)**: na `flow.wake`, als `result.changed` en `result.state == STATE_WAKING`, `await flow.finish_wake(..., progress=progress)`. De taakuitkomst is die van `finish_wake`. De gebruiker kijkt mee, dus het wachten hoort in de taak.
* **API-route (wekker-pod, wektoken)**: het antwoord mag niet langer duren dan nu. De wekker pollt `/status` en heeft zijn eigen time-outs. Plan `finish_wake` daarom na het antwoord in met FastAPI `BackgroundTasks` (geen losse `asyncio.create_task` zonder referentie, die kan door de garbage collector verdwijnen). Ook alleen als `result.changed` en de toestand `waking` is. Het 202/200-contract en de response-velden blijven ongewijzigd: `status_contract` is bevroren omdat het wekker-image ouder kan zijn dan OPI.

Er is geen nieuwe schrijfweg en geen nieuwe poort nodig: `finish_wake` schrijft alleen `deployments[].sleep` via `service.to_awake`, hetzelfde veld dat de sweeper nu al schrijft. Hij is alleen bereikbaar na een geslaagde `flow.wake`, dus na de bestaande sessie- of wektokencontrole. Het route-handler-pad krijgt geen eigen authenticatie erbij.

**Verify**: zie stap 4.

### 4. Tests

**Waar**: `operations-manager/python/tests/test_sleep_mode_*.py`. De bestaande bestanden voor service, scheduler, router en status-contract zijn het voorbeeld voor de mocks.

Verplicht te bewijzen:

1. **Het scenario uit productie.** De app-Deployment is ready, de wekker-Deployment niet (readiness 503, dus een ArgoCD-app die niet Healthy wordt). De wektaak via `handle_sleep_transition(direction="wake")` slaagt, eindigt met `state == "awake"`, en heeft twee commits gedaan (`waking`, daarna `awake`). De eerste reprocess is aangeroepen met het wachten uit, de tweede met het wachten aan.
2. **De app komt eerst niet en daarna wel.** Gereedheid geeft eerst `False`, daarna `True`: `finish_wake` wacht en rondt dan af. Geen echte `sleep`; patch de interval of de klok.
3. **De app komt niet terug voor de deadline.** Geen `to_awake`, geen commit, toestand blijft `waking`, de taak faalt met de afgesproken melding.
4. **De sweeper was eerder.** De toestand is al `awake` als `finish_wake` het bestand opnieuw laadt: geen commit, geen tweede reprocess, geen fout.
5. **API-route.** Antwoordt met 202 zonder op `finish_wake` te wachten, en heeft `finish_wake` als achtergrondtaak ingepland. Bij een no-op (200) wordt niets ingepland.
6. **`process_project_from_git` met het wachten uit**, zoals beschreven bij stap 1.
7. **De scheduler** gebruikt de verhuisde gereedheidsfunctie; de bestaande schedulertests blijven groen zonder inhoudelijke wijziging aan hun asserties.

Draai gericht: `uv run pytest tests/test_sleep_mode_*.py -q` plus de tests van `process_project_from_git`/het ArgoCD-wachtblok die je raakt. Daarna `uv run ruff check . --fix`, `uv run ruff format .`, `uv run pyright`.

### 5. Documentatie

**Waar**: `features/sleep-mode.md`.

Beschrijf de afronding van een wekactie: OPI wacht zelf tot de app ready is, zet `awake` en ruimt de wekker op; de sweeper is het vangnet (`CHECK_AWAKE` en `REVERT`). Vermeld in één zin waarom de eerste reprocess niet op ArgoCD-health wacht. Pas de bestaande alinea over de sweeper (rond regel 194) aan, zodat die niet meer suggereert dat de sweeper de normale afronding is.

## Buiten scope

* `images/zad-waker/`. Geen wijziging aan het image, dus ook geen `task publish-waker`.
* De health-check en het "problemen gevonden"-blok in de UI. Na deze fix bestaat de niet-ready wekker nog seconden; blijkt het daarna nog storend, dan is dat een eigen taak.
* Uitrollen naar productie. Dat beslist de gebruiker apart.
