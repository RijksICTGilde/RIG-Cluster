# Security Scanning Pipeline

BIO2 A8.08 vulnerability management implementation.

## Overview

Automated security scanning in GitHub Actions covering:

- **Python dependency vulnerabilities** via pip-audit
- **Container image vulnerabilities** via Trivy
- **SBOM generation** for supply chain transparency
- **Dependency updates** via Renovate (zie `features/renovate.md`)

## Docker Image CI/CD

### Operations Manager (ZAD)

Automated build on push to main and PRs.

- **Workflow**: `.github/workflows/docker.yml`
- **Registry**: `ghcr.io/rijksictgilde/zad/operations-manager`
- **Versioning**: CalVer (`YYYY.M.PATCH`) + `sha-X` + `latest`, alleen op main
- **Platforms**: linux/amd64, linux/arm64 op main, alleen linux/amd64 op een PR
- **PR's pushen niet**: pull requests bouwen images, gatet ze, maar publiceren niet
- **Provenance en SBOM**: gepushte images dragen SLSA-provenance (`mode=max`) en SBOM, zijn
  keyless getekend met cosign en dragen een build-provenance-attestatie in de registry
- **Nachtelijke herbouw**: een `schedule` bouwt zonder laagcache op een `nightly-<datum>`-tag.
  Een `workflow_dispatch` is dezelfde herbouw met de hand: geen CalVer-nummer en geen git-tag
- **Alleen main publiceert**: zowel de push als het tekenen hangen aan `refs/heads/main`, dus
  een dispatch op een tak bouwt en gatet maar laat niets ongetekend achter in de registry
- **Tekenen staat in een eigen job**: `build` draait ook op een pull request en heeft daarom
  geen OIDC-token; de job `publish` tekent, attesteert en promoveert de tags, en draait
  alleen op main. Zie hieronder bij "Rechten en pins"

### Supporting Images

Manual trigger via GitHub Actions UI.

- **Workflow**: `.github/workflows/docker-images.yml`
- **Images**: rig-backup, cmp-kustomize-sops, postgresql-with-dictionaries, keycloak-zad
- **Versioning**: CalVer on dispatch, `nightly-<datum>` op de nachtelijke herbouw
- **Provenance, SBOM, handtekening en nachtelijke herbouw**: idem als de operations-manager

## Security Scanning

**Workflow**: `.github/workflows/security.yml`

| Job | Tool | What | When |
|-----|------|------|------|
| `dependency-audit` | pip-audit | Python deps against PyPI/OSV | Push, PR, weekly |
| `secret-scan` | `scripts/scan-secrets.py` | Every tracked file in the branch tree | Push, PR, weekly |
| `secret-scan-history` | `scripts/scan-secrets.py --history` | Every blob that ever existed, minus `scripts/secret-scan-baseline.json` | Weekly and on demand |
| `container-scan` | Trivy | The five images CI builds plus `argocd-rig` (HIGH/CRITICAL), en de configuratie van hun Dockerfile | Push, PR, weekly |
| `manifest-scan` | Trivy | De Kubernetes-manifesten in `bootstrap/` en `infrastructure/`, rapporterend | Push, PR, weekly |
| `sbom` | Trivy | CycloneDX SBOM | Main only |

Results appear in the **GitHub Security tab** (SARIF upload). The secret scan is the exception: its
output stays in the job log and goes nowhere else, because a list of where secrets sit is a map to
what is not revoked yet. The same tree scan also runs as `test_this_repository_is_clean_right_now`
in `tests/test_secret_scan.py`, so a build outside GitHub has the guard too. The layers and the
baseline are in `features/sops-sleutel-roteren.md`.

De job `sbom` bewaart bovendien een CycloneDX-SBOM als workflow-artefact (90 dagen), los
van de SBOM-attestatie aan gepushte images.

## Rechten en pins

De workflows volgen de OpenSSF Scorecard-checks "Pinned-Dependencies" en
"Token-Permissions", en BIO2 5.21 en 8.32.

- **Elke externe action staat op een volledige commit-SHA**, met de versie als
  commentaar erachter (`uses: actions/checkout@11d5960... # v4`). Een tag kan onder
  dezelfde naam van inhoud veranderen, een SHA niet. Renovate houdt de pins bij: het
  leest het versiecommentaar en schrijft SHA plus commentaar mee.
  `tests/test_workflows_actions_op_sha.py` bewaakt dat er geen losse tag terugsluipt.
- **Elke job begint met `step-security/harden-runner` op `egress-policy: block`**, met de
  allowlist in `env.HARDEN_ENDPOINTS` van die workflow. Dat is de vangrail achter de pins:
  een pin houdt tegen dat een action van inhoud verandert, niet wat hij doet zodra hij
  draait. Een geblokkeerd endpoint komt als annotatie in de run te staan; dat is de plek om
  te lezen wat er nog in de lijst hoort. `tests/test_workflows_harden_runner.py` bewaakt het.
- **Het workflowtoken staat op `contents: read`.** Per job komt erbij wat die job nodig
  heeft: `packages: write` voor de build-job van `docker.yml`, `contents: write` alleen
  voor de job `tag`, die niets anders doet dan de CalVer-tag zetten en alleen op main
  draait.
- **Het OIDC-token zit niet in een job die op een PR draait.** Bij een `pull_request`
  draait de workflowdefinitie van de PR-head met de rechten van die job, dus een extra stap
  in een PR zou met `id-token: write` een willekeurige image keyless kunnen tekenen op naam
  van deze repo. Het certificaat noemt dan een `refs/pull/N/merge`-ref, en een reviewer die
  alleen op het repo-voorvoegsel toetst ziet geen verschil. Daarom staan `id-token: write`
  en `attestations: write` in `docker.yml` bij de job `publish`, die aan main hangt, en
  heeft `build` ze niet. `docker-images.yml` kent geen `pull_request`-trigger en houdt ze
  bij de bouwende jobs. `tests/test_workflows_actions_op_sha.py` bewaakt het, en het
  verificatierecept in `SECURITY.md` ankert dezelfde grens in de regexp.

### De poort voor de push

`.github/actions/image-gate` is dezelfde poort, maar in het publicatiepad: `docker.yml` en
`docker-images.yml` roepen hem aan VOOR ze pushen. Hij bouwt de image lokaal (`load: true`)
en weigert op een oplosbare HIGH of CRITICAL en op een misconfiguratie in de Dockerfile.
Faalt hij, dan is er geen digest gepusht en dus niets om een tag naar te laten wijzen.

De poort staat apart van `security.yml` omdat die twee verschillende dingen doen: de poort
blokkeert in het publicatiepad, `security.yml` rapporteert naar het Security-tabblad en
dekt ook `argocd-rig`, dat niemand hier bouwt. Wat ze delen staat op een plek:
`.trivyignore.yaml` en de scannerversie, die `tests/test_trivyignore.py` gelijkhoudt aan
`TRIVY_SCANNER_VERSION` in `security.yml`.

### De config-gate

Naast de CVE's van de image meet `container-scan` de configuratie van de Dockerfile
(`scan-type: config`), met dezelfde severity en dezelfde uitzonderingslijst. Dat vindt wat
een CVE-scan niet ziet: een container zonder niet-root `USER`, een `apt-get install` zonder
`--no-install-recommends`. `argocd-rig` valt af, die heeft geen Dockerfile.

De Kubernetes-manifesten in `bootstrap/` en `infrastructure/` gaan door de job
`manifest-scan`, die RAPPORTEERT en niet gatet. De bevindingen daar zijn platformbesluiten
(OPI beheert secrets en namespaces, dat is zijn taak) en ontbrekende
`readOnlyRootFilesystem` per component. Elk daarvan afwegen is eigen werk; ze tegenhouden
zou de poort met tientallen uitzonderingen moeten openzetten, en dan meet hij niets meer.

### De container-gate

`container-scan` scant elke image twee keer met dezelfde Trivy-actie: eerst voor de SARIF
die naar het Security-tabblad gaat, daarna als gate met `exit-code: 1`. De gate faalt op een
**oplosbare** HIGH of CRITICAL (`ignore-unfixed: true`) die niet in `.trivyignore.yaml`
staat. De upload staat achter `if: always()`, dus het Security-tabblad blijft gevuld ook als
de gate rood is.

De gate draait met de Trivy-default `vuln,secret`, dus een sleutel die per ongeluk in een
image belandt zet hem ook rood. Eén bestand is daarop uitgezonderd: de snakeoil-testsleutel
die het Debian-pakket `ssl-cert` in de postgresql-base achterlaat. Die uitzondering staat in
de `secrets`-sectie van `.trivyignore.yaml` en is op pad gescoped, niet op id, zodat elk
ander bestand en de vijf andere images gewoon meetellen.

De matrix dekt de vijf images die CI zelf bouwt, plus `argocd-rig`. Die laatste bouwt niets:
hij scant de gepinde productie-image, met de versie uit
`bootstrap/rig-system/kustomize/overlays/odcn-production/argocd-deployment.yaml` zodat die
niet op twee plekken staat. Het pakket `minbzk/base-images/argocd-rig` staat publiek, dus er
is geen registry-login nodig en de entry blokkeert net als de andere vijf.

### Waar de bevindingen vandaan komen

Vrijwel alles wat de gate vindt zit in een laag van iemand anders, niet in onze eigen code:
een Go-stdlib of een vendored dependency in een binary die we meenemen (`mc`, `kopia`,
`sops`, `chisel`, de tools in cmp-kustomize-sops), of een Java-dependency in de
Keycloak-base. Go brengt ongeveer maandelijks nieuwe stdlib-CVEs uit, dus elke image met een
binary van een ander krijgt binnen weken weer nieuwe treffers. Twee dingen volgen daaruit:

- De eerste zet is de pin van dat gereedschap naar zijn nieuwste release verhogen. Per
  binary helpt dat flink: bij het inrichten van de gate ging kustomize van 27 naar 3
  bevindingen, helmfile van 24 naar 3 en chisel van 16 naar 3.
- Op het niveau van de hele image verschuift dat weinig, want de achterblijvers (`mc` en
  `ksops`, beide zonder nieuwere release) dragen vrijwel dezelfde stdlib-CVEs. Zo lang die
  er zijn, heeft de gate de uitzonderingslijst nodig om groen te kunnen staan.
- Blijft er iets over doordat er geen nieuwere release is, dan is dat een uitzondering met
  een korte vervaldatum, en geen reden om de gate uit te zetten.

Een laatste groep komt uit de vendor-map van pip in de python-slim base
(`pip/_vendor/vendor.txt`): `setuptools`, `urllib3` en `msgpack`. Een nieuwere pip bestaat
niet, en een `purls`-scope op naamniveau onderscheidt die kopie niet van dezelfde naam in
onze eigen lock. Dat die uitzondering niet ook een regressie in onze eigen boom afdekt komt
van `pip-audit --strict` in dezelfde workflow: dat scant `uv.lock` en gebruikt deze lijst
niet.

De OS-pakketten in de images die op `python:3.14-slim` staan zijn geen uitzondering: die
Dockerfiles halen de Debian-security-updates op met `apt-get upgrade`, net als
cmp-kustomize-sops dat voor Ubuntu doet. Komt er toch een OS-treffer uit de gate, dan is dat
de plek om te kijken, niet `.trivyignore.yaml`. Daarom staan de OS-regels in de lijst op een
`purls`-scope: anders zou een uitzondering namens een base-image van iemand anders diezelfde
CVE in onze eigen apt-laag stil zetten, en juist die laag wordt gecached.

### Een uitzondering toevoegen

`.trivyignore.yaml` is de uitzonderingslijst. Een regel heeft drie velden, en een
OS-pakket er vier:

```yaml
vulnerabilities:
  - id: CVE-2026-12345
    statement: niet bereikbaar, de module wordt niet geladen
    expired_at: 2026-11-16
  - id: CVE-2026-84782
    purls:
      - pkg:deb/ubuntu/openssl?distro=ubuntu-26.04
    statement: zit in argocd-rig, herbouwen op een nieuwere upstream is een eigen taak
    expired_at: 2026-11-16
```

- `statement` is de afweging, en die is verplicht: BIO2 8.08.02 vraagt dat een risico dat je
  niet oplost expliciet is afgewogen.
- `expired_at` zorgt dat de bevinding terugkomt. Houd hem kort, ongeveer 30 dagen; verlengen
  is een nieuwe afweging en dus een nieuwe diff die iemand goedkeurt.
- `purls` beperkt de regel tot de genoemde pakketten. Zonder dat veld onderdrukt Trivy op
  CVE-id alleen, en zet een uitzondering voor de base-image van iemand anders dezelfde CVE
  stil in onze eigen lagen. Een OS-pakket heeft dus altijd een `purls`-scope, op naamniveau
  zonder versie (`pkg:deb/ubuntu/openssl`, niet `...@3.5.5-1ubuntu3.3`), want een purl met
  versie stopt met matchen na de eerste bump. De namen staan in `.PkgIdentifier.PURL` van
  `trivy image --format json`. Een `*`-wildcard werkt niet. Voor een Go- of
  Java-dependency in een binary van een ander hoeft het niet: die id komt niet in onze
  eigen laag terug.
- Draagt een van onze eigen images dezelfde distro als de base waarvoor de uitzondering
  bedoeld is, zet dan ook `?distro=` erbij. Concreet: `cmp-kustomize-sops` staat op
  `argocd:v2.14.21`, dus Ubuntu, net als `argocd-rig`. Een scope op naam alleen zet een
  uitzondering voor argocd-rig (`ubuntu-26.04`) ook stil in die eigen apt-laag
  (`ubuntu-24.04`), waar `apt-get upgrade` hem juist ophaalt. Gemeten op `argocd:v2.14.21`:
  met een scope op naam alleen verdwijnt `CVE-2026-84782` in openssl daar, met
  `?distro=ubuntu-26.04` blijft hij staan. De qualifier bevat geen versie, dus hij hoeft
  niet bijgehouden te worden; wordt argocd-rig op een nieuwere Ubuntu herbouwd, dan valt de
  bevinding terug in de gate en is dat een nieuwe afweging.
- `tests/test_trivyignore.py` weigert een regel zonder reden of vervaldatum, een
  vervaldatum die verder dan 90 dagen vooruit ligt, en een purl met een versie erin.

De IDs haal je uit de table-uitvoer van de gate-stap in de falende run. Los eerst op wat met
een patch- of minor-update opgelost kan worden; een uitzondering is voor wat daarna
overblijft, bijvoorbeeld een treffer waarvoor geen upstream-fix bestaat.

Lokaal nameten zonder op CI te wachten:

```bash
docker build -f operations-manager/Dockerfile --target application -t operations-manager:scan .
trivy image --severity HIGH,CRITICAL --ignore-unfixed --ignorefile .trivyignore.yaml \
  operations-manager:scan
```

Neem dezelfde Trivy als CI, `TRIVY_SCANNER_VERSION` in `security.yml`: een andere versie
meet andere aantallen.

Voor `argocd-rig` hoef je niets te bouwen: `trivy image` op
`ghcr.io/minbzk/base-images/argocd-rig:<versie uit de overlay>` haalt hem zelf op.

## Dependency Management

**Renovate** draait als GitHub Action (`.github/workflows/renovate.yml`) met de
configuratie in `renovate.json`, en vervangt Dependabot. Elke maandagochtend maakt het
PR's per ecosysteem voor Python, Go, Maven, npm, Dockerfiles, Kubernetes-manifesten,
remote kustomize-resources, de Taskfile, `.tool-versions` en de GitHub Actions zelf.
Majors komen alleen op het Dependency Dashboard en worden pas een PR na goedkeuring.

Zie `features/renovate.md` voor de dekking, de rcr-mapping, wat met opzet niet wordt
bijgewerkt, en de handstap bij het inrichten.

Een update wacht 7 dagen voor hij een PR wordt (`minimumReleaseAge`), zodat een
gecompromitteerde of ingetrokken release de kans krijgt om op te vallen. Alleen het
bouwgereedschap (GitHub Actions en `.tool-versions`) mergt zichzelf, en alleen op patch en
minor. De twee patchlagen staan in `SECURITY.md`.

## Setup

### TODO

- [ ] Zet het repository secret `RENOVATE_TOKEN` en start `renovate.yml` een keer met
      `workflow_dispatch` (zie `features/renovate.md`)
- [ ] Verify GHCR packages write permission for GitHub Actions (`Settings > Actions > General > Workflow permissions`)
- [ ] Loop de eerste ronde Renovate-PR's na
- [ ] Zet branch protection op main met de buildpoort als required check, zet auto-merge
      aan en force-push uit (zie `SECURITY.md`); zonder die drie kan laag B van de
      patchstrategie langs een rode poort mergen
- [ ] Draai de eerste run met `egress-policy: block` na en vul `HARDEN_ENDPOINTS` aan met
      wat de harden-runner-annotatie als geblokkeerd meldt
- [ ] Check GitHub Security tab after first `security.yml` run for SARIF results
- [ ] Migrate Taskfile publish tasks from `ghcr.io/minbzk/base-images/` to `ghcr.io/rijksictgilde/zad/`
- [ ] Update image references in `bootstrap/` deployment patches (operations-manager, cmp-kustomize-sops)
- [ ] Update image references in `infrastructure/` manifests (rig-backup, etc.)
- [ ] Update Skaffold configs (`operations-manager/skaffold*.yaml`) to new registry path

## Local Usage

```bash
# Check Python dependencies for vulnerabilities
task security-audit

# Scan Docker image with Trivy (requires Trivy installed)
task security-scan-image
```

Or directly:

```bash
cd operations-manager/python
uv run pip-audit --strict --desc
```
