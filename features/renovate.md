# Renovate: wekelijkse afhankelijkheidsupdates

Renovate draait als GitHub Action in `RijksICTGilde/RIG-Cluster` en maakt elke
maandagochtend PR's voor nieuwe versies van alles wat deze repo vastpint: Python, Go,
Maven, npm, Dockerfiles, Kubernetes-manifesten, remote kustomize-resources, de Taskfile
en `.tool-versions`.

## Wat er draait

| Onderdeel | Waar |
|---|---|
| Configuratie | `renovate.json` op de root |
| Workflow | `.github/workflows/renovate.yml` |
| Schema | maandag 6:00 UTC, plus `workflow_dispatch` |
| Token | repository secret `RENOVATE_TOKEN` |
| Dashboard | GitHub-issue "Dependency Dashboard" in `RijksICTGilde/RIG-Cluster` |

Het token is bewust niet `GITHUB_TOKEN`: PR's die daarmee worden gemaakt starten geen
andere workflows, waardoor `ci.yml` niet op de Renovate-PR's zou draaien.

## Dekking

| Bron | Waar | Manager |
|---|---|---|
| Python | `operations-manager/python/pyproject.toml` + `uv.lock` | `pep621` |
| Go | `images/e2e-allservices/go.mod`, `images/zad-waker/go.mod` | `gomod` |
| Maven | `keycloak-migration/*/pom.xml` | `maven` |
| npm | `operations-manager/python/static/js/package.json`, `presentation/reveal/package.json` | `npm` |
| Dockerfiles | 15 stuks in `images/`, `operations-manager/` | `dockerfile` |
| docker compose | `docker-compose.dev.yaml` | `docker-compose` |
| Kubernetes-manifesten | `image:` in `infrastructure/`, `bootstrap/` | `kubernetes` |
| Kustomize-overlays | `newName` + `newTag` in `infrastructure/`, `bootstrap/` | `kustomize` |
| GitHub Actions | `.github/workflows/*.yml` | `github-actions` |
| Tools | `.tool-versions` | `asdf` + custom manager voor `task` |
| Remote kustomize-resources | versies in de raw.githubusercontent-URL's | custom manager |
| Taskfile | de CNPG- en snapshot-CRD-URL's, de argocd-operator-versie | custom manager |
| kind-node | `sandboxed-local/kind-config.yaml` | custom manager |

Een nieuwe meting doe je met een dry-run, zie onderaan.

De actions staan op een commit-SHA met de versie als commentaar erachter
(`uses: actions/checkout@11d5960... # v4`). Renovate leest dat commentaar als de huidige
versie en schrijft bij een update zowel de SHA als het commentaar mee.

## Hoe een update binnenkomt

Patch en minor komen gegroepeerd per ecosysteem: een eigen PR voor python, go, maven,
npm, dockerfile, kubernetes, github actions, tool-versions en remote manifests. Dat is
met opzet geen enkele grote PR, want dan blokkeert een rode Python-update een
Dockerfile-update.

Een major wordt geen PR. Die staat op het Dependency Dashboard onder "Pending
Approval" met een checkbox. Zet je dat vinkje, dan maakt Renovate bij de volgende run de
PR. Dat is de goedkeuringsstap: een major vraagt om een migratie, en die hoort een mens
te plannen.

Er is geen automerge. Mergen is altijd mensenwerk.

## De rcr-paden

De odcn-overlays verwijzen naar `rcr.rijksapps.nl/<proxy>/...` in plaats van naar de
upstream registry. Dat moet zo blijven: de admission rewrite op ODCN vraagt dat pad,
anders blijft ArgoCD eeuwig OutOfSync.

Renovate zoekt de versie daarom op bij de echte bron en laat het rcr-pad in de PR staan.
De mapping staat in `renovate.json` als `overridePackageName`:

| rcr-proxy | echte bron |
|---|---|
| `rcr.rijksapps.nl/ghcr-rig/X` | `ghcr.io/X` |
| `rcr.rijksapps.nl/dockerhub-rig/X` | `docker.io/X` |
| `rcr.rijksapps.nl/k8s-rig/X` | `registry.k8s.io/X` |
| `rcr.rijksapps.nl/quay-rig/X` | `quay.io/X` |

Komt er een nieuwe proxy bij, voeg dan een `packageRules`-regel toe volgens hetzelfde
patroon. Zonder die regel zoekt Renovate de tags op bij `rcr.rijksapps.nl` zelf, en dat
levert niets op.

## Wat Renovate met opzet niet bijwerkt

- **Eigen images met een CalVer-tag** (`operations-manager`, `keycloak-zad`). De pin in
  de odcn-overlay is een deploybeslissing in git, die neemt een mens.
- **Eigen images zonder upstream-versiereeks** (`argocd-rig`, `zad-waker`,
  `rig-cmp-argo-kustomize-sops`).
- **Lokale sandboxverwijzingen** (`localhost:5001/...`, `operations-manager:latest`): er
  is geen registry om een versie bij op te zoeken.
- **`archive/`**, de testmappen en `self-service-portal/` (een schets, geen app).

## Wat nog niet gedekt is

Renovate kan hier niets zinnigs voorstellen; elk van deze punten is een eigen vervolgtaak:

- Images op een zwevende tag: `jpillora/chisel:latest`, `nginxinc/nginx-unprivileged:alpine`,
  `gcr.io/distroless/static-debian12:nonroot`, `registry:2`,
  `codeberg.org/forgejo/forgejo:14`, `redis:7-alpine`. Pin ze op een versie of een digest.
- Tools die een Dockerfile met `ARG ..._VERSION` en een curl binnenhaalt: `SOPS_VERSION`,
  `KOPIA_VERSION`, `CHISEL_VERSION`, `KUSTOMIZE_VERSION`, `KSOPS_VERSION`, en de
  jupyterhub-extensies. Die vragen per regel een `# renovate:`-annotatie.
- `mc` in `.tool-versions`: volgt `MC_VERSION` uit `images/mc/Dockerfile`, en dat is een
  MinIO RELEASE-datum.
- `aquasecurity/trivy-action` in `security.yml` is op een kale SHA gepind, zonder
  `# vX.Y.Z` erachter. Renovate meldt `unversioned-reference` en laat hem staan. Zet de
  versie in een commentaar achter de SHA en hij gaat wel mee.
- Het segment `release-X.Y` in de CNPG-URL in `Taskfile.yaml` schrijft Renovate niet mee.
  Controleer dat met de hand bij een minor.
- **external-dns staat met een `allowedVersions` op `<v0.22.0`.** Versie 0.22 haalde de
  ingebouwde transip-provider eruit, en `--provider=transip` geeft op v0.23.0 een fatal bij
  het parsen van de vlaggen. Verder dan 0.21 vraagt dus een migratie naar de
  webhook-provider: een eigen taak. external-dns staat alleen in de odcn-overlay, dus de
  sandbox vangt dit niet.
- **byte-buddy staat op de mainline-versielijn** (`allowedVersions` op drie cijfers). Naast
  1.17.7 bestaat een 1.18.15-jdk5, de JDK5-compatibele artefactlijn, en die leest als
  nieuwer.
- `KIND_VERSION` in `Taskfile.yaml` is dode config: niets gebruikt hem.

## Van GitHub naar de ontwikkeling

Ontwikkeling gebeurt op Forgejo main; GitHub main krijgt de releases. Een Renovate-PR
wordt op GitHub gemerged en komt met de eerstvolgende terugmerge van GitHub main naar
Forgejo in de ontwikkeling terecht. Renovate opent dus geen PR's op Forgejo.

## Handstap bij het inrichten

Een beheerder van `RijksICTGilde/RIG-Cluster`:

1. maakt een token aan, bij voorkeur een GitHub App met schrijfrechten op `contents`,
   `pull-requests`, `issues` en `workflows`; anders een fine-grained PAT met dezelfde
   rechten;
2. zet dat als repository secret `RENOVATE_TOKEN`;
3. start de workflow één keer met `workflow_dispatch`.

Daarna verschijnt het Dependency Dashboard als issue.

## De configuratie toetsen

```bash
npx --yes --package renovate renovate-config-validator renovate.json

# Wat Renovate in deze repo vindt, zonder iets te schrijven.
# Zonder GITHUB_COM_TOKEN lopen de github-tags-lookups tegen de rate limit aan.
GITHUB_COM_TOKEN=<pat> LOG_LEVEL=debug npx --yes renovate --platform=local --dry-run=lookup
```

Elke rij uit de dekkingstabel moet minstens één afhankelijkheid opleveren. Een rij die
nul oplevert is een gat in de config, geen gegeven.
