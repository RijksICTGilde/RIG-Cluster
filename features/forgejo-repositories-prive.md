# Forgejo-repositories op de sandbox zijn privé

De vier Forgejo-repositories van de sandbox (`zad-projects`,
`zad-argo-user-applications`, `zad-argo-infrastructure`, `zad-deployments`) staan op
privé, en Forgejo vraagt een login voordat het iets laat lezen. Ze bevatten de
projectbestanden met hun AGE-versleutelde waarden, de ArgoCD-applicaties en de
gegenereerde manifests; de sandbox draait op een dev-server onder een publiek
resolvende naam (`docs/sandbox-on-dev-server.md`), dus zonder dit was dat alles
leesbaar voor wie Forgejo kon bereiken (BIO2 8.31).

## Wat er aan staat

| Instelling | Waar |
|---|---|
| `FORGEJO__service__REQUIRE_SIGNIN_VIEW: "true"` | `infrastructure/bootstrap/infrastructure/forgejo/controller/base/statefulset.yaml` en de `sandboxed-local`-patch |
| `FORGEJO__repository__DEFAULT_PRIVATE: "true"` | idem |
| `"private":true` bij het aanmaken van de repositories | `task sandbox:init-forgejo` en `infrastructure/bootstrap/infrastructure/forgejo/config/` |
| `PATCH {"private":true}` per repository | idem (zet een bestaande installatie om) |

`task sandbox:init-forgejo` is de weg die werkelijk loopt: de bootstrap-job in
`forgejo/config/` staat in geen enkele cluster-kustomization. Die job maakt bovendien
twee anders genaamde repositories onder een andere beheerder aan, dus de twee paden
leveren niet hetzelfde op. Wel doen ze het op dezelfde manier: privé bij het aanmaken,
en een `PATCH` erna voor wat er al stond.

Een bestaande sandbox wordt omgezet door `task sandbox:init-forgejo` opnieuw te draaien:
de aanmaak slaat bestaande repositories over, de `PATCH` erna zet ze op privé.

## Wie er nog bij komt

Alles wat Forgejo leest heeft credentials nodig. Vier lezers:

- **OPI** krijgt ze uit zijn configmap (`GIT_PROJECTS_SERVER_USERNAME`/`PASSWORD`,
  `GIT_ARGO_APPLICATIONS_USERNAME`/`PASSWORD`, `PROJECT_REPO_USERNAME`/`PASSWORD` in
  `bootstrap/rig-system/kustomize/operations-manager/overlays/sandboxed-local/configmap.yaml`).
- **De e2e-tests** uit `FORGEJO_USER`/`FORGEJO_PASSWORD` (`tests/e2e/conftest.py`, met
  defaults in de `test-e2e-sandbox*`-taken).
- **De Taskfile** (`sandbox:sync`, `sandbox:import-project`) uit de credentials in
  de clone-URL.
- **ArgoCD** uit twee `repo-creds`-templates, hieronder.

`/api/healthz` blijft zonder login bereikbaar, ook met `REQUIRE_SIGNIN_VIEW=true`
(gemeten: 200). Dat moet ook, want de liveness- en readinessprobe van Forgejo hangen
eraan.

## De ArgoCD-credentials: twee repo-creds-templates

`bootstrap/rig-system/kustomize/overlays/sandboxed-local/argocd-repo-forgejo-secret.yaml`
levert twee secrets met het label `argocd.argoproj.io/secret-type: repo-creds`:

| URL-prefix | Dekt |
|---|---|
| `http://forgejo.rig-system.svc.cluster.local:3000/rig-admin/` | de platform-applicaties: `sandbox-infrastructure`, `ron-infrastructure`, `user-applications` |
| `https://forgejo.sandbox.rijksapp.dev/rig-admin/` | de tenant-applicaties, die via `PROJECT_REPO_URL` naar `zad-deployments` wijzen |

Twee, omdat de sandbox Forgejo onder twee basis-URL's benadert en een template één
prefix dekt.

### Waarom repo-creds en niet repository

Een secret met `secret-type: repository` geldt alleen voor de **exacte** URL die erin
staat. Het oude `forgejo-repo-secret` had als URL alleen de server
(`http://forgejo.rig-system.svc.cluster.local:3000`, zonder pad) en matchte daardoor
geen enkele applicatie: ArgoCD las de repositories gewoon anoniem, en dat viel niet op
zolang ze publiek waren. Een `repo-creds`-template matcht op prefix en dekt zo elke
repository onder `/rig-admin/`.

### De valkuil: userinfo in de repoURL

ArgoCD vergelijkt die prefix **letterlijk** tegen de repoURL, userinfo inbegrepen. Een
repoURL van de vorm

    https://{project}@forgejo.example/rig-admin/zad-deployments.git

matcht dus **niet** op `https://forgejo.example/rig-admin/`. Die vorm komt van
`make_argocd_repository_url_unique` (`opi/utils/naming.py`), die de projectnaam als
userinfo in de URL zet om ArgoCD's "Found multiple credentials for repoURL" te
voorkomen. Hij herschrijft alleen `https://`-URL's op github.com, gitlab.com en
bitbucket.org, dus:

- **sandbox**: de tenant-URL's staan op `forgejo.sandbox.rijksapp.dev` en dragen geen
  userinfo. De template matcht, en dit is daarmee het werkende voorbeeld.
- **odcn-production**: de tenant-URL's staan op github.com en dragen de userinfo wel.
  Een template zou daar nog niet matchen.

Dat laatste is precies de voorwaarde die stap 3 van
`features/futures/argocd-applicationset-migration.md` noemt: de 36 per-project
repository-secrets zijn pas te vervangen door één template als de repoURL's hun
userinfo kwijt zijn, en de template moet er staan vóórdat dat gebeurt.

### De per-project repository-secrets blijven

OPI schrijft per project nog een secret `{project}-main-repo` van het type
`repository`, met de exacte URL en dezelfde credentials. Die blijven staan en gaan
voor: ArgoCD pakt de repository-match en valt alleen op de template terug als die geen
credentials draagt. De template is er voor applicaties zonder zo'n secret, en als
voorbeeld voor de ApplicationSet-migratie.

Op de sandbox wijzen al die secrets naar dezelfde URL
(`https://forgejo.sandbox.rijksapp.dev/rig-admin/zad-deployments.git`), want elk project
deelt `zad-deployments`. Ze zijn dus onderling uitwisselbaar: gemeten met zeven
tenant-projecten blijft een applicatie syncen nadat haar eigen secret weg is, omdat de
zes andere dezelfde exacte URL dekken. Dat is dezelfde dubbeling die stap 3 van de
ApplicationSet-migratie opruimt.

## Toetsen

De twee instellingen zijn twee lagen, en de statuscode zegt welke greep:

| Anoniem op de API van een privé-repository | Code |
|---|---|
| alleen `private: true` | **404** |
| ook `REQUIRE_SIGNIN_VIEW: "true"` | **403** |

`REQUIRE_SIGNIN_VIEW` grijpt vóór de privé-controle en geldt ook voor een repository die
nog publiek staat; `private` sluit de repository zelf en blijft gelden als die eerste
instelling ooit losser gezet wordt. Een anonieme `git clone` vraagt in beide gevallen om
een gebruikersnaam en faalt.

```bash
# anoniem: 404 of 403
curl -s -o /dev/null -w '%{http_code}\n' \
  https://forgejo.sandbox.rijksapp.dev/api/v1/repos/rig-admin/zad-projects

# met credentials: 200
curl -s -o /dev/null -w '%{http_code}\n' -u "rig-admin:$FORGEJO_PASSWORD" \
  https://forgejo.sandbox.rijksapp.dev/api/v1/repos/rig-admin/zad-projects
```

Let bij de 404 op `ForgejoClient.list_project_names`
(`tests/e2e/helpers/forgejo.py`): die vat een 404 op als lege verzameling, dus een
ontbrekende toegang zou daar als een leeg `zad-projects` doorkomen. Op de 403 van
`REQUIRE_SIGNIN_VIEW` slaat `raise_for_status()` wel aan. Dat is de reden om beide lagen
aan te houden en niet alleen `private`.

Dat ArgoCD de credentials vindt:

```bash
kubectl get applications -n rig-system \
  -o custom-columns='NAME:.metadata.name,SYNC:.status.sync.status,HEALTH:.status.health.status'
```

Alle applicaties horen `Synced` te staan, de platform- en de tenant-applicaties. Haal je
de template weg, dan meldt een platform-applicatie na een harde refresh
`failed to list refs: authentication required` en is de template dus werkelijk wat de
toegang levert. Een tenant-applicatie blijft in die proef syncen op haar eigen
per-project secret; om alleen de template te meten gebruik je een applicatie op een
repository waarvoor geen per-project secret bestaat, bijvoorbeeld `zad-projects`.

Een refresh forceer je per applicatie met:

```bash
kubectl annotate application -n rig-system <app> argocd.argoproj.io/refresh=hard --overwrite
```

## Afhankelijkheden

- Forgejo 14 (rootless) met het admin-account `rig-admin`. De image draagt `curl`, wat
  de `PATCH` nodig heeft: de busybox-`wget` ernaast kan geen PATCH doen.
- ArgoCD 3.x voor de prefixmatch op `repo-creds`.
- Het wachtwoord `admin1234` is een vaste sandboxwaarde, op vier plekken vastgelegd
  (statefulset-init, `sandbox:init-forgejo`, de OPI-configmap en de twee repo-creds).
  Het genereren daarvan is een eigen taak en valt hierbuiten.
