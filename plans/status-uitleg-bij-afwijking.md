# Status-uitleg bij afwijking: ZAD vertelt waarom het niet groen is

## Aanleiding

Bij mb-docs-helmfile stond de deploymentkaart in ZAD op OutOfSync/Progressing terwijl ArgoCD tegelijk "Sync OK" meldde. Alle zeven Deployments draaiden gezond en exact volgens git; de enige afwijking waren twee oude Jobs die in verwijdering hangen (ODCN-webhookbug, zie memory `odcn-admission-image-rewrite`). Dat was in ZAD niet te zien: de kaart toonde twee gele badges zonder enige verklaring, en de gebruiker kon niet beoordelen of productie stuk was of dat het om restafval ging.

De oorzaak zit in `opi/services/deployment_diagnostics.py`: `gather_deployment_errors` verzamelt alleen *fouten* (Degraded/Missing, Progressing mét message, SyncFailed, conditions, events). Twee gaten:

1. Een resource die alleen **OutOfSync** is (sync-diff, geen health-probleem) komt nergens in beeld. De sync-status van de app wordt getoond, maar nooit *welke* resources afwijken of waarom.
2. Een resource die **Progressing zonder message** is (zoals een Job in Terminating) wordt bewust weggefilterd. De app-health wordt er wel door meegetrokken, dus de kaart wordt geel zonder verklaring.

## Principe

Geen ruis. Bij Synced/Healthy verandert er niets aan de kaart: geen extra regels, geen extra API-calls. Alleen wanneer de status afwijkt komt er een compacte verklaring bij: welke resources wijken af en waarom. De bestaande foutenlijst ("N problemen gevonden") blijft ongewijzigd; afwijkingen zijn een aparte, lichtere categorie, want "moet nog opgeruimd worden" is geen probleem-met-je-applicatie.

## Wat er al ligt

- `_fetch_argocd_deployment_status` (`opi/web/router.py:2022`) haalt de Application-status op en bouwt het statusdict voor de kaart; `status.resources[]` met per-resource sync/health/requiresPruning zit al in die payload en wordt nu weggegooid.
- De kaart is `opi/templates_lotc/bg/_argocd-deployment-card.html.j2`: badges (regel 194-205), "Laatste sync" (regel 212) en de fouten-accordion (regel 275).
- De V2 read-API (`opi/api/v2/router.py:302`) gebruikt dezelfde diagnostics; `deployment_diagnostics.py` noemt zichzelf de single source of truth voor beide.

## Werkpakketten

### WP1: afwijkingen verzamelen in diagnostics

Nieuwe functie in `opi/services/deployment_diagnostics.py`, werknaam `gather_sync_deviations(status_data)` (naam is een voorstel). Puur op de al opgehaalde Application-status, geen extra API-calls. Regels:

- App-sync is OutOfSync: één entry per resource uit `status.resources[]` met `status == "OutOfSync"`.
  - `requiresPruning` waar: reden "staat niet meer in git en wordt opgeruimd". Als de laatste sync-operatie Succeeded is en de resource staat er nog steeds (het mb-docs-geval): reden "verwijderd, maar het cluster maakt de verwijdering niet af" plus de leeftijd als die afleidbaar is.
  - anders: reden "wijkt af van git; wordt bij de volgende sync bijgewerkt" (of "auto-sync staat uit" wanneer `spec.syncPolicy.automated` ontbreekt).
- App-health is Progressing en `gather_deployment_errors` gaf niets: één entry per resource met health Progressing, ook zonder message, reden "nog bezig". Zo is een gele health-badge nooit meer onverklaard.
- Entries van disabled components worden gefilterd met dezelfde `_friendly_resource_name`-aanpak als in `gather_deployment_errors`.

Vorm per entry: `{"resource": "Job/docs-backend-migrate-1786315497", "reason": str, "kind": str}`. Verify: unittest met een vastgelegde payload van het mb-docs-geval (2 jobs requiresPruning plus OutOfSync, op Succeeded, health Progressing) die exact 2 entries met de vastloper-reden oplevert en een lege foutenlijst; en een groene payload die een lege lijst oplevert.

### WP2: router geeft afwijkingen door

`_fetch_argocd_deployment_status` berekent `deviations` alleen wanneer sync niet Synced is of health niet Healthy zonder gevonden fouten, en zet ze als apart veld in het statusdict. Niet mengen met `errors`: die lijst voedt `interpret_argocd_errors` en de probleem-telling. Verify: bestaande tests voor de route blijven groen; nieuwe test dat een gezonde status geen `deviations`-sleutel berekent.

### WP3: de kaart legt het uit

In `bg/_argocd-deployment-card.html.j2`, direct onder de badges en alleen als er afwijkingen zijn: een compacte lijst "Waarom niet groen", één regel per resource (vriendelijke naam plus reden), maximaal 5 regels en daarna "en N meer". Daarnaast één verbindende regel voor de verwarrende combinatie uit de aanleiding: bij sync OutOfSync met laatste operatie Succeeded wordt "Laatste sync" aangevuld tot "Laatste sync geslaagd, N resource(s) wijken nog af". LOTC-regels volgen (c-componenten, geen ul/li buiten c-rich-text). Verify: template-rendertest met het mb-docs-statusdict toont de twee jobregels en de verbindende regel; met een groen statusdict is de output byte-gelijk aan de huidige.

### WP4: V2-API consistent

`StatusError`-model in `opi/api/v2/models.py` krijgt gezelschap van een `deviations`-veld op het statusantwoord (zelfde entries als WP1), gevuld in `opi/api/v2/router.py` naast `gather_deployment_errors`. Verify: v2-statustest op de mb-docs-payload bevat de twee afwijkingen; OpenAPI-schema genereert zonder fouten.

## Buiten scope

- Het projectenoverzicht: de overzichtskaartjes blijven badges-only; uitleg alleen op de detailkaart. Eventueel later.
- ArgoCD-diff-inhoud tonen (welke velden afwijken): vergt een extra managed-resources-call per resource, veel ruis, weinig winst. Niet doen.
- De ODCN-webhookbug zelf: ligt bij ODCN, ticket verstuurd.

## Open beslissingen

1. Naamgeving: `deviations`/"afwijkingen" versus iets als "toelichtingen". Voorstel: afwijkingen.
2. Toon-drempel voor "en N meer": 5 is een gok; bij helmfile-apps met veel resources kan dat te weinig of juist genoeg zijn.
3. Of de verbindende regel ook in de V2-API hoort (als samengestelde tekst) of dat de API alleen de kale entries geeft en de tekst presentatie is. Voorstel: alleen in de template.
