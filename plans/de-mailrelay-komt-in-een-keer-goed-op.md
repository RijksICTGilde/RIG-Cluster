# De mailrelay komt in één keer goed op

**Status**: plan, klaar om te shippen.
**Datum**: 2026-10-13
**Aanleiding**: op 13 oktober 2026 lag de platform-mailrelay (`rig-mail-relay` in `rig-prd-ron`) twee uur plat zonder dat iemand het zag. Elke uitrol van een project met send-email faalde op `Cannot connect to host rig-mail-relay.rig-prd-ron.svc.cluster.local:8080`, en de foutmelding stuurde de gebruiker naar zijn eigen project.

## Wat er gebeurde (gemeten)

- Vanaf ongeveer 08:00 UTC antwoordde de relay traag. De probes hebben de default `timeoutSeconds: 1`, dus ze braken af.
- Stalwart v0.11.8 telt een HTTP-verbinding die sluit voordat het verzoek compleet is als scan. Na 30 per dag (`server.auto-ban.scan.rate`, default `30/1d`) bant hij het IP. Om 09:25:41 UTC: `Banned due to scan ... remoteIp = 10.124.37.96, reason = "connection closed before message completed"`. Dat IP is de node, dus de kubelet.
- De ban staat in de configopslag (tabel `s` in database `mailrelay` op `rig-db`) en overleeft een herstart. Elke probe kreeg daarna een reset: herstartlus, geen endpoints.
- Herstel met de hand: `DELETE FROM s WHERE k = 'server.blocked-ip.10.124.37.96'::bytea` met de eigen rol van de relay, daarna de pod verwijderd. In 30 dagen Loki was dit de enige ban.
- Waarom de relay rond 08:00 traag werd, is niet vastgesteld. Dat valt buiten deze taak (zie onder).

## Wat er al op de tak staat

Commit `445d3304f` op `claude/mail-relay-allowed-ip`: `[server.allowed-ip]` met `10.0.0.0/8`, `172.16.0.0/12` en `192.168.0.0/16` in `infrastructure/bootstrap/infrastructure/mail/controller/base/config.toml`. Getoetst tegen een echte Stalwart v0.11.8 in Docker met 40 afgebroken verzoeken: zonder de regel volgt een `scan-ban` en is `/healthz/live` dood, met de regel blijft hij `200`. Een ongeldige waarde geeft een parse-error, dus de sleutel wordt echt gelezen.

Gelezen in de broncode van v0.11.8, en ook in Docker bevestigd: de accept-lus (`crates/common/src/listener/listen.rs:228`) kijkt alleen naar de blocked-lijst. Allowed-ip voorkomt dus een nieuwe ban, maar heft een bestaande niet op.

## Wat we bouwen

### 1. De relay haalt zijn image op ODCN zonder admission-herschrijving en zonder wachten op een pull-secret

`ron-infrastructure` staat permanent OutOfSync. In git staat `stalwartlabs/mail-server:v0.11.8`, live draait `rcr.rijksapps.nl/dockerhub-rig/stalwartlabs/mail-server:v0.11.8`. Neem het rcr-pad op in `infrastructure/bootstrap/infrastructure/mail/controller/overlays/odcn/kustomization.yaml` (`images:` met `newName`), en zet `imagePullSecrets: dockerhub-rig-robot-pull-secret` expliciet op de pod. Het secret bestaat al in `rig-prd-ron`, maar hangt op een verse namespace pas na 7 tot 10 minuten aan het `default`-serviceaccount (`plans/private-images-uit-een-eigen-registry.md`). Precedent: `584d40639` (prometheus) en `daaeebf7a`.

### 2. De bootstrap van ODCN zet het sleutelsecret zelf in rig-prd-ron

Vandaag is `sops-age-key` in `rig-prd-ron` handwerk (`features/send-email.md:426-433`, `instructions/sops-sleutel-in-het-cluster.md:20`). Zonder dat secret weigert de CMP te renderen en komt de relay niet. `task bootstrap-argo-system` heeft de sleutel al lokaal (`KEY_FILE`, `Taskfile.yaml:454/468`) en past `namespace-ron.yaml` al toe (`Taskfile.yaml:478-483`). Voeg daar het secret toe met hetzelfde patroon als `sandbox:create-sops-age-secret` (`Taskfile.yaml:2782-2785`: `create secret generic --dry-run=client -o yaml | kubectl apply -f -`). Het moet idempotent zijn en mag een bestaand secret niet roteren.

### 3. OPI richt de platformaccounts in, ook als de relay later opkomt dan OPI

`ensure_platform_mail_account` en `ensure_keycloak_mail_account` draaien één keer bij het opstarten (`opi/core/startup.py:806-809`, "non-critical, no retry"). Bij een verse uitrol is de relay daar meestal nog niet klaar. Dan ontstaan `zad-platform` en `zad-keycloak` pas bij de volgende herstart van OPI, en zonder die accounts gaat er geen platformmail en geen inlogmail uit. Neem beide op in de bestaande `_startup_retry_loop` (`startup.py:702-728`), die dit voor database, projects, keycloak en oauth al doet. Geen tweede retrymechanisme ernaast.

### 4. Een onbereikbare relay is een platformfout, en zo heet hij ook

`MailConnector._request` (`opi/connectors/mail.py:174-212`) laat `aiohttp.ClientError`, `asyncio.TimeoutError` en `OSError` ongemoeid door. Die belanden als `processing_failed` met `error_category: Unknown` bij de CLI.

- In `_request`: vang die drie op en gooi een subklasse van `MailRelayError` met `from e`. Naamvoorstel: `MailRelayUnavailableError`. De tekst zegt dat de mailservice van het platform niet bereikbaar is en dat het niet aan het project ligt. Startup blijft werken, want `MailRelayError` staat al in `_MAILACCOUNT_STARTFOUTEN`.
- Een korte retry op connectfouten, zodat een podwissel van de relay geen uitrol kost. `opi/connectors/argo.py:44-90` heeft dit al (`_retry_on_connect_error`, budget 30 s, getest in `tests/test_argo_connect_retry.py`). Hergebruik die. Moet hij daarvoor uit `argo.py` naar een gedeelde plek, zeg dat dan in de PR.
- In de taakhandlers (`opi/core/task_handlers_project.py:334-353`, `opi/core/task_handlers_operations.py:425-444` en `563-582`): is `project_manager.get_processing_exception()` (`project_manager.py:920`, nu ongebruikt) zo'n fout, zet dan `"error_category": "InternalError"`. `_with_error_category` (`opi/api/task_models.py:534-549`) laat een categorie die de handler zelf zet staan. Het contract voor `processing_failed` blijft verder hetzelfde (`opi/api/v2/models.py:110-120`).

Wat dit nieuw schrijfbaar maakt: niets. Er komt geen veld, endpoint of schrijfweg bij, alleen een andere classificatie van een fout.

### 5. Een melding als de relay niet ready is

Er is in deze repo geen Alertmanager-routering. Een PrometheusRule zou mogelijk in het niets vuren (`features/image-pull-backoff-detection.md:184`). Het enige kanaal waarvan bewezen is dat het aankomt, is ntfy via de log_watcher (`features/log-watcher.md`), en die kijkt alleen naar OPI's eigen ERROR-regels.

Daarom: OPI controleert periodiek `GET /healthz/ready` op `MAIL_RELAY_API_URL`. OPI mag poort 8080 al bereiken (netpol van de relay). Is de relay langer dan 5 minuten niet ready, dan stuurt OPI één bericht naar het bestaande ntfy-topic, en bij herstel één bericht dat hij weer werkt. Geen herhaling per tik.

Hergebruik de scheduler en de ntfy-verzender van de log_watcher (`logwatcher_scheduler.py`, `log_watcher.py`) in plaats van een tweede. Zonder `MAIL_RELAY_API_URL` doet de check niets. Of dit een eigen scheduler-taak wordt of een tik in een bestaande, beslist de bouwer, met uitleg in de PR.

### 6. Een proef die de ban reproduceert

Breid de bestaande mailproef uit (`mail_identity_check.py`; zoek waar hij staat, hij draait tegen de sandbox-relay). Of maak er een tweede check naast als die beter past, met reden in de PR. De proef opent 40 HTTP-verbindingen naar de management-poort en sluit die voordat het verzoek compleet is. Daarna moet `/healthz/live` nog `200` geven. Zonder `[server.allowed-ip]` moet de proef falen. Toets dat één keer en noem het in de PR.

### 7. Documentatie

In `features/send-email.md`:

- Een sectie voor storingen. Symptoom: CrashLoopBackOff, en `security.ip-blocked` in de log. Daarbij de oorzaak, het DELETE-commando met de eigen rol van de relay via `-h localhost`, de pod-delete, en dat allowed-ip een bestaande ban niet opheft.
- De verouderde passages bijwerken: `:386` (draait niet op productie), `:397-398` en `:435-437` (uitgecommentarieerd), `:400` (kop in de toekomstvorm), `:404` (taaknaam is `generate-infrastructure-secrets-for-cluster`), `:445-449` (`MAIL_RELAY_API_URL` staat aan), `:298-301` (herstartmoment van OPI, klopt niet meer na punt 3) en `:426-433` (wordt geautomatiseerd door punt 2).
- Ook verouderd: het commentaar in `bootstrap/rig-system/kustomize/operations-manager/overlays/sandboxed-local/patches/deployment.yaml:110` en `plans/mail-vervolgpunten.md:98-108`.

## Wat we niet doen

- **Een initContainer die bij elke start oude bans wist.** Met punt 1 tot en met 3 kan geen intern adres meer verbannen worden, en de relay is alleen intern bereikbaar. Een oude ban kan alleen nog terugkomen met een restore van `rig-db` van vóór 13 oktober 2026 rond 11:20 UTC. Daarvoor staat het herstel in het runbook. Een extra image en een schrijfactie bij elke start wegen daar niet tegen op.
- **De probe-timeout verruimen.** Met allowed-ip leidt een trage probe niet meer tot een ban. Een ruimere timeout zou alleen echte traagheid verbergen, en de oorzaak van vanochtend kennen we niet (`project_service_monitoring_gap`).
- **De oorzaak van de traagheid rond 08:00 UTC.** Daarvoor ontbreekt metriekhistorie van de relay. Dat is het bredere observability-werk.
- **De kopregel in zad-cli.** De tekst "only got part of the way" komt uit `zad-cli/src/zad_cli/api/errors.py:592-605`, en dat is een andere repo. Met punt 4 wordt het al `Fault.PLATFORM` (bron "ZAD platform", exit 2). Een eigen kopregel is een vervolgtaak in zad-cli.
- **Uitrollen naar productie.** ArgoCD-app `ron-infrastructure` volgt GitHub `main`, en die is uiteengelopen met Forgejo `main`. Het overzetten naar GitHub gebeurt na de merge, met de hand en via een PR.

## Klaar als

- `kustomize build infrastructure/bootstrap/infrastructure/mail/controller/overlays/odcn` toont het rcr-image en `imagePullSecrets: dockerhub-rig-robot-pull-secret`, en de base toont `[server.allowed-ip]`.
- `task bootstrap-argo-system CLUSTER_TYPE=odcn-production` zet `sops-age-key` in `rig-prd-ron`. Getoetst met een dry-run of een unittest op de taak. Een tweede run verandert niets.
- Een unittest bewijst dat de startup-retry de mailaccounts opnieuw probeert na een `ClientConnectorError`, en stopt na succes. Hergebruik de fixture uit `tests/test_send_email_service.py:795-831`.
- Een unittest bewijst dat een `ClientConnectorError` uit de relay tijdens een deploy of refresh eindigt als `error_category: InternalError`, met een tekst die zegt dat het niet aan het project ligt. Breid `tests/test_error_category_on_failed_task.py` uit. De bestaande parametrisering voor `processing_failed` blijft `Unknown`.
- Een unittest bewijst dat de readiness-check precies één ntfy-bericht stuurt na 5 minuten niet ready, en één bij herstel.
- De proef uit punt 6 is groen tegen de sandbox-relay, en rood als `[server.allowed-ip]` eruit is gehaald.
- `uv run ruff check .`, `uv run ruff format .` en `uv run pyright` zijn schoon.
- Geen em dashes in nieuwe tekst.
