# Sleep-mode: één wake-token per deployment, niet per slaapronde

## Waarom

Gemeten in productie op 16 oktober 2026: twee waker-pods van project `wies` (`pr-460`, `pr-689`) pollden `GET /api/sleep-mode/wies/<deployment>/status` en kregen elke keer `401 Unauthorized`. Wie zo'n PR-omgeving opende, kon hem ook niet wekken, want `wake` gebruikt hetzelfde token.

De keten:

1. Elke keer dat een deployment in slaap gaat, maakt OPI een **nieuw** token: `token.encrypt(token.mint(), project_data)` in `opi/services/catalog/sleep_mode/scheduler.py` (rond regel 175, de SLEEP-tak van de sweeper) en in `flow.py` (rond regel 373, slapen via de knop). Het versleutelde token gaat naar `deployments[].sleep.wake-token` en, ontsleuteld, naar het secret `<app>-waker-token` (`opi/manager/project_manager.py` rond regel 1600).
2. Bij wakker worden gooit OPI het token weg: `service.to_awake` en `service.set_sleep_deadline` schrijven `wake_token=None` (`opi/services/catalog/sleep_mode/service.py`).
3. De waker-pod leest het token via `envFrom` op het secret, dus alleen bij het opstarten. Het secret telt bewust niet mee in de config-hash (`include_in_config_hash=False`, zie het commentaar daar).
4. De waker-Deployment overleeft een wake-sleep-cyclus. Gemeten: de waker-pods van `pr-460` en `pr-689` draaiden sinds 13 oktober, terwijl hun token-secret op 14 oktober twee keer per deployment werd vervangen (`pr-460`: 12:37 en 16:45; `pr-689`: 15:41 en 19:48, commits in de deployments-repo). De Deployment-spec is bij de volgende slaapronde identiek, dus Kubernetes start de pod niet opnieuw, en de pod houdt het oude token.

Resultaat: OPI verwacht het nieuwe token, de waker stuurt het oude, en elke aanroep geeft 401. Een `rollout restart` van de twee wakers loste het direct op (status 200).

## De beslissing

Het token wordt één keer per deployment aangemaakt en daarna hergebruikt, zolang het in het projectbestand staat. Het secret verandert dan niet meer tussen slaaprondes, en een waker-pod die blijft draaien heeft altijd het goede token.

Een gelekt token kan alleen de eigen deployment wekken (zie de docstring van `opi/services/catalog/sleep_mode/token.py`), dus vernieuwen per slaapronde voegt weinig toe. Een checksum van het token op de pod-template is overwogen en afgewezen: de SOPS-ciphertext verandert bij elke render, dus de checksum zou over de plattetekst moeten gaan, en dan staat een afgeleide van het geheim in het manifest.

## Wat bewezen moet worden

1. Twee slaaprondes achter elkaar (slapen, wekken, slapen) leveren **hetzelfde** token op, via de sweeper én via de knop.
2. Het token blijft bewaard als de deployment wakker wordt (`to_awake`) en bij een image-update (`set_sleep_deadline`).
3. Een deployment zonder token krijgt bij de eerste slaapronde er één, zoals nu.
4. Een deployment die buiten de scope van sleep-mode valt (`wake_without_deadline`) verliest het hele `sleep`-blok, inclusief het token, zoals nu.
5. Het projectbestand blijft geldig tegen het schema met een `wake-token` op een wakkere deployment (`tests/test_schrijvers_tegen_het_schema.py`).

## Het werk

1. **Hergebruik bij slapen.** Vervang op beide plekken die nu `token.mint()` aanroepen het aanmaken door "het bestaande token, of anders een nieuw". Doe dat in één helper, niet twee keer inline. Een logische plek is `opi/services/catalog/sleep_mode/token.py` of `service.py`; kies wat de aanroepers het kleinst maakt. De bestaande voorwaarde blijft: alleen een token als er een waker komt (`config.waker` en `select_waker_component` geeft een component).
2. **Bewaren bij wakker worden.** `service.to_awake` en `service.set_sleep_deadline` nemen `current.wake_token` over in plaats van `None`. Pas de docstrings aan ("drops the wake token", "dropping any stale token"). `begin_wake` bewaart het al.
3. **Laat de rest staan.** `wake_without_deadline` blijft alles weggooien. Het renderen van de waker-manifests (`project_manager.py`, alleen bij `sleeping`/`waking`) en het prunen bij wakker worden veranderen niet.
4. **Controleer `state.write`** (`state.py` rond regel 79): een wakkere deployment mét token moet zijn `sleep`-blok houden. Pas dat alleen aan als de toets uit punt 2 faalt.

## Toetsen

In `tests/test_sleep_mode_service.py` en de bestaande sweeper- en flow-tests:

- slapen, wekken, slapen via de sweeper: het opgeslagen token (versleuteld kan verschillen, toets de ontsleutelde waarde, of toets dat `mint` maar één keer is aangeroepen) is na de tweede ronde gelijk aan na de eerste;
- hetzelfde via `flow.sleep` (de knop);
- `to_awake` en `set_sleep_deadline` laten het token staan;
- een deployment zonder token krijgt er een;
- `wake_without_deadline` laat geen `sleep`-blok achter;
- het schema accepteert de wakkere deployment met token.

Draai de toetsen uit de eerste twee punten eerst zonder de fix en noteer in de PR dat ze dan falen.

## Na de uitrol (voor de PR-beschrijving)

Waker-pods die nu al een verouderd token hebben, blijven dat houden: het hergebruik bewaart het token dat nu in het projectbestand staat, en dat is het nieuwste. Na de uitrol eenmalig nagaan welke deployments nog 401 krijgen op `/api/sleep-mode/<project>/<deployment>/status` (in de log van OPI) en die wakers herstarten met `kubectl rollout restart`. Daarna komt het niet meer terug.

## Randvoorwaarden

- Niets uitrollen, geen wijziging aan clusters.
- Geen migratie van bestaande projectbestanden nodig: een bestaand token wordt gewoon hergebruikt.
- Code-stijl volgens `CLAUDE.md`: imports bovenaan, moderne type hints, specifieke exceptions, minimaal commentaar, Nederlandse prose met Engelse vaktermen, nooit em dashes.
- Werk `features/` bij als daar beschreven staat dat het token per slaapronde nieuw is (zoek op `wake-token` en `wake token`).

## Klaar als

- De toetsen hierboven staan er en zijn groen.
- `cd operations-manager/python && uv run ruff check . && uv run ruff format --check . && uv run pyright` is schoon.
- De bestaande testsuite is groen.
