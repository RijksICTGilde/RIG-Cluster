# Deel H: supply chain, build en ontwikkelproces

Onderzoeker: hoofdsessie (het deelonderzoek kon niet als aparte onderzoeker starten). Datum: 2026-09-26. Alleen gelezen; `gh` is read-only gebruikt voor branch protection en de alerts van GitHub.

## Onderzocht
- Dockerfiles: `operations-manager/Dockerfile`, `operations-manager/backup-image/Dockerfile`, `images/cmp-kustomize-sops/Dockerfile`, `images/postgresql-with-dictionaries/Dockerfile`, `operations-manager/docker-entrypoint.sh`, `.dockerignore` (repo-root; `operations-manager/.dockerignore` wordt volgens het commentaar niet gelezen).
- Dependencies: `operations-manager/python/pyproject.toml` (dependencies, ruff-config, liccheck), `keycloak-migration/relay-email-sender/pom.xml`, de ingecheckte jar.
- CI: `.github/workflows/ci.yml`, `security.yml`, `docker.yml`, `docker-images.yml`, `integration-tests.yaml`, `.pre-commit-config.yaml`, `renovate.json`, `scripts/scan-secrets.py`.
- Uitrol: alle `image:`-regels onder `infrastructure/` en `bootstrap/`, `images/argocd-rig/README.md`, `features/image-version-audit.md`, `features/image-registries.md`.
- Proces: GitHub branch protection en repo-instellingen via `gh api`, open Dependabot- en code-scanning-alerts, merged PR's met reviewaantal, `docs/generale-doorloop-augustus-2026.md`, `docs/productiebestanden-naar-een-sandbox.md`, `docs/post-mortems/`, `docs/zad-100-statements.md`, `Taskfile.yaml` (productie-rakende taken), `security/` (alleen bestandsnamen en rechten), `instructions/services.md`.
- Niet gedaan: geen build, geen `uv sync`, geen inhoud van sleutelbestanden, geen Forgejo-instellingen (die staan in deel F), geen toets van individuele CVE's op uitbuitbaarheid.

## Keten zoals hij in de code staat
Bron: GitHub `RijksICTGilde/RIG-Cluster` (publiek) is de bron voor CI en images; een interne Forgejo is de bouwbron voor de orchestrator en de GitOps-repos (deel F). Build: `docker.yml` bouwt bij elke push naar main en elke PR de OPI-image en pusht naar `ghcr.io/rijksictgilde/zad/operations-manager` (CalVer-tag plus `latest` op main, `pr-N-sha` op PR's). Ondersteunende images gaan via handmatige `workflow_dispatch`. Registry naar cluster: op ODCN via de Quay-proxy `rcr.rijksapps.nl/ghcr-rig/...`, gepind op een CalVer-tag in `bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production/patches/deployment.yaml` (de pin is de deploybeslissing, `task pin-odcn-image` in `Taskfile.yaml:1307`). Controlepunten die er zijn: pre-commit (ruff, pyright, djlint, secret-scan), CI (tests, coverage-ondergrens 21%, licentiecheck, reproduceerbare jar), security.yml (pip-audit strict via OSV, secret-scan op de branch-tree, Trivy op vier images naar SARIF, SBOM als artefact op main), GitHub secret scanning met push protection, CodeQL default setup (niet in de workflows, wel actief: er staan CodeQL-alerts). Controlepunten die ontbreken: geen signing of provenance van images, geen digest-pinning, geen verplichte review, geen triage van scanresultaten.

## Sterke plekken
1. **Secret-scan op drie niveaus**: pre-commit (`.pre-commit-config.yaml:52-56`), CI (`security.yml:53`), en een `--history`-modus die elke blob ooit scant (`scripts/scan-secrets.py:6`); een AGE-kandidaat telt pas als `age-keygen` hem accepteert, dus weinig ruis. Daarbovenop GitHub secret scanning met push protection (repo-instelling `secret_scanning_push_protection: enabled`).
2. **pip-audit strikt en zonder uitzonderingen**: `uv run pip-audit --strict --desc --skip-editable -s osv` (`security.yml:35`), geen `--ignore-vuln`. Wekelijkse run vangt nieuwe CVE's op bestaande code.
3. **Reproduceerbare jar**: CI bouwt de Keycloak-provider opnieuw en vergelijkt byte voor byte met de ingecheckte jar (`ci.yml:80-92`, `project.build.outputTimestamp`). Dat is precies de SLSA-gedachte "wat draait is wat er staat".
4. **Images draaien non-root**: OPI uid 1001 met `tini` als PID 1 (`operations-manager/Dockerfile`), backup-image uid 1001, CMP-sidecar uid 999, PostgreSQL uid 26. Multi-stage builds, test- en devgroepen niet in de image.
5. **`.dockerignore` sluit `security/`, `*.key`, `.env.secrets`, `bootstrap/` en `infrastructure/` uit** van de buildcontext, met uitleg waarom `**/` nodig is. Een sleutel kan dus niet per ongeluk in een laag komen.
6. **Beveiligingsguards als tests**: `test_plaintext_secret_guard.py` (ontsleutelde waarde mag op geen enkel schrijfpad naar git), `test_image_layer_guard.py`, `test_template_injection_sweep.py`, `test_pgdump_command_injection.py`, `test_csrf_enforcement.py`, `test_security_headers.py`, `test_secret_key_failclosed.py`, `test_user_token_auth.py`, `test_tenant_isolation_namespace.py`, `test_tenant_baseline_netpol.py`, `test_keycloak_email_verified.py`, `test_logging_redact.py`, `test_rate_limiter.py`, `test_argo_repository_no_hardcoded_key.py`, `test_secret_scan.py`. De post-mortem van 2026-07-20 heeft dus een blijvende regressietest opgeleverd.
7. **Ruff met de S-regels (bandit-equivalent) aan** in `pyproject.toml` `select`, plus `B`, `TRY`, `LOG`, `DTZ`.
8. **Licentiecontrole op PARANOID-niveau** (`[tool.liccheck] level = "PARANOID"`) in CI.
9. **Dependencies met een reden bij de pin**: `pyproject.toml` noemt per aangescherpte versie de CVE (authlib, python-multipart, mako, gitpython); `lord-of-the-components` is op commit gepind met de reden erbij.
10. **Eigen ArgoCD-image is verifieerbaar**: `images/argocd-rig` bouwt uit upstream v3.5.1 plus zeven genummerde patches en `task src:verify` bewijst dat de bouwboom precies dat is.
11. **Branch protection heeft zes verplichte status checks** (pre-commit, test, Integration Tests Mock en Kind, Lint & Type Check, license-check) met `strict: true`.
12. **Releaseproces met bewijs**: de generale doorlopen (`docs/generale-doorloop-*.md`, `docs/doorloop-rc*.md`) leggen per release vast op welke commit gemeten is, wat faalde en wat het oordeel was. De CalVer-pin in de odcn-overlay maakt de deploybeslissing een git-commit.

## Bevindingen

### H-1 Main is te mergen zonder review, met force-push en zonder handtekeningen
- **Ernst**: Hoog. **Zekerheid**: BEVESTIGD (repo-instelling opgevraagd).
- **BIO2**: 8.32 wijzigingsbeheer (8.32.01 goedkeuringsprocedure), 8.04 toegang tot broncode, 8.25 beveiligde ontwikkelcyclus, 5.03 functiescheiding.
- **Beschrijving**: `gh api repos/RijksICTGilde/RIG-Cluster/branches/main/protection` geeft `required_approving_review_count: 0`, `allow_force_pushes: true`, `enforce_admins: false`, `required_signatures: false`, `require_code_owner_reviews: false`, en een `bypass_pull_request_allowances` voor één gebruiker. Van de vijf laatst gemergede PR's hebben drie nul reviews (PR 168, 166, 163; PR 164 en 165 wel). Er is geen `CODEOWNERS`, geen PR-template.
- **Bewijs**: uitvoer `gh api .../branches/main/protection` (2026-09-26); `ls .github` toont alleen `workflows`.
- **Aanvalsscenario**: één gecompromitteerd GitHub-account (of één sessie-token op een laptop) volstaat om code op main te krijgen die vervolgens door `docker.yml` automatisch naar `ghcr.io` gaat met een CalVer-tag en `latest`. Vier-ogen ontbreekt als technische rem; de status checks toetsen of de code werkt, niet of hij bedoeld is. Force-push maakt bovendien historie herschrijfbaar, wat het audit-spoor van 8.32 aantast.
- **Aanbeveling**: minimaal één verplichte review van iemand anders dan de auteur, `enforce_admins` aan, force-push uit, lineaire historie aan, `CODEOWNERS` voor de paden die de vertrouwensgrens dragen (`opi/middleware/`, `opi/api/user_token_auth.py`, `opi/utils/age.py`, `opi/utils/sops.py`, `manifests/`, `bootstrap/`, `.github/workflows/`) met `require_code_owner_reviews`. Overweeg verplichte commit-signing (SSH- of gitsign-handtekeningen). Referentiepraktijk: OpenSSF Scorecard "Branch-Protection" tier 3 en hoger; Logius en DICTU eisen twee-persoonsregel op productiecode; SLSA Build L3 veronderstelt dat de bron niet door één persoon ongezien te wijzigen is.

### H-2 Kwetsbaarheidsmeldingen worden gegenereerd maar niet afgehandeld
- **Ernst**: Hoog. **Zekerheid**: BEVESTIGD.
- **BIO2**: 8.08 technische kwetsbaarheden (8.08.01: bij hoge kans en hoge schade binnen een week; 8.08.02 en 8.08.03: expliciete risicoafweging), 8.16.
- **Beschrijving**: op GitHub staan 227 open Dependabot-alerts (oudste 2025-10-08) en 545 open code-scanning-alerts: Trivy 7 critical en 281 high, CodeQL 1 critical en 28 high. Voorbeelden: Starlette high CVE-2026-54283 en CVE-2026-48818 open sinds 2026-06-17 terwijl `pyproject.toml` `starlette==0.50.0` en `fastapi==0.135.1` hard pint; GitPython 3 high sinds 2026-07-28; Keycloak-services CVE-2026-9792 t/m 9803 (de provider-pom) sinds 2026-07-02; cryptography CVE-2026-69248. Trivy draait wel maar faalt de build niet (alleen SARIF-upload, `ignore-unfixed: true`, alleen HIGH en CRITICAL), en Dependabot security updates staan uit (`dependabot_security_updates: disabled`). CodeQL-bevindingen in `opi/` zijn bij steekproef vals-positief (`kopia.py:276` logt een geredigeerd commando, `sops.py:142` logt stderr en niet de sleutel), maar niemand heeft ze als zodanig gesloten; `images/e2e-allservices/mail.go:92` (go/email-injection, critical) is niet beoordeeld.
- **Bewijs**: `gh api .../dependabot/alerts?state=open` en `.../code-scanning/alerts?state=open` (2026-09-26); `security.yml:95-108`; `pyproject.toml` regels met `starlette==0.50.0`.
- **Gevolg**: de maatregel 8.08.01 eist een reactietijd van een week bij hoog risico. Zonder triage is niet aantoonbaar of een open high echt geen risico is of gewoon niet bekeken. pip-audit in CI is strikt, maar Starlette staat kennelijk niet in de OSV-feed als kwetsbaar op 0.50.0 of de pin houdt hem tegen; dat verschil tussen twee bronnen is precies waar een triageproces voor is.
- **Aanbeveling**: (1) een wekelijkse triage met een eigenaar: elke alert krijgt binnen een week "fix", "niet van toepassing met reden" of "geaccepteerd tot datum"; sluit vals-positieven in GitHub met een reden zodat de teller betekenis krijgt. (2) Laat Trivy en pip-audit de PR blokkeren op CRITICAL en HIGH met een `.trivyignore` met vervaldatum per uitzondering. (3) Zet Dependabot security updates aan of zorg dat Renovate echt draait (zie H-11). (4) Leg de reactietijden uit 8.08 vast in `SECURITY.md`. Referentiepraktijk: NCSC "Beheer van kwetsbaarheden" en de standaard SLA's van Rijks-CIO's (critical 48 uur, high 7 dagen); teams met een hoog beveiligingsniveau houden het aantal open high-alerts structureel op nul en zien een niet-nul-stand als een incident, niet als een achterstand.

### H-3 Geen enkele image is op digest gepind; productie draait onderdelen op `latest`
- **Ernst**: Midden. **Zekerheid**: BEVESTIGD.
- **BIO2**: 8.19 installeren van software, 8.09 configuratiebeheer, 5.21 toeleveringsketen.
- **Beschrijving**: `grep -rnE 'image:.*@sha256' infrastructure bootstrap` levert 0 treffers. In de productie-overlay staat de SOPS-CMP-sidecar op `rig-cmp-argo-kustomize-sops:latest` (`bootstrap/rig-system/kustomize/overlays/odcn-production/argocd-deployment.yaml:132`), en dat is de container die de AGE-sleutel in handen heeft. Verder `jpillora/chisel:latest` (`infrastructure/bootstrap/infrastructure/chisel/controller/base/deployment.yaml:30`), Forgejo op de major-tag `forgejo:14` en `forgejo:14-rootless`, en `imagePullPolicy: Always` op vijf plekken. De OPI-image zelf is op CalVer-tag gepind (goed), maar een tag is herschrijfbaar in de registry.
- **Bewijs**: de grep-uitvoer hierboven; `images/argocd-rig/README.md` noemt zelf dat de Red Hat-build in productie op digest staat (`argocd-rhel9@sha256:5058a825...`), dus het patroon is bekend.
- **Aanvalsscenario**: wie de tag in `ghcr.io/minbzk/base-images` of in de rcr-proxy kan overschrijven (een gecompromitteerd bouwaccount, of de proxy-cache die een andere upstream-laag ophaalt), krijgt bij de volgende pod-herstart zijn code in de repo-server van ArgoCD, mét de SOPS-sleutel. Geen commit in deze repo is daarvoor nodig, dus GitOps ziet niets.
- **Aanbeveling**: pin alle platformimages op `tag@sha256:...` en laat Renovate de digest bijhouden (Renovate doet dat standaard als `pinDigests: true` aanstaat). Begin bij de sidecar met de sleutel en bij alles wat cluster-admin-achtige rechten heeft (ArgoCD, operator, cert-manager, ingress). Referentiepraktijk: Kubernetes-hardening van NSA/CISA en de CIS Benchmark eisen immutable references; Sigstore-policy-controllers (of Kyverno `verifyImages`) weigeren ongesigneerde of niet-gepinde images op admission, wat op OpenShift/ODCN met de bestaande admission-rewrite goed samengaat.

### H-4 Binaries in de images worden gedownload zonder checksum of handtekening
- **Ernst**: Midden. **Zekerheid**: BEVESTIGD.
- **BIO2**: 8.19, 5.21, 8.07.
- **Beschrijving**: `operations-manager/Dockerfile` haalt sops, kopia en chisel met `curl -LO` op versie op, zonder `sha256sum -c` en zonder `cosign verify-blob` (sops en kopia publiceren allebei checksums en Sigstore-handtekeningen). `images/cmp-kustomize-sops/Dockerfile` doet hetzelfde voor kustomize, sops, helm, helmfile, helm-diff en ksops, en haalt bovendien `kubectl` op de versie uit `stable.txt` (elke build een andere) en `yq` op `latest/download`. `images/postgresql-with-dictionaries/Dockerfile` cloont pgvector op een tag (`--branch v0.7.4`), en een tag is verplaatsbaar. `kubectl` in de OPI-image komt wel via een gesigneerde apt-repo (`pkgs.k8s.io`, goed).
- **Bewijs**: `operations-manager/Dockerfile` regels `curl -LO "https://github.com/getsops/sops/releases/download/..."`, `images/cmp-kustomize-sops/Dockerfile` regels `curl -Lo kubectl "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)..."` en `curl -Lo yq ".../releases/latest/download/..."`.
- **Aanvalsscenario**: een MITM op de bouwrunner is onwaarschijnlijk, maar een gecompromitteerde GitHub-release van een van deze projecten (het scenario van de xz- en Codecov-incidenten) landt ongemerkt in de image die de SOPS-sleutel bedient. De build is bovendien niet reproduceerbaar: dezelfde Dockerfile geeft op twee dagen twee verschillende kubectl- en yq-versies, dus de SBOM van vandaag zegt niets over de image van gisteren.
- **Aanbeveling**: pin elke download op versie én sha256 (een `ARG SOPS_SHA256=` naast `ARG SOPS_VERSION=` en `echo "$SHA  file" | sha256sum -c`), en verifieer waar het kan met `cosign verify-blob` (sops, kopia, kustomize, helm publiceren dat). Vervang `stable.txt` en `latest` door vaste versies; clone pgvector op een commit-hash. Referentiepraktijk: SLSA "Build L2" veronderstelt verifieerbare, gepinde inputs; de Google/Chainguard-images en de NCSC-richtlijn voor containers noemen checksum-verificatie van gedownloade tooling als basiseis.

### H-5 Images worden ongesigneerd en zonder provenance gepubliceerd; het workflow-token kan naar main schrijven
- **Ernst**: Midden. **Zekerheid**: BEVESTIGD.
- **BIO2**: 5.21, 8.32, 8.04.
- **Beschrijving**: alle `docker/build-push-action`-stappen hebben `provenance: false` en `sbom: false` (`docker.yml:77-78`, `docker-images.yml:72-73, 106-107, 140-141`). Er is geen cosign-signing of GitHub-attestation. De SBOM uit `security.yml` wordt als workflow-artefact bewaard, niet aan de image gehangen of gepubliceerd. `docker.yml` heeft `permissions: contents: write` (voor de CalVer-git-tag) en `packages: write`, en draait op `pull_request` met `push: true`: een PR uit de eigen repo pusht dus al een `pr-N-sha`-image naar ghcr met een token dat ook naar de repo kan schrijven. De actions zijn op mutable tags gepind (`actions/checkout@v4`/`@v6`, `docker/build-push-action@v6`); alleen `trivy-action` staat op een SHA.
- **Bewijs**: de genoemde regels; `security.yml:138-141` (upload-artifact van de SBOM).
- **Aanvalsscenario**: wie een PR kan openen en de status checks groen krijgt, publiceert al een image onder de officiële naam; met `contents: write` in dezelfde job kan een gecompromitteerde action (mutable tag) tags of branches aanmaken. Zonder handtekening kan het cluster een image van de officiële naam niet onderscheiden van een vervangen image.
- **Aanbeveling**: `provenance: mode=max` en `sbom: true` aanzetten (kost niets), images signeren met cosign keyless via de GitHub OIDC-identiteit en op het cluster verifiëren (Kyverno of Sigstore policy-controller), actions op SHA pinnen (Renovate houdt ze bij), het `contents: write` in een aparte job met alleen die stap zetten, en op `pull_request` bouwen met `push: false` (`load: true` volstaat voor de scan). Referentiepraktijk: SLSA Build L3 via de GitHub "generator"-workflows, OpenSSF Scorecard "Token-Permissions" en "Pinned-Dependencies", de Rijksbrede richtlijn dat productie-images herleidbaar zijn tot een gereviewde commit.

### H-6 Security-linting is verzwakt waar het het meest telt
- **Ernst**: Midden. **Zekerheid**: BEVESTIGD.
- **BIO2**: 8.28 veilig coderen, 8.29 testen van beveiliging.
- **Beschrijving**: in `pyproject.toml` staan voor `opi/**.py` de regels `S105`, `S106` (hardcoded wachtwoorden), `S603`, `S607` (subprocess), `S608` (SQL-injectie) uit, en `S701` (Jinja2 zonder autoescape) staat globaal uit. Het commentaar zegt "pre-existing issues to be fixed separately". Dat betekent dat precies de klassen die deel E onderzoekt (command injection, SQL-injectie, template-injectie) door de linter niet meer gemeld worden voor nieuwe code. De coverage-ondergrens in CI is 21% (`ci.yml:57`). Er is geen semgrep of bandit met een eigen regelset; CodeQL draait via de default setup maar zijn resultaten worden niet verwerkt (H-2).
- **Bewijs**: `pyproject.toml` `[tool.ruff.lint] ignore` en `per-file-ignores` (`"opi/**.py" = ["S105", "S106", "S603", "S607", "S608", "C901"]`), `"S701", # Jinja2 autoescape`.
- **Gevolg**: een nieuwe f-string in een SQL-statement of een nieuwe `subprocess.run(..., shell=True)` komt zonder waarschuwing door pre-commit en CI.
- **Aanbeveling**: zet de S-regels per bestand terug aan met `# noqa: S608  # reden` op de bestaande, beoordeelde plekken (dat is een eenmalige klus van een dagdeel met `ruff check --add-noqa`), zodat nieuwe overtredingen wel falen. Voeg semgrep toe met de `p/python` en `p/fastapi` regelsets plus eigen regels voor de projecteigen sinks (kubectl-connector, manifest-rendering). Verhoog de coverage-ondergrens per map: `opi/middleware`, `opi/api/user_token_auth.py`, `opi/services/project_authorization.py` en `opi/utils/{age,sops,api_keys}.py` verdienen 90% of meer. Referentiepraktijk: OWASP SAMM "Security Testing" niveau 2 (SAST blokkeert de merge), en de praktijk bij DigiD/Logius om per beveiligingskritische module een eigen dekkingsdrempel te hanteren.

### H-7 De productiesleutel en PAT's staan op ontwikkelaarslaptops, zonder break-glass en zonder logging
- **Ernst**: Midden. **Zekerheid**: BEVESTIGD (bestandsnamen en rechten; inhoud niet gelezen).
- **BIO2**: 8.02 speciale toegangsrechten (8.02.01 kwartaalbeoordeling), 8.18 speciale systeemhulpmiddelen (8.18.02 gebruik wordt gelogd), 8.24 cryptografie (sleutelbeheer), 5.17.
- **Beschrijving**: `security/` bevat lokaal `key.txt` (platformsleutel productie), `old_key.txt`, `original_zad_key.txt`, `sandbox-key.txt`, `sandbox-key-old.txt`, `developer-key.txt`, `pat_current.txt`, `pat_new.txt`, `vlam.txt`. Drie van de sleutelbestanden en beide PAT-bestanden staan op `0644` (wereldleesbaar op de laptop). `Taskfile.yaml:364` ontsleutelt productiebestanden met `SOPS_AGE_KEY="$(sed -n '3p' security/key.txt)"`. `docs/sops-en-age-met-de-hand.md:17` zegt zelf dat de sleutel "in een vault-achtige voorziening en niet in een map op een laptop" hoort. Er is geen break-glass-procedure, geen registratie wie de sleutel heeft, en gebruik van de sleutel buiten het cluster wordt nergens gelogd. Oude sleutels blijven bewaard naast de nieuwe.
- **Bewijs**: `ls -la security/` (2026-09-26), `Taskfile.yaml:364`, `docs/sops-en-age-met-de-hand.md:17,83-84`.
- **Aanvalsscenario**: één verloren of gecompromitteerde laptop levert de sleutel die elk secret in `zad-deployments` en elk projectbestand in `zad-projects` ontsleutelt, plus een PAT met schrijfrecht op de GitOps-repos (deel D en F werken dat uit). Omdat het gebruik niet wordt gelogd, is achteraf niet vast te stellen of dat gebeurd is.
- **Aanbeveling**: (1) de platformsleutel alleen in het cluster en in een gescheiden kluis (bijvoorbeeld de al aanwezige `vault`-component, of een hardware-token per beheerder met `age-plugin-yubikey`, wat AGE ondersteunt), en per beheerder een eigen recipient in `.sops.yaml` zodat intrekken per persoon kan; (2) oude sleutels na rotatie vernietigen, niet bewaren; (3) `chmod 600` afdwingen via een pre-flight in de Taskfile; (4) een register van wie welke sleutel heeft en een kwartaalcontrole (8.02.01); (5) PAT's met korte levensduur en minimale scope, of liever een deploy-key per repo. Referentiepraktijk: de Rijksoverheid-brede praktijk van "geen productiesecrets op werkplekken" (BIO 8.01 zero footprint), en de manier waarop teams met een hoog niveau SOPS inzetten: per persoon een YubiKey-recipient en de clustersleutel alleen in KMS of een HSM-achtige voorziening.

### H-8 Sandbox en productie zijn gekoppeld via Keycloak en via productiebestanden
- **Ernst**: Midden. **Zekerheid**: BEVESTIGD.
- **BIO2**: 8.31 scheiden van ontwikkel-, test- en productieomgevingen, 8.33 testgegevens, 5.34 privacy.
- **Beschrijving**: `task sandbox:configure-sso` laat een sandbox de productie-Keycloak (`keycloak.rijksapp.nl`, realm `rig-platform`) gebruiken via een client `development-clusters` met een gedeeld client-secret dat de ontwikkelaar handmatig overtypt (`Taskfile.yaml:2831-2862`). `task sandbox:import-project` haalt de 47 productieprojectbestanden naar een sandbox, herversleutelt de secrets met de sandboxsleutel en pusht ze naar de sandbox-Forgejo (`docs/productiebestanden-naar-een-sandbox.md`, `Taskfile.yaml:3178-3248`). Die bestanden bevatten de echte gebruikerslijsten (e-mailadressen) en de echte configuratie van productie.
- **Bewijs**: de genoemde regels en het document.
- **Gevolg**: een sandbox op een dev-server (`docs/sandbox-on-dev-server.md`) met productie-SSO en productieprojectbestanden is functioneel een tweede productie met een lager beveiligingsniveau. Een gecompromitteerde sandbox levert een geldige OIDC-client in de productie-realm en de projectconfiguratie van alle afnemers. De koppeling is bewust (echte SSO-Rijk-flows testen), maar staat niet als risicoafweging vastgelegd zoals `features/bio-network-access-no-vpn-compliance.md` dat wel doet voor de VPN-keuze.
- **Aanbeveling**: een aparte Keycloak-realm of liever een aparte Keycloak-instantie voor niet-productie, gekoppeld aan de acceptatieomgeving van SSO-Rijk; bij import naar een sandbox de gebruikerslijst vervangen door testaccounts en secrets opnieuw genereren in plaats van herversleutelen; en de resterende koppeling vastleggen als risicoafweging met eigenaar en einddatum. Referentiepraktijk: DigiD en Logius scheiden OTAP tot op de identity provider (preprod-SSO), en de BIO-thema-uitwerking Applicatieontwikkeling vraagt gescheiden testgegevens die geen productie-persoonsgegevens bevatten tenzij geanonimiseerd.

### H-9 Geen SECURITY.md, geen CODEOWNERS, geen threat model
- **Ernst**: Laag. **Zekerheid**: BEVESTIGD.
- **BIO2**: 5.24 incidentbeheer, 6.08 melden van gebeurtenissen, 8.27 veilige architectuur (security by design gedocumenteerd).
- **Beschrijving**: er is geen `SECURITY.md` in de repo (ook niet op GitHub via de API), terwijl het Wies-pentestvoorstel ernaar verwijst. `/.well-known/security.txt` verwijst door naar het centrale NCSC-bestand (`opi/server.py:715-720`), wat conform de Rijksrichtlijn is maar een melder niet bij dit team brengt. Er is geen threat model of dataflow-diagram met vertrouwensgrenzen; `architecture/` bevat een systeemoverzicht zonder beveiligingsperspectief. Het kaderdocument `features/bio-network-access-no-vpn-compliance.md` laat zien dat het team dit soort notities kan schrijven; het is er alleen voor één onderwerp.
- **Aanbeveling**: een `SECURITY.md` met meldweg, reactietijden (uit 8.08) en scope; `CODEOWNERS` (zie H-1); een threat model van twee pagina's op basis van de blast-radius-analyse uit deel C, jaarlijks herzien. Referentiepraktijk: OpenSSF Scorecard "Security-Policy", en de NCSC-CVD-leidraad die een projecteigen contactpunt naast het centrale NCSC-adres aanbeveelt.

### H-10 Eigen ArgoCD-fork en een CMP-sidecar buiten de scanmatrix
- **Ernst**: Laag. **Zekerheid**: BEVESTIGD.
- **BIO2**: 8.08, 8.19.
- **Beschrijving**: productie draait `argocd-rig:v3.5.1-rig2`, een eigen build van upstream v3.5.1 met zeven patches waarvan twee uit nog niet gemergede upstream-PR's (`images/argocd-rig/README.md`). De CMP-sidecar is gebaseerd op `quay.io/argoproj/argocd:v2.14.21` (`images/cmp-kustomize-sops/Dockerfile`), een andere major dan de server. `security.yml` scant vier images; `argocd-rig` zit niet in de matrix. Een fork legt de patchplicht bij het team: elke upstream-securityrelease van ArgoCD vraagt een rebase, een build en een uitrol.
- **Aanbeveling**: `argocd-rig` in de Trivy-matrix; een vaste afspraak dat een ArgoCD-securityrelease binnen de 8.08-termijn gerebased wordt; de sidecar op dezelfde ArgoCD-basis als de server, of op een minimale basis zonder ArgoCD (de sidecar heeft alleen `argocd-cmp-server` nodig, dat via de gedeelde volume uit de hoofdimage komt). Referentiepraktijk: Red Hat en SUSE houden forks uitsluitend met een geautomatiseerde rebase-pipeline en een expliciete lijst van afwijkingen; `images/argocd-rig/patches/` is daar al de goede vorm voor.

### H-11 Renovate is geconfigureerd maar lijkt niet te draaien
- **Ernst**: Info. **Zekerheid**: AANNEMELIJK.
- **BIO2**: 8.08.
- **Beschrijving**: `renovate.json` bestaat en `features/security-scanning-pipeline.md` noemt Renovate als vervanging van Dependabot, maar `gh pr list --author app/renovate --state all` geeft geen enkele PR, en Dependabot security updates staan uit. Als de Renovate-app niet op de organisatie is geïnstalleerd, is er nu geen enkele geautomatiseerde update-stroom, en blijft de update-arbeid handwerk (wat de open alerts in H-2 verklaart).
- **Aanbeveling**: controleer de installatie van de Renovate-app op `RijksICTGilde`; zo niet, zet Dependabot aan als tussenoplossing. Voeg `pinDigests` en de `github-actions`-manager toe zodat H-3 en H-5 vanzelf bijgehouden worden.

### H-12 Post-mortems zonder vaste vorm en zonder maatregelenregister
- **Ernst**: Info. **Zekerheid**: BEVESTIGD.
- **BIO2**: 5.27 leren van incidenten, 5.26.
- **Beschrijving**: `docs/post-mortems/` bevat drie documenten in drie vormen; het impersonatie-incident noemt zelf "nog uit te lijnen op het interne post-mortem-sjabloon". De lessen zijn deels in tests geland (`test_keycloak_email_verified.py`), maar er is geen register dat per incident de maatregel, de eigenaar en de status bijhoudt.
- **Aanbeveling**: één sjabloon (tijdlijn, oorzaak, impact, maatregelen met eigenaar en datum), en een tabel in `docs/post-mortems/README.md` met de status van elke maatregel. Referentiepraktijk: de blameless post-mortem uit het Google SRE-boek, en de BIO-eis dat incidenten aantoonbaar tot verbetering leiden.

### H-13 De productie-image wordt op een laptop gebouwd en gepubliceerd, buiten CI om
- **Ernst**: Hoog. **Zekerheid**: BEVESTIGD.
- **BIO2**: 8.32 wijzigingsbeheer, 8.19 installeren van software, 5.21 toeleveringsketen, 8.31 (de bouwomgeving is een ontwikkelomgeving).
- **Beschrijving**: de image die productie draait komt niet uit `docker.yml`. `task publish-operations-manager` (`Taskfile.yaml:1268-1300`) bouwt lokaal en pusht naar `ghcr.io/minbzk/base-images/operations-manager` met een tag `YYYY.MM.DD.HHMM-<sha>[-dirty]`; CI bouwt en scant een andere image (`ghcr.io/rijksictgilde/zad/operations-manager`). De productie-overlay wijst naar de laptop-image: `2026.09.02.2241-d81cdab4-dirty` (`overlays/odcn-production/patches/deployment.yaml:12`, deploy-commit 14a21061f "Release" van 2026-09-07), en de deploy ervoor was ook `-dirty` (c6552ccbf, 2026-08-29). `-dirty` betekent per definitie dat de werkboom niet-gecommitte wijzigingen bevatte; de Taskfile waarschuwt daar zelf voor ("does not describe what is being published") maar gaat door.
- **Bewijs**: `Taskfile.yaml:1259-1263` (`DIRTY="-dirty"`), `Taskfile.yaml:1289` (`REGISTRY_IMAGE: ghcr.io/minbzk/base-images/operations-manager`), `docker.yml:20` (`IMAGE_NAME: ghcr.io/rijksictgilde/zad/operations-manager`), `git log -- bootstrap/.../odcn-production/patches/deployment.yaml`.
- **Aanvalsscenario en gevolg**: alle controlepunten uit de keten (branch protection, tests, pip-audit, Trivy, secret-scan, SBOM) toetsen een image die niet in productie draait. De productie-image is niet herleidbaar tot een commit, dus niet reproduceerbaar, niet te reviewen en niet achteraf te verifiëren. Een gecompromitteerde ontwikkelaarslaptop (zie H-7: daar liggen ook de sleutels) levert rechtstreeks code in de productie-OPI, zonder dat er een commit in git verschijnt. Dit is de klassieke omweg om GitOps heen.
- **Aanbeveling**: productie mag alleen images uit CI draaien: één build-pijplijn, getriggerd door een gemergede commit op main, die de CalVer-tag zet, provenance en SBOM meegeeft en signeert (H-5). De Taskfile-taak weigert bij `-dirty` in plaats van te waarschuwen, en publiceert niet naar het productiepad. Het cluster verifieert de handtekening op admission, zodat een laptop-image technisch niet meer kan draaien. Referentiepraktijk: SLSA Build L2 en hoger eist een gehoste, niet door de ontwikkelaar beïnvloedbare build; dit is bij Logius, DICTU en de SSC-ICT-platforms een harde eis voor productie.

## Tabel: images in de uitrol (selectie, productie-relevant)

| Image | Waar | Pin | Registry | In CI gescand |
|---|---|---|---|---|
| operations-manager `2026.09.02.2241-d81cdab4-dirty` | odcn-production patches/deployment.yaml | CalVer-tag (met `-dirty`-suffix: gebouwd uit een niet-schone werkboom) | rcr.rijksapps.nl/ghcr-rig | ja (Trivy) |
| rig-cmp-argo-kustomize-sops `latest` | odcn-production/argocd-deployment.yaml:132 | geen | lokaal/ghcr | ja (Trivy) |
| argocd-rig `v3.5.1-rig2` | odcn-production/argocd-deployment.yaml:17 | tag | rcr.rijksapps.nl/ghcr-rig | nee |
| argocd-operator `v0.14.0` | bootstrap/crd/operator | tag | quay.io | nee |
| keycloak `25.0.6` | keycloak/controller/base | tag | quay.io | nee |
| forgejo `14` / `14-rootless` | forgejo | major-tag | codeberg.org | nee |
| minio `RELEASE.2025-07-23` | minio, backup-destination | tag | quay.io (eigen build sinds 187f8711f) | nee |
| chisel `latest` | chisel/controller/base | geen | docker.io | nee |
| external-dns `v0.15.0` | external-dns | tag | rcr.rijksapps.nl/k8s-rig | nee |
| kube-state-metrics `v2.13.0`, prometheus `v2.54.1` | prometheus | tag | registry.k8s.io, docker.io | nee |
| pgadmin4 `9.8.0`, mailpit, stalwart `v0.11.8`, redis `7-alpine`, registry `2` | overlays | tag | docker.io | nee |
| rig-backup, postgresql-with-dictionaries | tenant-namespaces via manifests | tag | ghcr.io/minbzk/base-images | ja (Trivy) |

Geen enkele regel gebruikt een digest. De `-dirty`-suffix op de productie-OPI-tag betekent dat de draaiende image niet een-op-een tot een commit herleidbaar is; dat verdient een aparte controle in deel F of bij de volgende release.

## Tabel: beveiligingstests die bestaan

| Test | Dekt af |
|---|---|
| test_plaintext_secret_guard.py | ontsleutelde waarde mag op geen schrijfpad naar git |
| test_template_injection_sweep.py, test_authwall_banner_yaml_injection.py, test_progress_fragment_injection.py | YAML/HTML-injectie via templates |
| test_pgdump_command_injection.py, test_kubectl_command_summary.py | command-injectie in subprocess-aanroepen |
| test_csrf_enforcement.py, test_template_fetch_csrf.py | CSRF-afdwinging |
| test_security_headers.py | HSTS, CSP, frame-ancestors |
| test_secret_key_failclosed.py, test_session_cookie_max_age.py, test_wizard_session_secrets.py | sessie-secret en cookie |
| test_user_token_auth.py, test_openapi_security_schemes.py, test_endpoint_util.py | bearer- en API-key-authenticatie |
| test_authorization.py, test_project_authorization.py, test_admin_check.py, test_admin_diensten_toegang.py, test_schema_permissions.py, test_table_permissions.py, test_live_status_permission_denied.py | autorisatie |
| test_keycloak_email_verified.py, test_keycloak_identity_lock.py, test_keycloak_role_gate_flow.py, test_keycloak_auto_link_flow.py, test_keycloak_restrict_access_rule.py, test_wizard_role_gate.py, test_keycloak_otp*.py | Keycloak-flows en de post-mortem-fix |
| test_tenant_isolation_namespace.py, test_tenant_isolation_registry.py, test_tenant_baseline_netpol.py, test_project_service_account.py, test_backup_pod_service_account.py | tenant-isolatie |
| test_age_encryption.py, test_secrets_utils.py, test_sops_*.py, test_key_rotation_*.py, test_attachment_crypto.py, test_set_sops_key_secret.py | cryptografie en rotatie |
| test_logging_redact.py | redactie in logs |
| test_rate_limiter.py | rate-limiting |
| test_secret_scan.py, test_argo_repository_no_hardcoded_key.py, test_image_layer_guard.py, test_build_guard.py, test_repository_pad_binnen_de_repo.py | guards op repo en build |
| test_domain_restrictions.py, test_domain_approval.py, test_cross_domain_access.py, test_upsert_domain_enforcement.py | domein-autorisatie |
| test_upload_staging.py, test_sanitize.py, test_naming_utils.py | invoervalidatie |

## Open vragen
- Draait Renovate op de organisatie (H-11)? Alleen een org-beheerder kan dat zien.
- Wat betekent de `-dirty`-suffix op de productie-image concreet: is er lokaal gebouwd en gepusht buiten CI om? `docs/sandbox-image-deploy-via-registry.md` en de recente commit e6c08b22c wijzen op een pad waarbij een ontwikkelaar zelf naar de registry pusht.
- Zijn de Trivy-critical-meldingen op `anyio 4.12.1` en `GitPython 3.1.50` inmiddels in `uv.lock` opgelost (uv.lock niet gelezen)?
- Hoe komt code van GitHub main op de Forgejo die de orchestrator gebruikt (`dclaude sync-main`), en geldt de branch protection daar ook? Dat raakt deel F.
