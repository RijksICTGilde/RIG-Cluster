# argocd-rig — eigen Argo CD image met namespace-sync fixes

Upstream Argo CD **v3.3.12** plus twee patches die voorkomen dat de cluster-cache stukloopt
(en namespaces uit de cache verdwijnen) als één namespace weggaat of RBAC-restricted is.

Productie draait nu de Red Hat build van v3.3.12
(`registry.redhat.io/openshift-gitops-1/argocd-rhel9@sha256:5058a825...`). Die is gewoon
upstream v3.3.12 zonder downstream patches, dus deze patches landen exact op de draaiende code.

Relevant omdat `argocd-default-cluster-config` 47 namespaces in het `namespaces`-veld heeft:
we draaien in **namespace-mode**, precies het pad dat de bug raakt.

## Inhoud van de patches

| Patch | Herkomst |
|---|---|
| `0001-cherry-pick-PR-27528.patch` | Upstream PR [argoproj/argo-cd#27528](https://github.com/argoproj/argo-cd/pull/27528) — cluster sync blijft leven als een namespace verdwijnt of RBAC-restricted is. Eén triviaal conflict opgelost bij de cherry-pick (de syncLock-refactor is master-only). |
| `0002-fix-drop-stranded-GVK-on-error-and-cap-sync-warnings.patch` | Twee fixes uit onze review op die PR, met regressietests (groen onder `-race`): (1) GVK wordt uit `apisMeta` verwijderd bij een error in `startMissingWatches`, anders blijft die tot de volgende full resync ongewatcht; (2) cap op sync-warnings (max 50 + "... and N more"), anders groeit `ConnectionState.Message` bij 47 namespaces naar honderden KB's. |

Niet meegenomen: PR [#25229](https://github.com/argoproj/argo-cd/pull/25229) (incremental namespace
sync, achter feature flag `ARGOCD_ENABLE_INCREMENTAL_NAMESPACE_SYNC`). Die botst qua ontwerp met
27528 — beide introduceren dezelfde helper onder een andere naam met tegengestelde `respectRBAC`-
semantiek. Wachten tot de auteur 25229 op 27528 rebaset. De bugfix kan wel al, de performance-feature nog niet.

De patches zijn de bron van waarheid: `task src:verify` bewijst dat de build-worktree exact
`v3.3.12` + deze patches is.

## Gebruik

De image wordt gebouwd uit een **git worktree** van de argo-cd checkout, standaard
`../../../argo-cd-rig` naast deze repo, op branch `rig/ns-sync`. Bestaat die branch niet
(bijv. op een andere machine), dan bouwt `task src:prepare` hem op uit tag `v3.3.12` + `patches/`.

```bash
cd images/argocd-rig

task src:prepare     # build-worktree klaarzetten
task src:verify      # bewijzen dat worktree == v3.3.12 + patches/
task build           # image bouwen voor de host-arch → argocd-rig:v3.3.12-rig1
task test            # smoke test, incl. willekeurige-UID check
task load-kind       # image in kind cluster rig-sandbox laden
task cr-snippet      # YAML voor de ArgoCD CR
```

Naar de registry (productie-architectuur, vraagt om bevestiging):

```bash
task publish         # linux/amd64 → ghcr.io/minbzk/argocd:v3.3.12-rig1
```

Branch naar de fork pushen:

```bash
task push-branch REMOTE=rig
```

Handige overrides: `ARGOCD_REPO`, `ARGOCD_SRC`, `BRANCH`, `BASE_TAG`, `IMAGE_TAG`,
`PLATFORM`, `PUBLISH_PLATFORM`, `KIND_CLUSTER_NAME`, `REGISTRY_IMAGE`.

## Uitrollen

`task cr-snippet` toont de exacte CR-wijziging. Kort:

- **sandboxed-local** (kind): `spec.image: argocd-rig`, `spec.version: v3.3.12-rig1` — image staat
  na `task load-kind` lokaal op de node, tag is niet `latest` dus de pull policy is `IfNotPresent`.
  Al uitgerold: controller, server en applicationset draaien `argocd-rig:v3.3.12-rig1`,
  repo-server bleef op `quay.io/argoproj/argocd:v3.3.10`.
- **odcn-production**: `spec.image: ghcr.io/minbzk/argocd`, `spec.version: v3.3.12-rig1`, en
  repo-server teruggepind op de Red Hat build.

Controller, server en applicationset volgen `spec.image`; de repo-server pinnen we terug op de
vendor-image. De cmp-sidecar heeft een eigen image en blijft ongemoeid.

> [!WARNING]
> De `sops-plugin` component doet `op: replace` op `/spec/repo`. Alles wat je onder `spec.repo`
> in `argocd-deployment.yaml` zet, wordt daardoor **stil weggegooid**. De repo-pin hoort als
> patch in de `kustomization.yaml` van de overlay, ná de component — zoals de
> odcn-production overlay dat al doet voor `spec.repo.env` (met dezelfde waarschuwing erbij).
> Verifieer altijd met `kustomize build <overlay> | grep -A2 '^  repo:'` vóór het uitrollen.
>
> Bij deze uitrol bleek de sandbox daar al langer last van te hebben: de repo-server env vars
> (waaronder `ARGOCD_REPO_SERVER_PLUGIN_USE_MANIFEST_GENERATE_PATHS`, issue #130) en de
> `ARGOCD_EXEC_TIMEOUT` van de cmp-sidecar waren nooit actief. Die staan nu als patch in
> `overlays/sandboxed-local/kustomization.yaml`. Nog steeds dood in de sandbox-CR, niet
> aangeraakt omdat het runtime-gedrag verandert: `spec.repo.annotations` (kyverno-excludes),
> `spec.repo.resources` (repo-server draait zonder limits) en de securityContext +
> `kube-api-access` volumeMount van de cmp-sidecar.

## Risico's (eerst in `sandboxed-local` testen)

1. **Willekeurige UID.** De upstream image draait `USER 999`; OpenShift kent een eigen UID uit de
   namespace-range toe (voor de cmp-sidecar moesten we al `runAsUser: 1001620000` zetten). De
   upstream image is daar minder op gehard dan de Red Hat build. `task test` draait de image daarom
   onder UID `1001620000` met gid 0. Gemeten op de arm64 build van `v3.3.12-rig1`:
   `argocd`, `git`, `helm` en `kustomize` werken, en `/home/argocd` is schrijfbaar (de Dockerfile doet
   `chown argocd:0` + `chmod g=u`). Enige afwijking: er is **geen `/etc/passwd`-entry** voor de
   toegekende UID, dus `getpwuid()` faalt — `id` klaagt. Dat blijft cosmetisch zolang `HOME` en
   `USER` uit de env komen (dat doet de Dockerfile), maar het is wél het verschil met de Red Hat
   build. Let op: kind ≠ OpenShift SCC, dus dit vervangt de sandbox-test niet.
2. **Honoreert de operator `spec.image`?** Sommige operator-versies negeren dat veld ten gunste van
   eigen `RELATED_IMAGE_*` env vars. Na uitrol verifiëren met de kubectl-regel uit `task cr-snippet`.
3. **Support.** Controller en server draaien met een eigen image buiten Red Hat support.

Optioneel later, los van de image-uitrol terug te draaien: feature flag via `spec.controller.env`
→ `ARGOCD_ENABLE_INCREMENTAL_NAMESPACE_SYNC=true`, pas nadat 25229 gemerged/gerebaset is.
