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
| Maven | `images/keycloak-zad/extensions/*/pom.xml` | `maven` |
| npm | `operations-manager/python/static/js/package.json`, `presentation/reveal/package.json` | `npm` |
| Dockerfiles | 15 stuks in `images/`, `operations-manager/` | `dockerfile` |
| docker compose | `docker-compose.dev.yaml` | `docker-compose` |
| Kubernetes-manifesten | `image:` in `infrastructure/`, `bootstrap/` | `kubernetes` |
| Kustomize-overlays | `newName` + `newTag` in `infrastructure/`, `bootstrap/` | `kustomize` |
| GitHub Actions | `.github/workflows/*.yml` | `github-actions` |
| Tools | `.tool-versions` | `asdf` + custom managers voor `task` en `mc` |
| Remote kustomize-resources | versies in de raw.githubusercontent-URL's | custom manager |
| Taskfile | de CNPG-URL (patch en het `release-X.Y`-segment), de snapshot-CRD-URL, de argocd-operator-versie | custom manager |
| kind-node | `sandboxed-local/kind-config.yaml` | custom manager |
| Versies in een Dockerfile-ARG | `ARG <NAAM>_VERSION` met een `# renovate:`-annotatie | preset `customManagers:dockerfileVersions` |
| De themajar van keycloak-zad | de versie in de download-URL | custom manager |

Een nieuwe meting doe je met een dry-run, zie onderaan.

De actions staan op een commit-SHA met de versie als commentaar erachter
(`uses: actions/checkout@11d5960... # v4`). Renovate leest dat commentaar als de huidige
versie en schrijft bij een update zowel de SHA als het commentaar mee.

## Hoe een update binnenkomt

Een net gepubliceerde versie wacht eerst zeven dagen (`minimumReleaseAge`), zodat een
ingetrokken of gecompromitteerde release de kans krijgt om op te vallen voordat wij hem
binnenhalen.

Patch en minor komen gegroepeerd per ecosysteem: een eigen PR voor python, go, maven,
npm, dockerfile, kubernetes, github actions, tool-versions en remote manifests. Dat is
met opzet geen enkele grote PR, want dan blokkeert een rode Python-update een
Dockerfile-update.

Een major wordt geen PR. Die staat op het Dependency Dashboard onder "Pending
Approval" met een checkbox. Zet je dat vinkje, dan maakt Renovate bij de volgende run de
PR. Dat is de goedkeuringsstap: een major vraagt om een migratie, en die hoort een mens
te plannen.

Auto-merge geldt sinds RC-369 alleen voor het bouwgereedschap: patch en minor op de GitHub
Actions en op `.tool-versions`, en pas nadat de required checks groen zijn. Dat is laag B van
de patchstrategie in `features/security-scanning-pipeline.md`. Een digest-update is een verplaatste tag en gaat niet
mee. Al het andere, en elke major, blijft een merge die een mens doet.

## Een versie in een Dockerfile-ARG

Een `ARG SOPS_VERSION=v3.13.2` die daarna in een `curl`-URL belandt, ziet Renovate niet:
hij kent `FROM`, `image:` en lockfiles. De annotatie erboven maakt hem wel zichtbaar.

```dockerfile
# renovate: datasource=github-releases depName=getsops/sops
ARG SOPS_VERSION=v3.13.2
```

De vorm komt van de preset `customManagers:dockerfileVersions`, die in `extends` staat:

```
# renovate: datasource=<ds> depName=<naam> [versioning=<v>] [extractVersion=<regex>]
ARG <NAAM>_VERSION=<versie>
```

- `extractVersion` haalt de versie uit een tag met een voorvoegsel, zoals
  `^kustomize/v(?<version>.+)$` of `^v(?<version>.+)$` als de ARG de `v` niet meeschrijft.
- `versioning` is nodig als de versie geen semver is. `minio/mc` tagt met
  `RELEASE.<datum>`, dus daar staat een `regex:`-versioning op de regel.
- `.tool-versions` kent dezelfde annotatie, via een eigen custom manager.

Een tool dat in meer dan een bestand staat (sops, kopia, mc) gaat via `packageRules` in een
enkele PR, want een halve ophoging laat de checksumcontrole in het andere bestand omvallen.

`operations-manager/python/tests/test_dockerfile_downloads_hebben_een_checksum.py` bewaakt
dat elke `ARG <NAAM>_VERSION` een annotatie heeft, in een `FROM` wordt gebruikt, of met een
reden in de uitzonderingslijst staat.

## De sommen gaan in dezelfde PR mee

Sinds RC-294 staat naast elke binary-download een literale sha256 per architectuur.
Renovate werkt die niet bij, dus een PR die alleen de versie ophoogt zou falen op
`sha256sum -c`. `scripts/renovate_checksums.py` schrijft ze mee: Renovate draait het als
`postUpgradeTasks` over de branch, en `RENOVATE_ALLOWED_COMMANDS` in `renovate.yml` laat
precies dat ene commando toe.

Het script leest per gereedschap het checksumbestand dat het project zelf bij de release
publiceert (`checksums.txt`, `<artefact>.sha256sum`, een `.sha256` met alleen de som, of de
hashtabel van yq). Publiceert een project niets (helm-diff, code-server, de themajar van
keycloak-zad), dan rekent het de som uit over het artefact en zegt dat in zijn uitvoer. De
bronnen staan in de tabel `RELEASES` bovenin het script; een nieuw gereedschap met een
checksum-ARG hoort daar een regel te krijgen.

### Alleen de som van een gewijzigde versie

Het script raakt alleen de sommen aan van een gereedschap waarvan de `ARG <NAAM>_VERSION`
in deze werkboom wijzigt, gemeten met `git diff HEAD`. Renovate schrijft de ophoging in de
werkboom voordat het de postUpgradeTasks draait en commit pas daarna, dus dat is precies
wat deze branch ophoogt.

Die afbakening is er om de grendel van RC-294 niet te ontwapenen. Voor helm-diff,
code-server en de themajar is er geen tweede bron, dus rekent het script de som uit over de
bytes die het downloadt. Zou het dat op elke branch voor elk gereedschap doen, dan werd een
upstream dat zijn artefact onder een bestaande tag vervangt stil opnieuw gepind in plaats
van luid gestopt door `sha256sum -c`. Het scheelt ook de honderden MB's die het anders op
elke branch zou downloaden.

`postUpgradeTasks` staat daarom bewust op het hoogste niveau en niet in een
`packageRules`-regel met `matchFileNames`. Dat laatste zou de poort op de verkeerde plek
leggen: Renovate neemt de branch-config uit `upgrades[0]`
(`workers/repository/updates/generate.js`), dus in een gegroepeerde branch zoals die van
`minio/mc` (`.tool-versions` plus `images/mc/Dockerfile`) hangt het van de sorteerorde af of
de regel meekomt, en dan blijven de sommen stil achter. Bovendien dekt het niets binnen een
Dockerfile-branch: een sops-ophoging zou de som van code-server nog steeds herpinnen.

Met de hand draaien kan ook, en is de snelste controle na een ophoging:

```bash
python3 scripts/renovate_checksums.py images/mc/Dockerfile # na je eigen wijziging
python3 scripts/renovate_checksums.py --alles              # alles doorrekenen
```

Het script toetst geen handtekening bij de release. Dat blijft handwerk, zie
`images/README.md`.

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
  `rig-cmp-argo-kustomize-sops`). De sidecar staat sinds RC-348 op een digest; ophogen
  gaat met `task pin-cmp-image` en is een deploybeslissing, dus ook de digest-update blijft
  uit.
- **Lokale sandboxverwijzingen** (`localhost:5001/...`, `operations-manager:latest`): er
  is geen registry om een versie bij op te zoeken.
- **Shellscripts** (`scripts/*.sh`). Geen Renovate-manager leest een `.sh`, ook geen custom
  manager, dus `registry:2.8.3` in `scripts/setup-kind-registry.sh` krijgt nooit een
  update-PR. `TestVasteVersies` leest die regel wel, dus de pin kan niet stil terugvallen.
- **De `imageName` van de platformdatabase**
  (`infrastructure/bootstrap/infrastructure/postgresql/database/base/cluster.yaml` en de
  `/spec/imageName`-patch in de twee odcn-overlays). De kubernetes-manager leest alleen
  `image:`-regels en de kustomize-manager alleen `images:`-entries, dus `17.11` krijgt nooit
  een update-PR. `TestVasteVersies` leest die drie regels wel, dus de pin kan niet stil
  terugvallen.
- **`archive/`**, de testmappen en `self-service-portal/` (een schets, geen app).

## Zwevende tags: gedekt

Geen image van derden onder `infrastructure/`, `bootstrap/`, `images/` of `scripts/` staat
nog op een tag die meebeweegt. Voor de yaml- en Dockerfile-verwijzingen stelt Renovate er
gewone patch- en minor-PR's voor, op twee plekken na: `scripts/` en de `imageName` van de
platformdatabase leest geen enkele manager, dus daar houdt alleen de grendel de pin vast.
RC-348 pinde `rig-cmp-argo-kustomize-sops` en `rig-backup`, RC-353 de images van derden.
`TestVasteVersies` in `operations-manager/python/tests/test_image_layer_guard.py` houdt dit
vast en noemt per uitzondering de reden.

## Wat nog niet gedekt is

Renovate kan hier niets zinnigs voorstellen; elk van deze punten is een eigen vervolgtaak:

- De twee VS Code-extensies in `images/jupyterhub-user/Dockerfile`
  (`PYTHON_EXTENSION_VERSION`, `BASEDPYRIGHT_VERSION`). Renovate heeft geen datasource voor
  de extensiemarktplaats, dus die blijven handwerk. Ze staan met die reden in de
  uitzonderingslijst van de test.
- `mc` in `.tool-versions`: volgt `MC_VERSION` uit `images/mc/Dockerfile`, en dat is een
  MinIO RELEASE-datum.
- `aquasecurity/trivy-action` in `security.yml` is op een kale SHA gepind, zonder
  `# vX.Y.Z` erachter. Renovate meldt `unversioned-reference` en laat hem staan. Zet de
  versie in een commentaar achter de SHA en hij gaat wel mee.
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
