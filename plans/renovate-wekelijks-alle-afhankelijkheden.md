# Renovate houdt wekelijks alle afhankelijkheden bij

## Aanleiding

Niets in deze repo controleert of er nieuwe versies zijn. Het gevolg, gemeten op 16-10-2026:

- De sandbox draait Kubernetes 1.32.0 (`sandboxed-local/kind-config.yaml`), productie 1.34.9.
- Keycloak staat op 25.0.6 (`images/keycloak-zad/Dockerfile`), Postgres op 17 (`infrastructure/bootstrap/infrastructure/postgresql/database/base/cluster.yaml`), de ArgoCD-basisimage op v2.14.21, `.tool-versions` op kind 0.27.0 terwijl lokaal 0.32.0 draait, Go-builds op `golang:1.23-alpine`, `alpine:3.19`.

Er is al eens een poging gedaan. Commit `639d95e0c` (11-03-2026) verving Dependabot door een `renovate.json`, en `features/security-scanning-pipeline.md` beschrijft die nog steeds. Maar de afvinkpunten daar ("Install the Renovate GitHub App", "Review first Renovate onboarding PR") zijn nooit gedaan, en `renovate.json` staat niet meer op main. Er draait dus niets, terwijl de documentatie zegt van wel.

## Doel

Elke week maakt Renovate, als GitHub Action in `RijksICTGilde/RIG-Cluster`, PR's voor alle bronnen hieronder, zodat updates gestructureerd en gepland binnenkomen. Majors komen alleen op het Dependency Dashboard en worden pas een PR na goedkeuring. Daarnaast neemt deze taak de eerste ronde patch- en minor-updates mee, getest.

## Wat Renovate moet dekken

Gemeten op main `42878026f`. De builder controleert de lijst opnieuw en vult hem aan.

| Bron | Waar | Renovate-manager |
|---|---|---|
| Python | `operations-manager/python/pyproject.toml` + `uv.lock` | `pep621` (uv) |
| Go | `images/e2e-allservices/go.mod`, `images/zad-waker/go.mod` | `gomod` |
| Maven | `keycloak-migration/custom-mapper/pom.xml`, `keycloak-migration/relay-email-sender/pom.xml` | `maven` |
| npm | `operations-manager/python/static/js/package.json`, `presentation/reveal/package.json` | `npm` |
| Dockerfiles | 15 stuks: `images/*/Dockerfile`, `operations-manager/Dockerfile`, `operations-manager/backup-image/Dockerfile` | `dockerfile` |
| Kubernetes/kustomize | `image:`/`newName`+`newTag`/`imageName:` in `infrastructure/` en `bootstrap/` (34 unieke verwijzingen) | `kubernetes`, `kustomize` |
| Kustomize remote resources | versies in URL's, bv. `kubernetes-csi/external-snapshotter/v8.2.0/...`, `csi-driver-host-path/v1.17.1/...` | custom regex, datasource `github-tags` |
| Taskfile | `cnpg-1.27.3.yaml`-URL, de snapshot-CRD-URL's in `Taskfile.yaml` | custom regex |
| kind-node | `kindest/node:v1.32.0` in `sandboxed-local/kind-config.yaml` | custom regex, datasource `docker` |
| Tools | `.tool-versions` | `asdf` |
| GitHub Actions | `.github/workflows/*.yml` | `github-actions` |

## Plan

Namen hieronder zijn voorstellen, geen bestaande code, tenzij ze als bestaand worden genoemd.

1. **`renovate.json` op de root**, met als basis `config:recommended` en:
   - schedule wekelijks (maandagochtend, zoals de oude config en `security.yml`), `timezone: Europe/Amsterdam`;
   - Dependency Dashboard aan;
   - patch en minor gegroepeerd per ecosysteem (niet alles in één PR: een rode Python-update mag een Dockerfile-update niet blokkeren);
   - majors met `dependencyDashboardApproval: true`: alleen op het dashboard, PR pas na een vinkje;
   - geen automerge;
   - `prConcurrentLimit` en `prHourlyLimit` laag genoeg dat een eerste run geen tientallen PR's opent;
   - de custom regex-managers uit de tabel.
   → toets: `npx --yes renovate-config-validator renovate.json` slaagt.
2. **De rcr-paden.** De odcn-overlays bevatten bewust `rcr.rijksapps.nl/<proxy>/...`-paden; de admission rewrite op ODCN vraagt dat, anders blijft ArgoCD eeuwig OutOfSync. Renovate moet de versie opzoeken bij de echte bron (`rcr.rijksapps.nl/ghcr-rig/X` → `ghcr.io/X`, `dockerhub-rig/X` → `docker.io/X`, `k8s-rig/X` → `registry.k8s.io/X`, `quay-rig/X` → `quay.io/X`) en het rcr-pad in de PR laten staan. Gebruik daarvoor `packageRules` met `overridePackageName` of een gelijkwaardig Renovate-mechanisme.
   → toets: in de dry-run van stap 4 vindt Renovate voor minstens één rcr-image (bv. `rcr.rijksapps.nl/k8s-rig/external-dns/external-dns:v0.15.0`) een nieuwere versie, en het voorgestelde bestand behoudt het rcr-pad.
3. **Uitsluiten wat geen afhankelijkheid is:**
   - eigen images met CalVer-tags (`operations-manager`, `keycloak-zad`): hun pin in de odcn-overlay is een deploybeslissing in git, die neemt een mens;
   - `localhost:5001/...` en kale lokale namen (`operations-manager:latest`, `rig-cmp-argo-kustomize-sops:latest`);
   - voorbeeld- en testwaarden in `operations-manager/python/opi` en `tests/` (`ghcr.io/voorbeeld/...`, `ghcr.io/myorg/...`): Renovate kijkt niet in Python-code, controleer dat het zo blijft;
   - `archive/`.
   Images op `:latest` of een zwevende tag (`jpillora/chisel:latest`, `redis:7-alpine`, `busybox:1.36`) kan Renovate niet zinnig bijwerken. Laat ze voor nu staan, maar zet ze in de PR-beschrijving als lijst voor een vervolgtaak (pinnen op versie of digest).
4. **Lokaal proefdraaien.** `LOG_LEVEL=debug npx --yes renovate --platform=local --dry-run=lookup` vanuit de root, uitvoer bewaren.
   → toets: elke rij uit de tabel levert minstens één gevonden afhankelijkheid op; noteer per manager het aantal in de PR-beschrijving. Een rij die nul oplevert is een gat in de config, geen gegeven.
5. **De workflow `.github/workflows/renovate.yml`:** `renovatebot/github-action` (versie vastgepind), `schedule` op maandagochtend plus `workflow_dispatch`, `RENOVATE_REPOSITORIES: RijksICTGilde/RIG-Cluster`. Het token komt uit een secret `RENOVATE_TOKEN`, niet uit `GITHUB_TOKEN`: PR's die met `GITHUB_TOKEN` worden gemaakt starten geen andere workflows, en dan draait `ci.yml` niet op de Renovate-PR's.
   → toets: `actionlint` op het bestand (via `npx` of de Docker-image) is schoon.
6. **Eerste ronde patches en minors** in dezelfde branch, op basis van de dry-run uit stap 4. Alleen patch en minor, geen majors. Per ecosysteem een eigen commit, zodat een terugdraai klein blijft.
   Niet in deze taak, ook niet als het een minor lijkt: de eigen CalVer-images, Keycloak, Postgres, ArgoCD/argocd-operator, CNPG en de kind-node. Die hebben elk een eigen migratie of afhankelijkheid met productie en worden aparte taken; zet ze met de gevonden doelversie in de PR-beschrijving.
   → toets per ecosysteem:
   - Python: `uv lock`, daarna `uv run pytest tests/ -q`, `uv run ruff check .`, `uv run pyright` vanuit `operations-manager/python`;
   - Go: `go build ./...` en `go test ./...` in beide `images/*`-mappen;
   - Maven: `mvn -q package` in beide `keycloak-migration/*`-mappen;
   - Dockerfiles: elke gewijzigde Dockerfile bouwt (`docker buildx build --load`);
   - kustomize: `kustomize build` op elke overlay die een gewijzigde image bevat (met de SOPS-variant uit `CLAUDE.md` waar nodig);
   - samen: een verse sandbox via `orch sandbox claim`, `task sandbox:setup` draait door tot het eind, en alle Argo-apps staan op Synced/Healthy.
7. **Documentatie.** Werk `features/security-scanning-pipeline.md` bij: het Renovate-deel beschrijft nu een app die nooit is geïnstalleerd. Beschrijf wat er nu draait, waar het dashboard staat, hoe je een major goedkeurt, de rcr-mapping, en de handstap hieronder.

## Hoe een Renovate-PR in de ontwikkeling komt

Ontwikkeling gebeurt op Forgejo main; GitHub main krijgt releases, en na een release wordt GitHub main teruggemerged (zie bv. `fe90713c0` "GitHub main terugmergen na de release-PR (#217)"). Een Renovate-PR die op GitHub gemerged wordt, komt dus met de eerstvolgende terugmerge op Forgejo. Beschrijf dat in de documentatie uit stap 7. Verander de flow in deze taak niet.

## Handstap na de merge (niet voor de builder)

Een beheerder van `RijksICTGilde/RIG-Cluster` maakt het token aan (bij voorkeur een GitHub App met `contents`, `pull-requests`, `issues` en `workflows` schrijven; anders een fine-grained PAT) en zet het als repository secret `RENOVATE_TOKEN`, en start de workflow één keer met `workflow_dispatch`. De builder pusht niets naar GitHub en maakt geen secrets aan.

## Wat hierdoor schrijfbaar wordt

Een bot met schrijfrechten opent PR's op GitHub. Hij merget niet: er is geen automerge, dus mergen blijft mensenwerk. De rcr-paden en de CalVer-pins zijn uitgesloten of blijven behouden, dus een Renovate-PR kan geen productie-uitrol starten zonder dat een mens de PR merget.

## Buiten scope

- Majors en de uitgesloten onderdelen uit stap 6. Elk een eigen taak.
- `:latest`-images pinnen. Vervolgtaak op basis van de lijst uit stap 3.
- Andere repositories dan RIG-Cluster.
- De release- en terugmergeflow tussen GitHub en Forgejo.

## Afronding

`uv run ruff check . --fix`, `uv run ruff format .`, `uv run pyright` vanuit `operations-manager/python`. Geen em dashes. Nederlandse prose met de Engelse vaktermen uit de woordenlijst in `CLAUDE.md`.
