# Cluster editing (open design)

Status: **niet ondersteund**. Bewust gelockt in PR #69 via de
`protected_keys`-lijst in `_save_existing_project` (`router_wizard.py:1892`)
totdat het ontwerp hieronder is uitgewerkt en geïmplementeerd.

## Wat er nu (intentioneel) niet werkt

`project.clusters` is in het edit-mode wizard-formulier niet zichtbaar
(`IDENTITY_EDIT_SECTION` in `forms/visualizers/wizard_sections.py` rendert
alleen `display-name` en `description`). De create-flow heeft wel een
cluster-picker (`IDENTITY_SECTION`), maar zodra het project bestaat is de
clusterlijst effectief immutable. Een form die de waarde tóch zou submitten
wordt door de `protected_keys`-merge in `_save_existing_project` stilzwijgend
genegeerd; het inline commentaar daar verwijst naar dit document.

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
3. **`clusters` verwijderen uit `protected_keys`** in
   `router_wizard.py:_save_existing_project` (verwijder dan ook de tweede
   helft van het inline commentaar dat naar dit document verwijst).
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

## Gerelateerde follow-ups uit dezelfde review (PR #69)

- **`_modal_do_submit` in `router_detail_edit.py:1098-1230`** heeft dezelfde
  blinde merge en geen TOCTOU-recheck als de pre-PR wizard-save. Pre-
  existing, niet door PR #69 geïntroduceerd, maar verdient defensief
  dezelfde behandeling (rol-recheck + protected-key-merge). Eigen PR.
- **Step-POST in `submit_step`** (`router_wizard.py:725`) is ongegate'd
  in edit-modus. Acceptabel zolang de eindbevestiging via
  `_save_existing_project` gaat (dat is nu gegate'd), maar als de
  step-state ooit direct gebruikt wordt voor side-effects (auto-save per
  step bijvoorbeeld) moet de gate ook daar.
