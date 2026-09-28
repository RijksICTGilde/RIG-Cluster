# Wachtwoorden roteren: de complete flow-analyse

**Datum:** 28 september 2026
**Status:** gemeten aan beide clusters en beide repos; dit document lost de openstaande vragen uit `wachtwoorden-roteren-per-component.md` op en bepaalt per secret of rotatie volledig via de tool kan lopen
**Volgende stap:** `secret_rotate.py` hierop bouwen (categorieën-met-stappen, zie § 4)

## 1. Twee sync-werelden, exact afgebakend

Gemeten via owner-label (`app.kubernetes.io/instance`) én tracking-annotatie (`argocd.argoproj.io/tracking-id`) — op ODCN staat het eigenaarschap in de **annotatie**, wie alleen het label leest concludeert ten onrechte "geen eigenaar".

**Wereld 1: ArgoCD-beheerd, automated + selfHeal.** Elf roterende infra-secrets, op ODCN onder app `production-infrastructure` (bron: `github.com/RijksICTGilde/RIG-Cluster.git@main`, pad `infrastructure/bootstrap/clusters/odcn`), op sandbox onder `sandbox-infrastructure` (bron: Forgejo `rig-admin/zad-argo-infrastructure@main`, pad `bootstrap/clusters/sandboxed-local`):

`postgres-admin-credentials`, `keycloak-db-credentials`, `keycloak-admin-credentials`, `keycloak-mail-credentials`, `mail-db-credentials`, `mail-relay-credentials`, `minio-admin-credentials`, `pgadmin-credentials`, `redis-admin-credentials`, `transip-credentials`, `vault-init-credentials`.

Twee harde consequenties. Een `kubectl edit secret` op deze secrets is zinloos (selfHeal zet hem binnen de minuut terug) — rotatie loopt hier **altijd via git**. En omgekeerd: na commit+push komt de wijziging vanzelf aan; de tool hoeft alleen te refreshen en te *wachten tot hij er is*.

**Wereld 2: bootstrap — eenmalig met kubectl ge-applied, nooit gesynct.** Op ODCN: `sops-age-key`, `argocd-secret`, `argocd-admin-credentials`, de dex-tokens, `operations-manager-env-secrets`, `operations-manager-keycloak`, de pull-robot-secrets, `letsencrypt-*-key`, `keycloak-rijksapp-tls`, `keycloak-client-mb-grist-helmfile-production`, `ceph-csi-kms-token`, `my-secret`. Daarnaast op een eigen plek: `minio-credentials` in `rig-prd-backup` (de backup-MinIO — de overlay `backup-destination` wordt door de Taskfile direct ge-applied, regel 739).

Deze tweede wereld heeft een **eigen templates-boom** onder `bootstrap/rig-system/kustomize/secrets/templates/` (argocd-admin, de twee argocd-repo-secrets) met overlays per cluster. De toepassingsweg is direct: `SOPS_AGE_KEY=... kustomize build <overlay> | kubectl apply` (Taskfile regels 674, 1016, 1050, 3414). Voor rotatie betekent dat: bestand bijwerken + datzelfde build-apply-pad draaien — geen Argo-sync om op te wachten.

**Verheldering die meteen doorhad:** de MinIO-"drift" uit het vorige document bestaat niet. De CMP injecteert `ARGOCD_APP_NAMESPACE` in elke kustomization tijdens de render (`configmap-sops-plugin.yaml:272-276`), dus op ODCN landen secret (git zegt `rig-system`) en deployment (overlay zegt `rig-prd-operations`) samen in `rig-prd-operations`. Gemeten: `minio-admin-credentials` staat daar, naast de minio-deployment.

## 2. Kan elk secret volledig via API/calls? — de dekkingstabel

"Via API" betekent hier: het script roept `kubectl`, `argocd`-annotaties en de eigen API's van de componenten aan; geen handmatige tussenstap in een console of UI.

| secret / component | app-zijde aanpassen | sync activeren | verifiëren | volledig automatiseerbaar |
|---|---|---|---|---|
| `postgres-admin-credentials` (CNPG superuser) | operator reconciliet uit secret (`enableSuperuserAccess`) | Argo-refresh-annotatie + pollen | `psql SELECT 1` in `rig-db-1` met nieuwe waarde | **ja** |
| `keycloak-db-credentials` (CNPG managed role) | idem (`managed.roles[].passwordSecret`) | idem | idem, daarna Keycloak-health | **ja** |
| `mail-db-credentials` (managed role) | idem | idem | psql + relay health | **ja** |
| `forgejo-db-credentials` (managed role) — nog niet in de tabel | idem | idem | idem + forgejo health | **ja** |
| `redis-admin-credentials` | `redis-cli ACL SETUSER` in de pod | idem (bootschrijfpad moet mee) | `PING` met nieuwe waarde | **ja** |
| `keycloak-admin-credentials` | `kcadm set-password` (Admin API, in-pod) | idem | frisse kcadm-login | **ja** |
| `minio-admin-credentials` | **geen API voor root** — env bij start; herstart is de aanpassing | idem | `mc alias` vanuit OPI-pod | **ja** |
| `minio-credentials` (backup, `rig-prd-backup`) | idem, géén root-API | **bootstrap-apply** (geen Argo-app) | idem | **ja** (via apply-pad) |
| `pgadmin-credentials` | env bij start | Argo-refresh (productie: uitgecommentarieerd → alleen bestand) | login/handmatig | **ja** |
| `prometheus-metrics-auth-secret` | server leest bij start | Argo-refresh | query met nieuw token | **ja** |
| `mail-relay-secret` (Stalwart admin) | management-API `POST /api/principal` | Argo-refresh | `GET /api/principal` met nieuwe waarde | **ja** |
| `keycloak-mail-credentials` (smtp-account) | relay-API als relay-admin | Argo-refresh + keycloak-restart | SMTP-login | **ja** |
| `argocd-secret` (ArgoCD admin) | bcrypt genereren + `kubectl patch` van `admin.password`+`admin.passwordMtime`; geen herstart nodig | n.v.t. (bootstrapped) | `POST /api/v1/session` geeft JWT | **ja** |
| `argocd-repo-*` (Forgejo-repo-credentials) | Forgejo-wachtwoord via `forgejo admin user change-password` in de forgejo-pod; dan het repo-secret bijwerken | bootstrap-apply (geen herstart: repo-server leest repo-credentials per request) | refresh van een app lukt | **ja** |
| `operations-manager-keycloak` (OIDC-clientsecret) | Keycloak Admin API: nieuw clientSecret | bootstrap-apply | OPI-readiness "OAuth Client" + loginflow | **ja** |
| `transip-credentials` | **nee** — TransIP heeft geen API voor zijn eigen API-sleutel | Argo-refresh | `TransIPConnector.list_domains()` | **deels** (console stap blijft) |
| Grafana-token (leeft alleen in het env-bestand) | **nee** — token komt van ODCN | n.v.t. (env-secret regenereren) | `GET /api/org` met Bearer | **deels** (aanvraag bij ODCN blijft) |
| `vault-init-credentials` | unseal-keys roteren is een eigen procedure, geen wachtwoordwissel | — | — | **buiten scope** (zoals plan) |
| `sops-age-key` | eigen script (`rotate-sops-key.py`), CMP pikt hem via hard-refresh van de apps | bootstrap | de final-clean-check van dat script | **via eigen script** |

Het onvermijdelijke handwerk dat overblijft, en waaróm het handwerk is: (1) **git commit+push** van het gewijzigde SOPS-bestand — een bewuste reviewpoort op een security-diff, geen technische beperking; (2) de **TransIP-console**; (3) de **Grafana-aanvraag bij ODCN**. Alles daarbuiten, inclusief de Argo-refresh, loopt door de tool.

## 3. Waarom de volgorde héél precies moet zijn (de gates)

De sync-volgorde klopt alleen als elke poort *gemeten* is in plaats van aangenomen. Vier harde regels:

1. **Vertraag nooit op "Argo heeft vast wel gesynct" maar op "het cluster-secret draagt de nieuwe waarde".** De tool poll `kubectl get secret …` tot gelijkheid (met timeout), zélf na een refresh-hard — anders herstart je een consumer op de oude omgeving.
2. **Bij CNPG wacht je op de operator, niet op de pod.** Poort voltooid pas als een login met de nieuwe waarde werkt én de oude faalt — pas dan mag de consumer herstarten (Keycloak komt anders om met een wachtwoord dat de DB nog niet kent).
3. **Bij app-eerst-categorieën geldt het omgekeerde**: de app wijzigt vóór het bestand, omdat er géén declaratief pad is; de selfHeal-sync convergeert het cluster-secret daarna naar dezelfde waarde.
4. **Herstarten is een bevraagde actie, geen vaste stap** — behalve waar de consumer het wachtwoord alleen bij start leest; dan ís de herstart de rotatie en valt hij niet weg te vragen.

De volgorde binnen een bulkronde: eerst waar niemand van afhangt (postgres-superuser), dan waar componenten van afhangen (keycloak-db → keycloak-herstart), dan de rest, en **keycloak-admin als allerlaatste** — dat is het enige wachtwoord waarmee je jezelf buitensluit uit het systeem waarmee je een mislukte rotatie herstelt.

## 4. Het categorie-model dat hiervoor in het script komt

Elke categorie bezit haar stappen één keer; een component declareert categorie + parameters. Uitbreiden = één rij in de tabel, geen nieuwe logica.

- **`cnpg-secret`** → pre-flight → bestand → push-poort → refresh+pollen → operator-reconcile-gate → consumers herstarten → e2e-verify. Rijen: postgresql, keycloak-db, mail-db, forgejo-db.
- **`live-app`** → pre-flight → app-call met huidige waarde → verify met nieuwe → bestand → sync-convergentie-check. Rijen: redis, keycloak-admin, mail-relay, keycloak-mail.
- **`env-restart`** → drift-check (cluster↔bestand) → bestand → refresh+pollen → workload-restart → verify. Rijen: minio, minio-backup, pgadmin, prometheus.
- **`external`** → check huidige via API → getoonde handeling buiten het cluster (de enige echte pauze) → verify nieuwe via API → bestand/apply. Rijen: transip, grafana.
- **`bootstrap`** → pre-flight → bestand → directe build-apply → verify. Rijen: argocd-repo-secrets, operations-manager-keycloak, backup-minio (schrijfkant), argocd-secret.

Detectie van de wereld is live (`tracking-id` of instance-label aanwezig?), dus een secret mácht verhuizen van bootstrap naar Argo zonder dat de tool breekt.

## 5. Rekening houdend met de corrigerende bevindingen

- De MinIO-"drift" uit het vorige plan **bestaat niet** (CMP-injectie) — die stap valt weg, de namespace-vraag ook: component en consumer delen per cluster altijd een namespace.
- De `pgadmin` template kent de typefout `PGLADMIN_DEFAULT_PASSWORD`; de tool volgt de bestaande naam en "verbetert" hem niet.
- `@secret-gen` op `keycloak-db-credentials` ("random:20") is nooit uitgevoerd — de rotatie van keycloak-db herstelt die generatie direct mee.
- Open punt, voor later: `argocd-admin-credentials` (custom, uit de bootstrap-templates) naast `argocd-secret` (waar ArgoCD zelf leest) — voor rotatie van de ArgoCD-admin geldt `argocd-secret` als operationeel; of de eerste nog een consumer heeft moet de repetitie uitwijzen.

## 6. Restant handwerk — bewust, en dus expliciet

Git commit/push (reviewpoort), TransIP-console, Grafana-token bij ODCN. Al het andere — refresh, wachten op propagatie, operator-reconcile, restarts, verificaties, drift-checks — is scriptpad.

## 7. Addendum: twee driften die de eerste rotatie zelf moet repareren

Gemeten toen het gereedschap klaar was (28-09). Verklaren waarom "de tool roteert het bestand" niet klakkeloos overal kan:

1. **`keycloak-db-credentials` is op ODCN een plaintext bestand** (`overlays/odcn/keycloak-db-credentials.yaml`, rechtstreeks in de `resources` van de kustomization, niet via `decrypt-sops.yaml`). De eerste rotatie van dit component moet daarom drie dingen tegelijk doen: versleuteld bestand schrijven onder de canonieke naam (`...-secret.yaml.sops.yaml`), registreren in `decrypt-sops.yaml` en de kustomization omleggen, en het plaintext bestand verwijderen — alles in dezelfde commit als de waarde-rotatie, anders staat git even met twee bronnen.
2. ~~De sandbox-secrets-overlay bestaat alleen in de Forgejo-mirror~~ — **gecorrigeerd na verdere meting, zelfde dag**: de mirror (`zad-argo-infrastructure`) bevatte de canonieke SOPS-overlay al en de waarden daarin zijn identiek aan het live cluster. De afwezigheid in deze repo is bewust: de map staat in `.gitignore`. De `.gitignore`-regel dekt echter niet alles af: wie de overlay lokaal niet heeft, seed hem met `--seed-overlay` (die schrijft hem vanuit live); daar is die ingang voor gemaakt.

Beide punten zijn géén uitzonderingen op het categorie-model maar erop: de gates blijven gelijk; alleen de schrijfkant kent bij deze twee een extra eerste keer.
