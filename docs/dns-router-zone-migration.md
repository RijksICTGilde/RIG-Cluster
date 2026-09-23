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
- [x] `project_manager.py` passes the target into the helm values of the helmfile route
- [x] `bootstrap/.../ingress-rijksapp.yaml` has the annotation hardcoded
- [x] `infrastructure/.../keycloak/.../kustomization.yaml` patches it onto Keycloak
- [x] `infrastructure/.../external-dns/controller/base/deployment.yaml` runs `--policy=sync`
- [x] `router.rijksapp.nl`, `router.rijks.app`, `router.rijksapp.dev` A/AAAA exist in TransIP

## The script

`operations-manager/python/scripts/transip_delete_dns.py` — uses the TransIP API
v6 to list and delete DNS records. Reads `TRANSIP_ACCOUNT_NAME` and
`TRANSIP_PRIVATE_KEY` from env. **Must be run from inside the production cluster**
(TransIP API has IP whitelist; local execution returns 401).

## Which records need what

The snapshot that used to stand here (three fixed name lists, 2026-05-08) went stale
within months: seven names it filed under "needs a project redeploy first"
(`algoritmes`, `amt.bzk`, `assessments`, `desa`, `website.desa`, `task-registry`,
`frontend-main-wies`) already carried the annotation when the migration was picked up
again on 2026-09-23, and were simply deleted and came back correct. A list of names is
not the thing to check. The question is:

**Does the Ingress behind this name already carry
`external-dns.alpha.kubernetes.io/target`?**

Ask it per record, right before you delete:

```bash
# Every hostname in the cluster whose Ingress already carries the target annotation.
kubectl get ingress -A -o json | python3 -c "
import json, sys
for i in json.load(sys.stdin)['items']:
  ann = i.get('metadata',{}).get('annotations',{}) or {}
  if ann.get('external-dns.alpha.kubernetes.io/target'):
    for r in i.get('spec',{}).get('rules',[]):
      print(r.get('host'))
" | sort
```

Three answers, three routes:

| Answer | What to do |
|---|---|
| Annotation present | Delete the orphan CNAME. external-dns recreates it with the right target plus TXT marker within ~70s. |
| Ingress exists, annotation missing | Get the annotation onto the Ingress first (see below), verify it is really there, then delete. |
| No Ingress for this name at all | Truly orphaned. Delete as cleanup; external-dns will not recreate it. Verify it is genuinely abandoned first (old PR builds, deleted projects, test fixtures). |

### Getting the annotation onto an Ingress

For a project whose manifests OPI renders itself, an OPI reprocess is enough: the
current `manifests/ingress.yaml.jinja` emits the annotation, so the re-rendered Ingress
carries it.

For a project whose manifests are rendered by an external chart (a helmfile deployment:
`docs`, `static-docs`, `grist`), a reprocess was **not** enough for a long time, and
nothing in this runbook said so: the category lists it used to carry filed those names
under "needs a project redeploy first" like any other. OPI passed the target at its own
three ingress sites only, so the helmfile route never saw it and external-dns kept writing the
CNAME to the OCP-router hostname. RC-225 closed that gap: OPI now writes the target into
`cluster.ingress.annotations` of the helm values, which every ingress block of the
mijn-bureau charts reads. Since then a reprocess works for these projects too, but only
after that change is deployed to the cluster you are migrating.

Either way: an OPI reprocess and a green ArgoCD sync are not proof. Look at the Ingress
itself with the query above. `mb-docs-helmfile-production` in particular fails its
ArgoCD sync on an unrelated broken Kyverno policy, so "sync succeeded" carries no
information there at all.

## Two traps

Both were walked into on 2026-09-23.

**A name can carry two CNAMEs at once.** `algoritmes` had the broken record *and* a
hand-made good one pointing at `router.rijksapp.nl`. Deleting only the broken one leaves
a record that looks migrated but has no TXT ownership marker, so it still lives outside
external-dns and will not follow the next Ingress change. After every delete, check the
marker, not just the target. The Verification section below has the three lookups.

**The zone is the truth, not the cluster inventory.** The dry-run listed 12 records in
`rijksapp.nl` while the cluster only accounted for 10. The two extra,
`frontend-productie-wies.rijksapp.nl` and `frontend-production-wies.rijksapp.nl`, have
no Ingress and show up in no kubectl query whatsoever. Always start from the zone
listing (Step 2), and use the cluster only to classify what the zone hands you. These
two are awaiting a decision and stay put for now.

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

This listing is the work list: the zone knows about records the cluster does not
(see the second trap above). Classify every name it returns with the annotation query
from "Which records need what", and handle each one on its own route.

### Step 3 — Delete the names whose Ingress already carries the annotation

Per name from Step 2 that the annotation query lists:

```bash
ZONE=rijksapp.nl
NAMES="name-a name-b"          # the labels, without the zone

for name in $NAMES; do
  run_in_pod --zone "$ZONE" --name "$name" --type CNAME --yes
  sleep 5
done
```

`--name` deletes every CNAME on that name, which is what you want: a name can carry
two (first trap above). Wait ~70s, then verify external-dns recreated them with the
new target:

```bash
for name in $NAMES; do
  printf "%-30s " "$name.$ZONE"
  curl -sS "https://dns.google/resolve?name=$name.$ZONE&type=A" | \
    python3 -c "import json,sys; d=json.load(sys.stdin); print('Status', d['Status'], 'AD', d.get('AD'))"
done
```

Expected: `Status 0 AD True` for all of them. Then check the TXT markers per name
(Verification below): `Status 0` also holds for a hand-made record that external-dns
does not own.

### Step 4 — Delete the names with no Ingress at all

These external-dns will not recreate, since no Ingress claims the name. Pure cleanup.

```bash
# Per zone, list what is left:
run_in_pod --zone rijksapp.nl --type CNAME \
  --target-equals 'router-rig.rig.prd1.gn2.quattro.rijksapps.nl.' --dry-run
# Inspect the list, confirm none of the names still needs its annotation first,
# then drop --dry-run.
```

Be careful: `--target-equals` matches the names that still have an Ingress without the
annotation just as happily as the abandoned ones. Don't run a blunt bulk-delete on a
whole zone while any name in it is still waiting for Step 5.

Safer: enumerate the abandoned hosts explicitly, one zone at a time.

### Step 5 — Get the annotation onto the remaining Ingresses, then delete

For each name that still has an Ingress without the annotation:

1. Get the annotation onto the Ingress. For an OPI-rendered project that is an OPI
   reprocess; for a helmfile project it is an OPI reprocess on a cluster that runs
   RC-225 or later. See "Getting the annotation onto an Ingress" above.
2. Verify the Ingress in cluster now really has the annotation, with the query from
   "Which records need what". This is the gate: a green ArgoCD sync is not one, and for
   `mb-docs-helmfile-production` it is not even available.
3. Delete the orphan CNAME(s) for that project's hostnames using the script.
4. Wait ~70s, verify Google resolves to the new target, and check the TXT markers.

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

The CNAME lookup must return exactly one line. Two lines means a hand-made record is
still sitting next to the one external-dns wrote, and the TXT markers say nothing about
which of the two answers a resolver gets.
