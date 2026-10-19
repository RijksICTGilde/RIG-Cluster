# Delete overleeft een git-storing

## Aanleiding

Op 13-10-2026 kon OPI in productie van 08:36 tot 13:59 UTC github.com niet bereiken (`Failed to connect to github.com port 443 ... Could not connect to server`). Elke `delete_deployment` in dat venster faalde op de clone van argo-applications ("Error deleting ArgoCD application file: Failed to clone repository with all strategies").

Wat goed ging: de delete laat de deployment dan bewust in het projectbestand staan, zodat een retry hem kan afmaken (RC-254). Er ontstonden geen wezen.

Wat misging:

1. **De retry op taakniveau is te kort.** `TASK_WORKER_MAX_ATTEMPTS = 3` en `AsyncTaskService` zet een mislukte taak meteen terug op `pending`, zonder wachttijd. Alle pogingen waren binnen ongeveer 15 minuten op, de storing duurde ruim vijf uur. Daarna stond de taak op `failed` en vroeg niemand hem opnieuw aan.
2. **Herstel hing af van de aanroeper.** pr-361 (gemerged 13-10 11:03) bleef in vier projecten (`mpfpsm-lcl`, `mpfm-w3h`, `mpfb-8wh`, `mpfs-rab`) draaien tot de CI van het project de delete op 14-10 opnieuw stuurde. Zonder die tweede aanvraag was hij blijven staan.
3. **De clone-fallbacks klonen in een vuile map.** Faalt strategie 1 van `GitConnector.ensure_repo_cloned` (`opi/connectors/git.py`) halverwege, dan klonen strategie 2, 3 en 4 in dezelfde, niet-lege werkmap. Op 13-10 gaf dat bij `nerds-9sr` pr-446 `fatal: destination path '.' already exists and is not an empty directory`. Die fallbacks kunnen bij een gedeeltelijke clone dus nooit slagen.

## Doel

Een delete die faalt doordat git tijdelijk onbereikbaar is, wordt afgemaakt zodra git terug is, zonder dat een mens of de CI hem opnieuw hoeft te sturen. Een deployment die intussen opnieuw is aangemaakt, wordt daarbij nooit verwijderd.

## Plan

Namen hieronder zijn voorstellen, geen bestaande code, tenzij ze als bestaand worden genoemd.

1. **Clone-fallbacks in een schone map.** In `ensure_repo_cloned`: maak de werkmap leeg vóór elke fallbackstrategie.
   → toets: unit test die strategie 1 een halve clone laat achterlaten; strategie 2 of 3 slaagt dan.
2. **Korte retry met backoff op transient git-fouten.** Voor clone en push: herken netwerkfouten (`Failed to connect to`, `Could not resolve host`, `Connection timed out`, `The requested URL returned error: 5xx`) en probeer het nog een paar keer met oplopende wachttijd, bijvoorbeeld 3 pogingen in ongeveer een minuut. Gebruik `tenacity`; dat zit al in de dependencies en wordt al gebruikt in `opi/connectors/kubectl.py`. Auth-fouten en "repository not found" krijgen geen retry. Dit vangt korte haperingen op, niet een storing van uren.
   → toets: unit tests met een gemockte `_run_git_command`: een transient fout gevolgd door een succes slaagt, een auth-fout faalt meteen.
3. **Mislukte deletes 's nachts opnieuw starten.** Breid de bestaande `reconciliation_scheduler` (nachtelijke run) uit met een stap die `delete_deployment`-taken van de afgelopen 7 dagen met status `failed` opnieuw in de wachtrij zet. Dat valt binnen het doel van die scheduler: werk afmaken dat iemand al heeft aangevraagd.
   **Guard (verplicht):** sla een deployment over als
   - hij niet meer in het projectbestand staat (delete is al gelukt), of
   - er na de mislukte delete een nieuwere taak voor dezelfde project/deployment is aangemaakt. De CI van nd-j7s doet bij elke push delete + recreate; zonder deze guard verwijdert de nachtrun een deployment die bewust opnieuw is aangemaakt.

   Per deployment komt er hooguit één nieuwe poging per nacht, zodat een delete die structureel faalt geen lus wordt.
   → toets: unit tests voor beide guard-gevallen en voor het gewone geval. Daarna in de sandbox: Forgejo onbereikbaar maken, een delete laten falen, Forgejo terugzetten, de reconciliation via `POST /api/v2/admin/reconciliation/trigger` starten en controleren dat de deployment weg is.
4. **Zichtbaar maken.** De nachtrun logt per deployment of hij opnieuw gestart of overgeslagen is, met de reden.
   → toets: logregels in de sandbox-run uit stap 3.

## Overwogen en afgewezen

- **Wachttijd per taak (`not_before`-kolom plus exponentiële backoff in de wachtrij).** Dit overbrugt een storing sneller dan een nachtrun, maar vraagt een migratie (007), een aanpassing van de claim-query en een backoff-beleid per taaktype. Voor preview-deletes is "uiterlijk de volgende nacht" goed genoeg. Heroverwegen als ook upserts dit nodig blijken te hebben.
- **`TASK_WORKER_MAX_ATTEMPTS` verhogen.** Zonder wachttijd tussen de pogingen verplaatst dat alleen het moment waarop de taak opgeeft. Het verdubbelt de belasting tijdens een storing.

## Buiten scope

- De delete van pr-366/pr-368 op 12-10 die faalde op "Namespace answered for only part of its resource types". Dat is een rechtenkwestie in de tenant, geen git-storing.
- Upserts die op git falen. Die stuurt de CI bij de volgende push opnieuw; op 13-10 slaagden ze allemaal later alsnog.

## Open beslissingen

- Venster van 7 dagen voor het opnieuw starten: past dat, of korter?
- Moet stap 3 ook gelden voor `delete_project`, of alleen voor `delete_deployment`?
