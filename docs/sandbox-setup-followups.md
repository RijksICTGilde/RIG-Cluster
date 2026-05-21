# Sandbox setup follow-ups (2026-05-20)

Issues we hit tijdens de sandbox-setup van vandaag en oppervlakkig gefixt
hebben. Hier op één plek zodat we er later naar kunnen kijken.

## 1. Setup faalde door ontbrekende bestanden

**Wat:** TODO — invullen welke bestanden precies misten. (Vergeten te noteren
tijdens troubleshooting; user weet wat dit was.)

**Waar te kijken:** `task sandbox:setup` output van vandaag, of de scripts in
`Taskfile.yaml` rond de `sandbox:*` tasks.

## 2. Wildcard cert was 10 dagen verlopen

**Wat:** `*.sandbox.rijksapp.dev` cert verliep 2026-05-10, niemand zag het
totdat de sandbox vandaag (2026-05-20) gebruikt werd.

**Tijdelijke fix:** Cert opnieuw geïssued via nieuw scripted flow
(`task sandbox:renew-wildcard-cert`, runbook in
`docs/sandbox-wildcard-cert-renewal.md`). Nieuw cert geldig tot 2026-08-18.

**Wat structureel nodig:** Monitoring/alerting op cert NotAfter. Voorstel:
GitHub Action die faalt als de committed `.age` cert <14 dagen heeft. Geen
cluster-toegang nodig, faalt in CI met duidelijke message.

## 3. ZAD database `operations_manager` bestond niet

**Wat:** Op een fresh cluster crashed OPI op `alembic upgrade head` met
`database "operations_manager" does not exist`. De in-app
`_ensure_database_exists()` (in `opi/core/database_pools.py:77`) komt nooit
aan bod omdat de docker entrypoint Alembic *vóór* `python -m opi.server`
draait.

**Tijdelijke fix:** Database als CNPG `Database` CRD pre-aangemaakt (commit
`a589f5df`, file
`infrastructure/bootstrap/infrastructure/postgresql/database/base/databases.yaml`).
Naast de bestaande `forgejo` en `keycloak` databases.

**Wat structureel nodig:** Of de entrypoint fixen (ensure-DB vóór alembic),
of de nu-dode `_ensure_database_exists` code verwijderen omdat de CNPG CRD
de enige bootstrap-pad is geworden. Eén van de twee — niet beide bewaren.

## 4. Prometheus scrape down (stale SA token)

**Wat:** `kubernetes-cadvisor` en `kubernetes-kubelet` targets waren `down`
met 401 Unauthorized. Kubelet log:
`service account UID (4f27913d-...) does not match claim (7f1e5664-...)`.
De `namespace-manager` SA was opnieuw aangemaakt door ArgoCD nadat de
Prometheus pod al draaide; de pod hield de oude (stale) projected token vast.
Effect: geen CPU/memory metrics voor user projects in de ZAD details view.

**Tijdelijke fix:** `kubectl rollout restart deploy/prometheus -n rig-system`.
Targets daarna `up`, metrics binnen.

**Wat structureel nodig:** Onderzoeken of dit reproduceerbaar gebeurt bij
elke fresh setup (waarschijnlijk wel, gezien de order waarin ArgoCD dingen
aanmaakt). Mogelijke oplossingen:
- ArgoCD sync wave: SA in eerdere wave dan Prometheus deployment.
- Init container die wacht tot de SA stabiel is.
- Liveness check die pod restart bij stale-token detectie.

## 5. Doc-gap audit

**Wat:** De "truuk" met `*.sandbox.rijksapp.dev` → 127.0.0.1 + Let's Encrypt
wildcard via TransIP DNS-01 was nergens gedocumenteerd, terwijl het
load-bearing is. Nu wel (`docs/sandbox-wildcard-cert-renewal.md`).

**Wat structureel nodig:** Audit van andere operationele kennis die nergens
op schrift staat. Kandidaten:
- Wie beheert de `rijksapp.dev` zone in TransIP? (Eigenaarschap, escalatie.)
- Wie heeft toegang tot het TransIP-account (en de IP-whitelist daar)?
- Waar leeft de developer AGE key (`security/developer-key.txt`) als single
  source of truth voor nieuwe team-leden?
- Welke andere certs hebben een soortgelijke handmatige renewal-cyclus?
