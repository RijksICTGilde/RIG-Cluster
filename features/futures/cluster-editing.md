# Cluster editing (open design)

Status: **niet ondersteund**. Bewust gelockt in PR #69 via
`PROTECTED_PROJECT_KEYS` in `opi/web/project_edit_security.py` totdat het
ontwerp hieronder is uitgewerkt en geïmplementeerd.

## Wat er nu (intentioneel) niet werkt

`project.clusters` is in het edit-mode wizard-formulier niet zichtbaar
(`IDENTITY_EDIT_SECTION` in `forms/visualizers/wizard_sections.py` rendert
alleen `display-name` en `description`). De create-flow heeft wel een
cluster-picker (`IDENTITY_SECTION`), maar zodra het project bestaat is de
clusterlijst effectief immutable. Een form die de waarde tóch zou submitten
wordt door `merge_preserving_protected_keys` in
`opi/web/project_edit_security.py` stilzwijgend genegeerd. Dezelfde lijst
beschermt nu ook het modal-save-pad (`_modal_do_submit` in
`router_detail_edit.py`).

## Waarom dit niet "even een veld editable maken" is

ZAD draait op een gedistribueerd model: elke cluster heeft zijn eigen
Operations Manager (OPI), en die instance beheert uitsluitend resources voor
zijn eigen `CLUSTER_MANAGER`. Zie ook de architectuur in de root-`CLAUDE.md`.
Een wijziging in `project.clusters` raakt meerdere OPIs op meerdere
clusters en heeft meerdere niet-triviale gevolgen.

### 1. Cluster toevoegen

- De OPI op de **nieuwe** cluster moet het projectbestand zien en zelfstandig
  alles opzetten: namespace, RBAC, ArgoCD-Application, repository-secret,
  NetworkPolicy, eventueel een nieuwe deployment.
- Pickup-mechanisme: ofwel via de bestaande git-monitor (poll), ofwel een
  expliciete reconcile-trigger. Beide werken, maar pickup-tijd is dan
  afhankelijk van poll-interval (nu 120s default).
- Existing deployments wijzen via `deployments[].cluster` naar een specifieke
  cluster. Toevoegen van een cluster maakt geen deployment voor die cluster
  aan — moet de gebruiker dat handmatig doen via een deployment-flow, of moet
  het wizard-formulier een vervolgstap krijgen ("welke deployments wil je op
  de nieuwe cluster"?). Open ontwerpkeuze.

### 2. Cluster verwijderen

- Als er nog deployments naar de te verwijderen cluster wijzen: blokkeren of
  cascade-verwijderen. Cascade is destructief en moet expliciet bevestigd
  worden door de gebruiker, niet een silent side-effect van een formulier-
  save.
- De OPI op de **vertrekkende** cluster moet detecteren "ik ben er niet meer
  bij" en lokaal opruimen (namespace + ArgoCD-Application + repo-secret).
  Vereist een teardown-pad dat nu niet bestaat.
- Distributed-model race: de oude OPI en de nieuwe OPI zien dezelfde
  projectfile-wijziging tegelijk. Geen locking/serialisatie momenteel
  voor cross-OPI coördinatie.

### 3. Migratie tussen clusters

Een specifieke deployment van cluster A naar cluster B verplaatsen is een
ander scenario dan add-or-remove. Dat is buiten scope hier — vergt
data-overzetten (database snapshots, PVCs, secrets) en hoort in een
deployment-migratie-feature, niet in de project-level cluster-list.

### 4. UX-vereisten

- Confirmation-step bij verwijderen ("dit raakt N deployments op cluster
  X"), niet stille submit.
- Visuele feedback dat een toegevoegde cluster nog "leeg" is (geen
  deployments) en hoe de gebruiker dat invult.
- Foutmodus: cluster removal terwijl deployment-tear-down faalt — hoe geven
  we dat terug aan de gebruiker?

## Wat er moet gebeuren bij implementatie

In volgorde:

1. **Ontwerp afronden** voor add, remove en het distributed-model race-
   geval. Document hier verder uitwerken.
2. **`CLUSTERS_EDITABLE` toevoegen aan `IDENTITY_EDIT_SECTION`** in
   `forms/visualizers/wizard_sections.py`.
3. **`clusters` verwijderen uit `PROTECTED_PROJECT_KEYS`** in
   `opi/web/project_edit_security.py` (en de cluster-clausule uit het
   docstring-commentaar boven die constante halen).
4. **Pre-save validatie:** als de gebruiker een cluster verwijdert die nog
   referenties heeft in `deployments[].cluster`, óf blokkeren met duidelijke
   foutmelding óf een aparte confirmation-stap "verwijder ook deze N
   deployments".
5. **Reconciliatie- en teardown-paden** in `project_manager` en
   `argo_manager` voor de "OPI ziet dat zijn cluster nieuw is toegevoegd"
   en "OPI ziet dat zijn cluster verwijderd is" gevallen.
6. **Regressietests:** add-cluster end-to-end (nieuwe OPI pickt het op),
   remove-cluster met deployments aanwezig (geblokkeerd), remove-cluster
   schoon (teardown loopt).

## Form-RBAC: architecturele lacune

PR #69 (en de uitbreiding ervan) plaatst de bescherming op het **save-
handler**-niveau via `require_project_edit_access` + `merge_preserving_
protected_keys` in `opi/web/project_edit_security.py`. Dat is een
pragmatische verdediging, maar geen structurele oplossing.

Het editables-systeem (`opi/forms/editables/editable.py`) heeft geen
field-level RBAC: `Editable` dataclass heeft wel `validator`,
`converter`, `enforcer`, `hooks` en `generator`, maar geen
`requires_role` of vergelijkbaar. De enige role-bewuste `Enforcer` is
`AdminEnforcer` ("≥1 admin moet blijven bestaan") — een business-rule,
geen "wie mag dit veld editen".

Gevolg: élke save-handler die submitted form data merget in een opgeslagen
project moet zelf `require_project_edit_access` aanroepen én
`merge_preserving_protected_keys` toepassen. Vergeet één save-pad en de
bescherming is daar afwezig. PR #69 dekt nu wizard-save, modal-save en
wizard step-submit (in edit-modus); toekomstige save-paden (REST API,
batch-edit, etc.) moeten dezelfde rotor handmatig aansluiten.

**Wenselijke eindstaat:**

1. `Editable` (of `FormSection`) krijgt een `requires_role: str | None`-
   attribuut. Definitie naast de field-definitie zelf — single source of
   truth.
2. Centrale `form_submission_guard` matcht request-user-role tegen field-
   metadata vóór de processor draait. Niet-toegestane waardes worden
   gedropt (of leveren een nette FieldError op) vóór ze de save-handler
   bereiken.
3. Form-renderer kan op basis van dezelfde metadata velden disablen voor
   users zonder permission — heldere UI-feedback in plaats van stille
   drop achteraf.
4. `merge_preserving_protected_keys` kan dan verdwijnen: bescherming zit
   in de validate-stage en is voor alle save-paden automatisch actief.

Dat is een eigen feature/refactor, niet iets dat in PR #69 past.
