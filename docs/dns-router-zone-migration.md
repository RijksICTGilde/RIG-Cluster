# DNS migration: orphan CNAMEs to `router.<zone>`

Runbook for cleaning up the legacy CNAMEs that point at the OCP-router hostname
(`router-rig.rig.prd1.gn2.quattro.rijksapps.nl`) so that external-dns takes over
management with the new `router.<zone>` target. Background and root cause are in
`memory/project_dnssec_router_zone.md`.

## Why this is needed

Legacy CNAMEs were created without a TXT ownership marker, so external-dns refuses
to update or delete them — even with `--policy=sync`. The migration deletes them
once via the TransIP API; external-dns then recreates them with the correct
target plus the TXT marker so future updates flow normally.

## Prerequisites (already in main)

Confirmed before starting:

- [x] `cluster_config.py` has `external_dns_target` per supported domain
- [x] `manifests/ingress.yaml.jinja` emits the annotation
- [x] `project_manager.py` passes the target at all three ingress sites
- [x] `bootstrap/.../ingress-rijksapp.yaml` has the annotation hardcoded
- [x] `infrastructure/.../keycloak/.../kustomization.yaml` patches it onto Keycloak
- [x] `infrastructure/.../external-dns/controller/base/deployment.yaml` runs `--policy=sync`
- [x] `router.rijksapp.nl`, `router.rijks.app`, `router.rijksapp.dev` A/AAAA exist in TransIP

## The script

`operations-manager/python/scripts/transip_delete_dns.py` — uses the TransIP API
v6 to list and delete DNS records. Reads `TRANSIP_ACCOUNT_NAME` and
`TRANSIP_PRIVATE_KEY` from env. **Must be run from inside the production cluster**
(TransIP API has IP whitelist; local execution returns 401).

## Records to migrate (snapshot 2026-05-08)

48 orphan CNAMEs total, in three categories.

### Category A — Ready to delete (3)

Static ingresses with the new annotation already on the resource. Deleting these
triggers external-dns to recreate them with the correct target within ~1 minute.

```
keycloak.rijksapp.nl
wies.rijksapp.nl
zad.rijksapp.nl
```

### Category B — Needs project redeploy first (28)

Ingress exists in cluster but the rendered manifest in git was produced before
the template change, so the annotation is missing. Redeploy via OPI causes the
new template to render the annotation onto the Ingress — only then is it safe
to delete the orphan CNAME.

```
algoritmes.rijksapp.nl                    bouwmeester.rijks.app
amt.bzk.rijksapp.nl                       component-1.bouwmeester.rijks.app
assessments.rijksapp.nl                   component-2.bouwmeester.rijks.app
desa.rijksapp.nl                          docs.regelrecht.rijks.app
docs.rijksapp.nl                          editor.regelrecht.rijks.app
frontend-main-wies.rijksapp.nl            grafana.regelrecht.rijks.app
grist.rijksapp.nl                         harvester-admin.regelrecht.rijks.app
static-docs.rijksapp.nl                   hello.robbert.rijks.app
task-registry.rijksapp.nl                 landing.regelrecht.rijks.app
website.desa.rijksapp.nl                  lawmaking.regelrecht.rijks.app
                                          regelrecht.rijks.app
gebruikersonderzoek-2026-03-moza.rijksapp.dev  registers.rijks.app
moza.rijksapp.dev                         uitbetrouwbarebron.rijks.app
proef.gebruikersonderzoek-2026-03-moza.rijksapp.dev  upload.regelrecht.rijks.app
proef.moza.rijksapp.dev
```

amt.rijksapp.nl was in this category and is now done — proven workflow.

### Category C — Truly orphaned, no in-cluster Ingress (17)

No active project owns these. external-dns will not recreate them after deletion.
Pure cleanup. Verify each is genuinely abandoned before deleting (some look like
old PR builds, deleted projects, test fixtures).

```
frontend-productie-wies.rijksapp.nl       editor.pr129.rijks.app
frontend-production-wies.rijksapp.nl      editor.pr130.rijks.app
                                          editor.pr133.rijks.app
bado.rijks.app                            *.regelrecht-regel-k4c.rijks.app  (6 records)
component-1.bado.rijks.app                hello.robbert.rijks.app — keep? (Robbert's test domain)
belang.rijks.app
deletemij.rijks.app
component-1.deletemij.rijks.app
test.moza.rijksapp.dev
```

Note: `hello.robbert.rijks.app` is also listed under Category B above because it
*does* have an Ingress; verify before treating it as orphan.

## Procedure

### Step 1 — Stage script and credentials in the operations-manager pod

```bash
POD=$(kubectl get pod -n rig-prd-operations -l app=operations-manager -o jsonpath='{.items[0].metadata.name}')

kubectl cp \
  operations-manager/python/scripts/transip_delete_dns.py \
  "rig-prd-operations/$POD:/tmp/transip_delete_dns.py"

kubectl get secret -n rig-prd-operations transip-credentials \
  -o jsonpath='{.data.TRANSIP_ACCOUNT_NAME}' | base64 -d | \
  kubectl exec -i -n rig-prd-operations "$POD" -- sh -c 'cat > /tmp/td-account'

kubectl get secret -n rig-prd-operations transip-credentials \
  -o jsonpath='{.data.TRANSIP_PRIVATE_KEY}' | base64 -d | \
  kubectl exec -i -n rig-prd-operations "$POD" -- sh -c 'cat > /tmp/td-key'
```

A helper to run any script invocation with the credentials sourced:

```bash
run_in_pod() {
  kubectl exec -n rig-prd-operations "$POD" -- sh -c '
    export TRANSIP_ACCOUNT_NAME=$(cat /tmp/td-account)
    export TRANSIP_PRIVATE_KEY=$(cat /tmp/td-key)
    python3 /tmp/transip_delete_dns.py '"$*"
}
```

### Step 2 — Sanity-check current state per zone

```bash
for zone in rijksapp.nl rijks.app rijksapp.dev; do
  run_in_pod --zone "$zone" --type CNAME \
    --target-equals 'router-rig.rig.prd1.gn2.quattro.rijksapps.nl.' --dry-run
done
```

Compare the output against the lists above. The set may have changed since the
snapshot.

### Step 3 — Delete Category A (the 3 ready ones)

```bash
for name in keycloak wies zad; do
  run_in_pod --zone rijksapp.nl --name "$name" --type CNAME --yes
  sleep 5
done
```

Wait ~70s, then verify external-dns recreated them with the new target:

```bash
for h in keycloak.rijksapp.nl wies.rijksapp.nl zad.rijksapp.nl; do
  printf "%-30s " "$h"
  curl -sS "https://dns.google/resolve?name=$h&type=A" | \
    python3 -c "import json,sys; d=json.load(sys.stdin); print('Status', d['Status'], 'AD', d.get('AD'))"
done
```

Expected: `Status 0 AD True` for all three.

### Step 4 — Delete Category C (the truly orphaned ones)

For each host in Category C, delete its CNAME. external-dns will not recreate
since no Ingress claims the name. Pure cleanup.

```bash
# Per zone, delete all matching orphans:
run_in_pod --zone rijksapp.nl --type CNAME \
  --target-equals 'router-rig.rig.prd1.gn2.quattro.rijksapps.nl.' --dry-run
# Inspect the list — confirm none of the listed names belong to a redeploy-pending
# project that you haven't redeployed yet — then drop --dry-run.
```

Be careful: `--target-equals` filtering will match BOTH categories B (still has
Ingress, no annotation yet) AND category C (no Ingress at all). Don't run a blunt
bulk-delete on the whole zone until Category B is fully redeployed.

Safer: enumerate the Category C hosts explicitly, one zone at a time.

### Step 5 — Migrate Category B per project

For each project still on the legacy CNAME:

1. Trigger an OPI reprocess (re-render of manifests with the new template).
2. Verify the Ingress in cluster now has `external-dns.alpha.kubernetes.io/target`:
   ```bash
   kubectl get ingress -A -o json | python3 -c "
   import json, sys
   for i in json.load(sys.stdin)['items']:
     ann = i.get('metadata',{}).get('annotations',{}) or {}
     if ann.get('external-dns.alpha.kubernetes.io/target'):
       for r in i.get('spec',{}).get('rules',[]):
         print(r.get('host'))
   " | sort
   ```
3. Delete the orphan CNAME(s) for that project's hostnames using the script.
4. Wait ~70s and verify Google resolves to the new target.

### Step 6 — Cleanup

```bash
kubectl exec -n rig-prd-operations "$POD" -- \
  rm -f /tmp/td-account /tmp/td-key /tmp/transip_delete_dns.py
```

If `/tmp/test-edns-ingress.yaml` from the earlier dry-run still exists locally:

```bash
kubectl delete -f /tmp/test-edns-ingress.yaml || true
rm /tmp/test-edns-ingress.yaml
```

## Rollback

Worst case during the migration: a name resolves to NXDOMAIN for ~60 seconds
between delete and external-dns recreate. Existing TLS sessions keep working
(connection-keepalive); new connections retry transparently in most clients.

If a delete went wrong (e.g. the Ingress did not actually have the annotation),
the result is a record that comes back with the *old* broken target — which is
no worse than the starting state. To force a "good" state immediately, recreate
the CNAME manually in the TransIP control panel pointing to `router.<zone>`.

## Verification

A record is fully migrated when all of the following are true:

```bash
HOST=zad.rijksapp.nl
ZONE=rijksapp.nl
NAME=zad

# At authoritative TransIP NS
dig @ns0.transip.net "$HOST" CNAME +short              # -> router.rijksapp.nl.
dig @ns0.transip.net "edns-$NAME.$ZONE" TXT +short      # -> heritage=external-dns,...
dig @ns0.transip.net "edns-cname-$NAME.$ZONE" TXT +short  # -> heritage=external-dns,...

# Through Google's strict validator
curl -sS "https://dns.google/resolve?name=$HOST&type=A"
# expected: "Status": 0, "AD": true
```

When all three TXT/CNAME entries exist with `heritage=external-dns,owner=default`,
external-dns owns the record. Future Ingress changes will flow through automatically.
