# Beveiligingsbeleid

RIG-Cluster draait ZAD (Zelfservice Applicatie Deployment), een Rijksvoorziening onder [BIO2](https://www.bio-overheid.nl/).

## Een kwetsbaarheid melden

- Meld het vertrouwelijk via GitHub: [Report a vulnerability](https://github.com/RijksICTGilde/RIG-Cluster/security/advisories/new) (alleen het team ziet je melding).
- Of meld het via [Coordinated Vulnerability Disclosure van het NCSC](https://www.ncsc.nl/contact/kwetsbaarheid-melden) als je het team niet kunt bereiken.

Meld ook vermoedens en houd bevindingen vertrouwelijk totdat het team heeft kunnen reageren.

## Wat je van ons mag verwachten

- Je krijgt een reactie van het team op iedere melding.
- Kwetsbaarheden waarbij de kans op misbruik en de verwachte schade beide hoog zijn, mitigeren we zo snel mogelijk en uiterlijk binnen één week (BIO2 8.08).
- We houden je op de hoogte van de afhandeling van je melding.

## Hoe we beveiliging geregeld hebben

- **Toegang**: inloggen gaat via Keycloak SSO-Rijk ([`opi/api/auth_routes.py`](operations-manager/python/opi/api/auth_routes.py), [`opi/configs/keycloak/bootstrap.yaml`](operations-manager/python/opi/configs/keycloak/bootstrap.yaml)).
- **Autorisatie**: wat je met een project mag, hangt af van je rol in dat project ([`opi/services/project_authorization.py`](operations-manager/python/opi/services/project_authorization.py)); machine-toegang loopt over een API-sleutel per project ([`opi/utils/api_keys.py`](operations-manager/python/opi/utils/api_keys.py)).
- **Geheimen in git**: alles wat een geheim is, staat versleuteld met SOPS en AGE ([`opi/utils/sops.py`](operations-manager/python/opi/utils/sops.py), [`decrypt-sops.yaml`](infrastructure/bootstrap/infrastructure/secrets/config/overlays/odcn/decrypt-sops.yaml)).
- **Geheimen in logs**: een logfilter maskeert wachtwoorden, tokens, AGE-sleutels en API-sleutels voordat een regel wordt weggeschreven ([`opi/utils/logging_redact.py`](operations-manager/python/opi/utils/logging_redact.py)).
- **Herleidbaarheid**: wie of wat wijzigde, wordt server-side vastgesteld en gaat mee in commits en taakadministratie ([`opi/core/actor.py`](operations-manager/python/opi/core/actor.py)).
- **Secret-scan**: iedere pull request en iedere push naar `main` draait een eigen scan over de volledige boom op gecommitte geheimen ([`scripts/scan-secrets.py`](scripts/scan-secrets.py), job `secret-scan` in [`security.yml`](.github/workflows/security.yml)).
- **Dependencies**: `pip-audit` toetst de Python-dependencies tegen OSV, bij iedere pull request, bij iedere push naar `main` en wekelijks (job `dependency-audit` in [`security.yml`](.github/workflows/security.yml)).
- **Container-images**: Trivy scant de Operations Manager, de back-upimage, de SOPS-sidecar en onze eigen PostgreSQL-, Keycloak- en ArgoCD-image op kwetsbare OS-pakketten en bibliotheken; bevindingen komen als SARIF in de Security-tab van de repository (job `container-scan` in [`security.yml`](.github/workflows/security.yml)). Een image die we zelf bouwen, gaat pas de registry in als hij langs de poort komt, en wordt getekend; zie [De toeleverketen van de images](#de-toeleverketen-van-de-images).
- **Eigen code**: semgrep en CodeQL lezen de code statisch op injectie en op een webroute die zonder authenticatie open staat; gitleaks scant de boom op sleutelvormen en wekelijks de hele historie (jobs `sast-semgrep`, `sast-codeql`, `gitleaks` en `gitleaks-history` in [`security.yml`](.github/workflows/security.yml)).
- **Gegenereerde manifests**: kube-linter en checkov meten de manifests die ZAD voor elke tenant genereert op `securityContext`, niet-root en een vaste tag (job `manifest-posture` in [`security.yml`](.github/workflows/security.yml)).
- **Het draaiende portaal**: nuclei vuurt ruim 11.000 templates af op een instantie die de run zelf opzet op de loopback van de runner, met en zonder sessie ([`aanvalsproef.yml`](.github/workflows/aanvalsproef.yml)). Wat die scanners vinden gaat door een poort met een uitzonderingslijst met reden en vervaldatum; zie [`features/aanvalsproef-harnas.md`](features/aanvalsproef-harnas.md).
- **Schotten op het cluster**: Kyverno-policies in waarschuwmodus en Falco voor runtime-detectie ([`features/kyverno-admission-audit.md`](features/kyverno-admission-audit.md), [`features/falco-runtime-detectie.md`](features/falco-runtime-detectie.md)).
- **Dekking**: welke ASVS-eis door welke toets gedekt is, en wat handwerk blijft, staat in [`docs/asvs-zelfbeoordeling.md`](docs/asvs-zelfbeoordeling.md).
- **Wijzigingen**: de testsuite, de linter en de typecontrole draaien op iedere pull request en iedere push naar `main` ([`ci.yml`](.github/workflows/ci.yml)); de gevoelige paden hebben een vaste eigenaar ([`.github/CODEOWNERS`](.github/CODEOWNERS)).

De volledige BIO-verantwoording per control wordt hier niet herhaald; die is intern belegd.

## De toeleverketen van de images

Hoe de images van dit platform gebouwd, gescand en getekend worden, en hoe je zelf nameet dat wat er draait is wat wij gebouwd en getekend hebben. De scanners, de uitzonderingslijst en het toevoegen van een uitzondering staan in [`features/security-scanning-pipeline.md`](features/security-scanning-pipeline.md).

### De runner staat dicht

Elke job in `.github/workflows/` begint met `step-security/harden-runner` op `egress-policy: block`. Alles buiten de allowlist van die workflow wordt tegengehouden, dus een gecompromitteerde action of buildstap kan niets naar buiten praten. De lijst staat per workflow in `env.HARDEN_ENDPOINTS`, en een geblokkeerd endpoint komt als annotatie in de run te staan: dat is de plek om te lezen wat erbij hoort.

Dat geldt ook voor de jobs van `aanvalsproef.yml`, die een eigen allowlist hebben: die workflow haalt de nuclei-templates, de kind-binary en de Kyverno- en Falco-images op. Die lijst is afgeleid uit de bron van elk werktuig en op een echte GitHub-run nog niet gemeten.

Dit is de vangrail achter de SHA-pins: elke externe action staat op een commit-SHA, dus de inhoud kan niet onder dezelfde naam veranderen. `tests/test_workflows_actions_op_sha.py` en `tests/test_workflows_harden_runner.py` bewaken die twee.

### De poort staat voor de push

De vijf images die deze repo zelf bouwt gaan door `.github/actions/image-gate` voordat er iets gepusht wordt. Die poort bouwt de image lokaal (`load: true`) en weigert op:

- een **oplosbare** HIGH of CRITICAL kwetsbaarheid die niet in `.trivyignore.yaml` staat;
- een **misconfiguratie** in de Dockerfile (HIGH of CRITICAL), bijvoorbeeld een container zonder niet-root `USER`.

Faalt de poort, dan is er geen digest gepusht en dus niets om een tag naar te laten wijzen. Een pull request komt tot de poort en pusht niet.

De Kubernetes-manifesten in `bootstrap/` en `infrastructure/` worden met dezelfde scanner gemeten, maar in de job `manifest-scan` van `security.yml`, die rapporteert naar het Security-tabblad en niet gatet. De bevindingen daar zijn platformbesluiten (OPI beheert secrets en namespaces, dat is zijn taak) en ontbrekende `readOnlyRootFilesystem` per component. Die afwegen is eigen werk; ze tegenhouden zou de poort met tientallen uitzonderingen moeten openzetten, en dan meet hij niets meer.

### Patchen in twee lagen

**Laag A, de OS-pakketten, gaat zonder mens.** `docker.yml` en `docker-images.yml` hebben een `schedule` die elke nacht de images herbouwt zonder laagcache (`no-cache`), zodat de `apt-get upgrade` in de Dockerfile de verse security-updates ophaalt. Zo'n run publiceert op een `nightly-<datum>`-tag plus `latest` en zet geen git-tag: CalVer hoort bij een merge die iemand doet. De poort hierboven staat ook voor die nachtelijke push.

Twee van de vijf images hebben geen eigen apt-laag: `postgresql-with-dictionaries` kopieert alleen woordenboeken in de CNPG-base, en `keycloak-zad` voegt providers toe aan de Keycloak-base. Voor die twee levert de nachtelijke herbouw alleen op wat de base-tag zelf intussen heeft gekregen.

**Laag B, het bouwgereedschap, gaat via Renovate** (`renovate.json`, zie `features/renovate.md`) met twee afspraken:

- een **cooldown** van 7 dagen (`minimumReleaseAge`): een net gepubliceerde versie wordt niet meteen een PR, zodat een gecompromitteerde of ingetrokken release de kans krijgt om op te vallen voordat wij hem binnenhalen;
- **selectieve auto-merge**: alleen patch en minor, en alleen voor het bouwgereedschap (de GitHub Actions en `.tool-versions`). De manifesten, de base-images en de app-afhankelijkheden blijven een merge die iemand doet. Een major komt alleen op het Dependency Dashboard, en een digest-update gaat ook niet mee: bij een action die op een SHA staat met een major-only versiecommentaar komt een verplaatste tag binnen als digest-update, en dat is precies waar die pin tegen beschermt.

Een auto-merge gaat pas door als de required checks groen zijn, dus de poort hierboven staat er ook voor.

### Een gepubliceerde image verifieren

Elke image die op main gepusht wordt is keyless getekend (cosign, OIDC, geen sleutel in beheer) en draagt een SLSA-build-provenance-attestatie in de registry. Zet `IMAGE` op de image met de tag of de digest die je wilt narekenen:

```bash
IMAGE=ghcr.io/rijksictgilde/zad/operations-manager:latest
```

**De handtekening.** Hij moet van een publicerende workflow in deze repo komen, op main, en niet van een willekeurige andere tekenaar:

```bash
IDENTITEIT='^https://github\.com/RijksICTGilde/RIG-Cluster/\.github/workflows/docker(-images)?\.yml@refs/heads/main$'

cosign verify "$IMAGE" \
  --certificate-identity-regexp "$IDENTITEIT" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

Die regexp noemt de twee workflows die publiceren en ankert op `refs/heads/main`. Alleen het repo-voorvoegsel zou te ruim zijn: dan accepteert de toets elke workflow en elke ref in deze repo, dus ook een `refs/pull/N/merge`. De workflows zelf houden dat tegen (tekenen zit in een job die alleen op main draait), en deze regexp is de tweede helft van dezelfde afspraak, voor als iemand die rechten ooit verplaatst.

**De provenance.** Welke workflow, welke commit en welke runner de image gebouwd hebben, met dezelfde begrenzing:

```bash
gh attestation verify "oci://$IMAGE" --owner RijksICTGilde --cert-identity-regex "$IDENTITEIT"
```

**De SBOM.** Buildx hangt hem als in-toto-attestatie aan de image-index, dus hij komt uit `imagetools inspect` en niet uit `cosign download sbom`, dat alleen het oudere cosign-formaat leest:

```bash
docker buildx imagetools inspect "$IMAGE" --format '{{ json .SBOM }}' | jq '.SPDX.packages | length'
```

Een getal groter dan nul betekent dat de SBOM er staat en gevuld is.

### Wat met de hand bij de git-host staat

Deze drie zijn instellingen en geen code, dus ze staan niet in deze repo en moeten gezet zijn om het bovenstaande te laten werken:

- **branch protection op main**, met de buildpoort als required check. Zonder die eis kan een auto-merge langs een rode poort.
- **auto-merge aan** voor de repo, anders blijft laag B op een open PR wachten.
- **geen force-push op main**, zodat de commit waar een attestatie naar wijst blijft bestaan.

## Versies

Alleen de laatst uitgerolde versie (`main` naar productie) krijgt beveiligingsfixes.
