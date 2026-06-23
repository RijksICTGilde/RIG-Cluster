# Crossplane via a Shadow Control Plane (Research)

**Status:** Research / not started. No code, no commitment. This documents an
investigation into replacing OPI's imperative connector + reconciliation layer
with Crossplane, running on a Kubernetes control plane we own inside our tenant
namespace — since we lack CRD/cluster-admin rights on the ODCN host cluster.

**Date:** 2026-06-23

---

## Problem & motivation

OPI today is a Python monolith that imperatively provisions infrastructure
(PostgreSQL, Keycloak, MinIO, Git repos, ArgoCD apps, K8s manifests) via
connectors, and hand-rolls its own reconciliation (orphan detection, scoped
manifest regeneration, drift handling). We want Crossplane's declarative
CRD + continuous-reconcile model instead.

**The blocker:** Crossplane needs its CRDs and controllers installed in a
cluster where we have CRD/cluster-admin rights. On ODCN we are a *tenant* — we
do not have those privileges on the host API server.

**The idea (validated):** run our *own* Kubernetes API server as a pod ("shadow
API server") inside our tenant namespace, register our CRDs there, run
Crossplane against it, and let Crossplane providers (also pods in our namespace)
do the real work against the backends. This is a known, productized pattern — we
do **not** build the API server from scratch.

---

## Key finding: this is not a rewrite of the monolith

**Crossplane replaces the *connector + reconciliation engine*, not the portal.**

Crossplane reconciles desired→actual continuously. It has no good notion of a
*human-approval gate* mid-flow. So these stay in OPI regardless:

- the FastAPI portal, wizard, project YAML schema, schema migrations, auth;
- **the domain/subdomain approval workflow** (the work in #137, restricted-path
  enforcement) — this is a workflow concern, not a reconcile concern.

Honest scope: this is "replace the imperative connector layer + hand-rolled
reconciliation with a declarative control plane the portal drives," **not**
"delete the monolith." Smaller, more defensible, and a better story.

---

## The shadow API server — options

| Option | What it is | Verdict |
|---|---|---|
| **vcluster** (Loft, CNCF) | Full virtual cluster (apiserver + controller-manager, optional scheduler) as pods in one host namespace. cluster-admin *inside* it; install any CRDs; no host privileges used. | **Best fit, lowest risk.** Run headless (no node/workload sync). Backing store: SQLite, embedded etcd, or **kine → our existing PostgreSQL** (consolidates state into infra we already run). |
| **kcp** | Purpose-built control plane = Kubernetes APIs without nodes/pods. Cleanest conceptual match to "just CRDs + controllers." | **Higher risk.** Crossplane-on-kcp historically rough (workspaces/APIBindings ≠ vanilla apiserver). `generic-controlplane` spin-off explicitly "not production-ready." |
| Bare `kube-apiserver` + kine + controller-manager | Roll our own from binaries. | **Don't.** This *is* what vcluster productizes (lifecycle/HA/backup/upgrades). Reinventing it violates KISS. |

The user's mental model is accurate, including: *the API server talks to backends
that are also pods in our cluster doing the real work* = a **Crossplane provider**.
Crossplane v2 decouples the provider runtime, so that separation is first-class.

**Recommendation:** vcluster, headless, backed by kine on our existing
PostgreSQL, with Crossplane installed inside.

---

## Connector → Crossplane mapping

| Connector | Crossplane path | Maturity |
|---|---|---|
| PostgreSQL (DB/role/grant) | `provider-sql` — manages DBs/roles/grants, *not* servers (we already have the server) | Good, direct fit |
| Keycloak (realms/clients) | `crossplane-contrib/provider-keycloak` — active, Crossplane v2 / namespaced-resource support landing | Good fit |
| MinIO (buckets/policies) | community `provider-minio`, or `provider-terraform` + MinIO TF provider | Workable |
| ArgoCD Applications | `provider-kubernetes` applies `Application` CRs directly, or keep committing to git | Fine |
| Git repos (Forgejo) | **No native provider.** `provider-terraform` + Gitea/Forgejo TF provider, a custom provider, or keep in OPI | **Gap — bespoke work** |
| Manifest generation (kubectl) | Composition functions render objects; `provider-kubernetes` applies them | Re-engineering |

---

## Risks & pushback (read before committing)

1. **Reconciliation logic isn't free.** The hard parts we recently fixed — SOPS
   skip-unchanged churn, scoped manifest regeneration, orphan detection,
   ArgoCD false-success deletes — get **re-expressed** as composition/provider
   logic, not skipped. Some get easier (drift correction, orphan detection are
   native strengths); some get harder.

2. **Secrets model friction.** Today: SOPS+AGE ciphertext in git = source of
   truth. Crossplane: connection details are plain k8s `Secret`s in the control
   plane's store (kine/etcd). State partially moves from git into the shadow
   apiserver. We *can* still GitOps the Claims/XRs via ArgoCD into the shadow
   plane, but the generated-manifests-in-git pattern changes shape. Design this
   deliberately — don't discover it mid-migration. (See also
   `features/futures/avoid-unnecessary-reprocessing-and-sops-churn.md`.)

3. **Forgejo has no provider.** Our git-repo provisioning is the one piece with
   no off-the-shelf answer. Don't start the PoC here, or you measure
   custom-provider effort instead of Crossplane's value.

4. **Operational surface.** We'd run+upgrade vcluster + Crossplane core + N
   providers, always-on, in a tenant namespace — with monitoring we've already
   flagged as a gap (`project_service_monitoring_gap`). Adding a control plane
   after an incident-heavy stretch is a real reliability cost.

5. **On Backstack / the BACK stack.** "BACK" = **B**ackstage + **A**rgoCD +
   **C**rossplane + **K**yverno — a *pattern*, not a product. We already have A,
   could add C (+ K for policy). The B (Backstage) is the part we don't want —
   and we don't need it: **keep our existing portal as the Backstage-equivalent.**
   Adopting "the stack" does not obligate its UI.

---

## Recommended path: a narrow spike, not a migration

De-risk the whole thesis with one vertical slice in the sandbox, then decide.

```
1. Stand up vcluster (headless, kine→Postgres) in a sandbox namespace
   → verify: working apiserver we have admin on; no host privileges used
2. Install Crossplane inside the vcluster
   → verify: CRDs register; core pods healthy
3. Migrate ONE real resource end-to-end: PostgreSQL DB provisioning
   via provider-sql, driven by a Claim ArgoCD syncs into the vcluster
   → verify: Claim provisions DB+role+grant + connection secret;
             delete cleans up; drift is auto-corrected
4. Wire OPI's portal to emit that Claim instead of calling the PG connector
   → verify: existing wizard flow still works, now backed by Crossplane
5. Assess the two hard ones deliberately: Forgejo (no provider) + SOPS secrets
   → verify: write down how each would work BEFORE expanding scope
```

**Spike success criterion:** one resource type fully runs through Crossplane in
the sandbox, AND we've written concrete answers for the Forgejo-provider gap and
the secrets model. If those answers are ugly, that's the signal to stop — cheaply.

Pick **PostgreSQL or Keycloak** as the first slice (both have real providers).
Avoid the git/Forgejo piece first.

---

## Sources

- vcluster — https://github.com/loft-sh/vcluster
- vcluster, cluster-wide CRDs — https://www.vcluster.com/blog/solution-clusterwide-crds
- vcluster backing store / kine — https://www.vcluster.com/docs/vcluster/configure/vcluster-yaml/control-plane/components/backing-store/
- Crossplane + vcluster + ArgoCD multi-tenancy — https://medium.com/@verajm/engineering-multi-tenancy-for-crossplane-052f05bd2152
- kcp.io — https://www.kcp.io/
- kcp generic-controlplane — https://github.com/kcp-dev/generic-controlplane
- Crossplane v2 what's new — https://docs.crossplane.io/latest/whats-new/
- provider-keycloak — https://github.com/crossplane-contrib/provider-keycloak
- provider-sql — https://github.com/crossplane-contrib/provider-sql
- BACK Stack — https://github.com/wnqueiroz/platform-engineering-backstack
