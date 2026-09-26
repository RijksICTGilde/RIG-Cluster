# Deel D: secretbeheer

Whitebox-toets op de code en configuratie in deze repository, 26 september 2026. Alleen gelezen: geen kubectl, geen git-wijzigingen, geen deploys. De map `security/` is alleen met `ls -la` bekeken. Nergens in dit rapport staat de inhoud van een sleutel of secret; waar een bestand een geheim bevat staat alleen het pad.

Toetskader: BIO2 v1.3, controls 8.24 (cryptografie), 5.17 (authenticatie-informatie), 8.12 (voorkomen van gegevenslekken), 8.10 (wissen van informatie), 8.04 (toegang tot broncode), en goede praktijk voor sleutelbeheer. Zekerheid per bevinding: BEVESTIGD (letterlijk in code of configuratie aangetroffen) of AANNEMELIJK (volgt uit de code, maar het sluitstuk staat buiten deze repo of is niet zonder cluster te toetsen).

## Onderzocht

Documentatie: `instructions/sops-sleutel-in-het-cluster.md`, `features/sops-sleutel-roteren.md`, `features/sops-skip-unchanged-reencryption.md`, `docs/sops-en-age-met-de-hand.md`, `docs/post-mortems/sops-architecture-mismatch.md`, `security/readme.md`, `features/project-opvragen-api.md`, `features/service-config-api.md`, `features/component-values-api.md`, `features/futures/kube-secrets-to-env.md`, `features/helmfile-single-app-deployment.md`, `features/argocd-render-error-surfacing.md`, `plans/de-blast-radius-van-een-sleutel.md`, `plans/de-sops-sleutel-vervangen-met-een-script.md`.

Plugin en bootstrap: `bootstrap/rig-system/kustomize/sops-plugin.sh`, `bootstrap/rig-system/kustomize/configmap-sops-plugin.yaml`, `images/cmp-kustomize-sops/Dockerfile`, `bootstrap/rig-system/kustomize/operations-manager/base/deployment.yaml`, `bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production/{configmap.yaml,operations-manager-env-secrets.yaml,_blueprint-cluster-role.yaml}`, `bootstrap/rig-system/kustomize/overlays/{odcn-production,sandboxed-local}/argocd-deployment.yaml`, `bootstrap/rig-system/kustomize/overlays/*/argocd-repo-*.sops.yaml` (alleen keys en metadata), `.gitignore`.

Code (`operations-manager/python/`): `opi/utils/age.py`, `opi/utils/sops.py`, `opi/handlers/sops.py`, `opi/utils/secrets.py`, `opi/utils/passwords.py`, `opi/utils/api_keys.py`, `opi/core/secret_key.py`, `opi/core/config.py`, `opi/utils/logging_redact.py`, `opi/utils/totp.py`, `opi/connectors/kubectl.py`, `opi/connectors/git.py`, `opi/manager/project_manager.py` (SOPS-secret, helmfile, API-key), `opi/manager/keycloak_manager.py`, `opi/services/project_store.py`, `opi/services/project_service.py`, `opi/handlers/project_file_handler.py`, `opi/utils/project_utils.py`, `opi/api/v2/project_read.py`, `opi/web/router.py`, `opi/templates_lotc/bg/project-tabs.html.j2`, `opi/forms/editables/generators.py`, `manifests/generic-secret.yaml.to-sops.jinja`, `manifests/binary-secret.yaml.to-sops.jinja`, `manifests/decrypt-sops.yaml.jinja`, `manifests/project-serviceaccount.yaml.jinja`, `manifests/deployment.yaml.jinja`.

Scripts: `scripts/key_rotation.py`, `scripts/rotate-sops-key.py`, `scripts/rotate-project-keys.py`, `scripts/sops_rotation.py`, `scripts/project_rotation.py`, `scripts/argo_rotation.py`, `scripts/sops_key_secret.py`, `scripts/set-sops-key-secret.py`, `scripts/replace-git-pat.py`, `scripts/project_decrypt.py`, `scripts/scan-secrets.py`, `scripts/secret_scan.py`, `operations-manager/python/scripts/extract_project_api_key.py`, `Taskfile.yaml` (secret-generatie en TLS-taken).

Secret-scanning: `.github/workflows/security.yml`, `.pre-commit-config.yaml`, `operations-manager/python/tests/test_secret_scan.py`, `docker-compose.dev.yaml`, `operations-manager/python/opi/configs/`, `operations-manager/python/tests/` (credential-achtige literalen), `operations-manager/python/.env*` (alleen keys en vorm).

Git: `git ls-files` en `git log` op bestandsnamen (nooit op inhoud) voor sleutelbestanden, `.to-sops.yaml`, `secrets-overview-*` en bekende tokenpatronen; `git log -S` op sleutelmarkers met validatie van kandidaten via `age-keygen -y` en `ssh-keygen -y` (alleen geldig/ongeldig genoteerd); vergelijking van de publieke helft met de `recipient:`-metadata van SOPS-bestanden op eerdere commits; `gh repo view` voor de zichtbaarheid van de GitHub-remote.

## Sleutelmodel zoals het in de code staat

Er is geen `.sops.yaml`-configuratie in de repo (`git ls-files` op die naam is leeg). Elke versleuteling noemt de recipient expliciet: `sops --encrypt --age <pubkey>` (`opi/utils/sops.py:208`) of `age --armor -r <pubkey>` (`opi/utils/age.py:98-102`). Er is dus ook geen `encrypted_regex`: SOPS versleutelt alle waarden in het bestand, ook `metadata.name`. Dat is strenger dan nodig en niet zwakker.

De hiërarchie heeft twee lagen. De platformsleutel versleutelt per project de eigen projectsleutel; de projectsleutel versleutelt de rest van dat project. Alles wat de platformsleutel direct opent staat in `features/sops-sleutel-roteren.md:13-29`; de code bevestigt het.

| sleutel | waar de private key staat | wat hij ontsluit |
|---|---|---|
| platformsleutel (productie) | k8s Secret `sops-age-key` in `rig-prd-operations` en een handmatige kopie in `rig-prd-ron` (`instructions/sops-sleutel-in-het-cluster.md:17-23`); in de OPI-pod als env `SOPS_AGE_KEY_CONTENT` (`deployment.yaml:117-121`); op de laptop van de beheerder als `security/key.txt` (gitignored) | alle SOPS-bestanden in `bootstrap/` en `infrastructure/` (ArgoCD-admin, Keycloak-admin, Postgres, Redis, MinIO, OPI-env-secrets met onder meer `KEYCLOAK_ADMIN_PASSWORD`, `MINIO_ADMIN_SECRET_KEY`, `SECRET_KEY`); de git-wachtwoorden van OPI in de prod-configmap (`configmap.yaml:14,22`, `base64+age:`); per project `config.age-private-key` en `repositories[].password` in zad-projects (`opi/utils/project_utils.py:344`, `opi/utils/age.py:486`); de ArgoCD repository-secrets in zad-argo-user-applications (`features/sops-sleutel-roteren.md:27-28`); de TLS-wildcard voor de sandbox NIET (die staat op de developer-sleutel) |
| platformsleutel (sandbox) | `sops-age-key` in `rig-system` en `rig-ron`; laptop `security/sandbox-key.txt` | hetzelfde voor de sandbox |
| projectsleutel, één per project | in het projectbestand, versleuteld met de platformsleutel (`config.age-private-key`); als k8s Secret `sops-age-key` in elke namespace van dat project, geschreven door OPI (`opi/handlers/sops.py:195-238`, `project_manager.py:2186-2245`); in de UI zichtbaar voor admin/owner (`project-tabs.html.j2:191-194`) | alles in zad-deployments van dat project (`*.sops.yaml` via `encrypt_to_sops_files_or_fail`, `project_manager.py:4199,4708`); in het projectbestand: `config.api-key` (`project_utils.py:353`), Keycloak realm-adminwachtwoord en TOTP-seed (`keycloak_manager.py:1898,1908`), `user-env-vars`, `aliases`, `helm-values`, attachments (`generators.py:158`) |
| developer-sleutel | `security/developer-key.txt` (gitignored) | `security/tls/sandbox-wildcard/{fullchain,privkey}.pem.age`, de enige sleutelbestanden die versleuteld in git staan (`Taskfile.yaml:2498-2549`) |

Wie welke private key heeft:

- De OPI-pod: de platformsleutel als env-var, en daarmee via zad-projects elke projectsleutel. De ClusterRole-blueprint geeft OPI daarnaast clusterbreed `secrets: create, get, list, patch, delete` (`_blueprint-cluster-role.yaml:16-20`); op ODCN krijgt OPI via Capsule nog meer (regel 2-6 van dat bestand).
- De ArgoCD CMP-sidecar: geen sleutel in zijn image of configuratie, maar hij leest bij elke render het Secret `sops-age-key` uit de doelnamespace met `kubectl get secret` (`sops-plugin.sh:23`, `configmap-sops-plugin.yaml:51`). Hij rendert zowel projectnamespaces als `rig-prd-operations` en `rig-prd-ron`, dus zijn ServiceAccount moet elk van die secrets kunnen lezen. Het repo-server-deployment, en dus de sidecar, draait onder `argocd-argocd-server` (`overlays/odcn-production/argocd-deployment.yaml:127`, sandbox `:89`).
- Tenant-namespaces: het Secret `sops-age-key` in een projectnamespace bevat de PROJECTsleutel, niet de platformsleutel (`project_manager.py:2205-2207` haalt de projectsleutel op en geeft die door aan `store_project_sops_key_in_namespace`). Een tenant-pod krijgt geen Role of RoleBinding van OPI (geen `kind: Role` in `manifests/` of `opi/`), dus met zijn ServiceAccount-token kan hij het Secret niet via de API lezen. Dat is goed.
- Ontwikkelaarslaptops: de platformsleutel staat lokaal in `security/key.txt` bij wie de bootstrap of een rotatie doet. `docs/sops-en-age-met-de-hand.md:16-18` noemt dat zelf een tussenoplossing (RC-222).

Wat één gelekte sleutel ontsluit: de platformsleutel plus een clone van zad-projects levert elke projectsleutel, en daarmee elk geheim van elk project in zad-deployments, plus de git-credentials van OPI en ArgoCD. `plans/de-blast-radius-van-een-sleutel.md:5` stelt dat deze sleutel blootgesteld is geweest; de rotatie is volgens `features/sops-sleutel-roteren.md` ingericht en volgens een ongetrackt plan op 24 september uitgevoerd. Een gelekte projectsleutel ontsluit alleen dat project, maar wel ook zijn `repositories[].password`, en dat is de gedeelde GitHub-PAT (zie D-4).

## Sterke plekken

- Randomness klopt overal waar het telt: `secrets.choice`/`secrets.SystemRandom` voor wachtwoorden (`opi/utils/passwords.py:48-67`), API-keys (`api_keys.py:42-43`), TOTP-seeds (`totp.py:41`), cookie-secrets en CSRF-tokens. `random` komt alleen voor in `opi/utils/project_names.py:65,73` voor een naam-postfix, met `noqa: S311`.
- Lengtes en alfabet: standaard 20 alfanumerieke tekens met minimaal 3 hoofdletters, 3 kleine letters en 3 cijfers (ongeveer 119 bits) voor database-, MinIO-, mail- en Keycloak-adminwachtwoorden (`database_manager.py:332`, `minio_manager.py:1000`, `keycloak_manager.py:1892`); API-key 32 alfanumeriek (`api_keys.py:24`); TOTP-seed 32 (`totp.py:35`); `SECRET_KEY` minimaal 32 tekens met fail-closed bij een kortere waarde en een verse random per proces als hij ontbreekt (`core/secret_key.py:26,46,59`).
- Private keys gaan nooit als command-line-argument naar `age` of `sops`: als tijdelijk bestand met `NamedTemporaryFile` (mode 0600, `delete=True`, opgeruimd door de context manager ook bij een exceptie) (`age.py:38-54,174-184`), of via de omgevingsvariabele `SOPS_AGE_KEY` van alleen dat kindproces (`sops.py:139`, `handlers/sops.py:172`). Plaintext gaat via stdin (`age.py:54,108`). Alleen de publieke sleutel staat in argv.
- Fail-closed rond plaintext: `encrypt_to_sops_files_or_fail` is de enige ingang voor secretbestanden en weigert door te gaan als er een `.to-sops.yaml` overblijft (`sops.py:254-293`); een test bewaakt dat managers de kale variant niet aanroepen (`features/sops-skip-unchanged-reencryption.md:116-118`); de git-connector weigert een commit met een `.to-sops.yaml` in de werkboom (`git.py:1181-1213`); `*.to-sops.yaml` staat als laatste vangnet in `.gitignore:32-35`; en `store_project_sops_key_in_namespace` en de attachments-generator breken af zonder publieke sleutel (`project_manager.py:2203-2206`, `generators.py:151-156`).
- De skip-if-unchanged-optimalisatie faalt de goede kant op: bij twijfel wordt opnieuw versleuteld (`sops.py:102-129`), en `sops rotate` in plaats van `updatekeys` zodat de datasleutel meedraait (`docs/sops-en-age-met-de-hand.md:85-87`).
- Logging: geen plaintext van wachtwoorden of sleutels in de logregels van `age.py`, `sops.py`, `handlers/sops.py`; kubectl-commando's worden als waardevrije samenvatting gelogd (`kubectl.py:234-243`); git-commando's via `_obfuscate_git_command`; de CMP dumpt `.cmp-env` niet meer in de repo-server-logs (`configmap-sops-plugin.yaml:26-28`); de private key wordt in `config.py:721,744` alleen als eerste en laatste twee tekens gelogd; `logging_redact.py` maskeert `Authorization`, `Cookie`, `X-API-Key` en verwanten in header-dumps.
- API: de leesendpoints maskeren beide opslagvormen en ook `plain:` (`opi/api/v2/project_read.py:52-73`), `config/api-key` en `age-private-key` komen nooit terug (`features/project-opvragen-api.md:136`), er is bewust geen endpoint dat de API-key teruggeeft (`extract_project_api_key.py:10`), en de vergelijking van API-keys gebeurt met `secrets.compare_digest` (`endpoint_util.py:68,134,175`, `task_router.py:140,267`).
- UI: de TOTP-seed bereikt de pagina nooit, alleen de code van dit moment (`router.py:1567-1583`); API-key, private key en realm-adminwachtwoord staan achter `user_role in ["admin", "owner"]` (`project-tabs.html.j2:163`).
- Attachments, `user-env-vars`, `aliases` en helm-values worden met de projectsleutel versleuteld voordat ze in zad-projects landen; de keycloak-adminwachtwoorden en de TOTP-seed ook (`keycloak_manager.py:1898,1908,2120`).
- Het `sops-age-key`-Secret in een projectnamespace draagt de projectsleutel en niet de platformsleutel; OPI vervangt het alleen als de publieke sleutel niet meer klopt (`project_manager.py:2211-2225`).
- Tenant-pods krijgen een eigen ServiceAccount zonder Role (`manifests/project-serviceaccount.yaml.jinja`), dus geen API-toegang tot secrets in hun eigen namespace.
- Rotatie van de platformsleutel is uitgewerkt met dry-run, fingerprint van de plaintext voor en na, selectie op recipient in plaats van op naam, een coverage-guard op losse waarden, een verplichte eindtoets dat de oude sleutel niets meer opent, en per-namespace vervanging die projectsleutels ongemoeid laat (`features/sops-sleutel-roteren.md`, `scripts/sops_key_secret.py:194-226`). Sleutels en PAT's komen uit bestanden in het gitignored `security/`, nooit uit argv (`scripts/key_rotation.py:226-237`, `scripts/project_rotation.py:726-736`). De scripts printen paden, veldnamen en aantallen, geen waarden.
- De rotatie is als BIO2 8.24-maatregel geformuleerd (`features/sops-sleutel-roteren.md:9`) en heeft acht testbestanden, waaronder een test dat het manifest nooit door argv gaat (`tests/test_set_sops_key_secret.py:161`).
- TLS-materiaal in git staat alleen versleuteld (`security/tls/sandbox-wildcard/*.pem.age`), `.gitignore:11` sluit `.pem` onder `security/tls/` uit.
- `secrets-overview-*` heeft nooit in git gestaan (`git log --all -- '*secrets-overview*'` is leeg) en de Taskfile ruimt het bestand op (`Taskfile.yaml:3509-3510`). Ook `*.to-sops.yaml` en `.env.secrets` zijn nooit gecommit, en `git check-ignore -v` bevestigt dat alle betreffende `.gitignore`-regels werken (`:8,12,18,30,35,73`).
- De secret-scanner valideert semantisch in plaats van met een allowlist: een AGE-kandidaat telt alleen als `age-keygen` hem accepteert, een JWT alleen met een echte header, een PEM alleen met body (`scripts/secret_scan.py:74-89,107-122,133-145`); base64-runs worden één niveau gedecodeerd en opnieuw getoetst (`:148-177`); ongelezen bestanden worden geteld en in het verdict gemeld (`:393-413`); er is bewust geen schrijfpad voor bevindingen (`scripts/scan-secrets.py:11-13`). De huidige boom van `main`, `origin/main` en `forgejo/main` is schoon.
- Eerdere lekken zijn eerlijk in commitberichten benoemd, inclusief "blijft in de historie staan" (fc65e2f19, a7d5511c9), en hebben regressietests gekregen (`tests/test_argo_repository_no_hardcoded_key.py`, `tests/test_plaintext_secret_guard.py`); sinds f34f90c9f maakt elke test zijn eigen sleutelpaar.

## Bevindingen

### D-1: De productie-platformsleutel heeft ruim een jaar in een testbestand op publiek GitHub gestaan; de rotatie verving de sleutel maar niet de secrets eronder

- Ernst: Kritiek
- Zekerheid: BEVESTIGD (sleutel geldig, recipient-koppeling, publieke remote, geen vervanging in git); of de waarden buiten git om zijn vervangen is niet vast te stellen, maar GitOps maakt dat onwaarschijnlijk: dan zouden de SOPS-bestanden mee moeten zijn veranderd.
- BIO2: 8.24 (cryptografie en sleutelbeheer), 5.17 (authenticatie-informatie), 8.04 (broncode), 8.12 (gegevenslekken), 5.26 (incidentrespons)
- Beschrijving: `operations-manager/python/tests/test_age_password_decryption.py` bevatte van 4 juli 2025 tot 22 september 2026 een geldige AGE-privésleutel. Diezelfde sleutel was tot de rotatie van 24 september 2026 de recipient van 21 SOPS-bestanden in deze repo, waaronder de volledige odcn-productieset: de OPI-env-secrets (met onder meer `KEYCLOAK_ADMIN_PASSWORD`, `DATABASE_ADMIN_PASSWORD`, `MINIO_ADMIN_SECRET_KEY`, `OIDC_CLIENT_SECRET`, `GRAFANA_TOKEN`, `SECRET_KEY`), argocd-admin, keycloak-admin, postgres-admin, minio-admin, redis-admin, pgadmin, transip, vault-init, mail-relay, mail-db, backup-destination en prometheus-metrics-auth. De repository `RijksICTGilde/RIG-Cluster` is publiek op GitHub, en het testbestand met de sleutel zit in de eerste commit van `origin/main` (oktober 2025). Sleutel en ciphertext hebben dus bijna een jaar naast elkaar in het openbaar gestaan. De rotatie van 24 september heeft, volgens het eigen commitbericht, "alleen de encryptie veranderd" en alleen de git-token vervangen; er is sindsdien geen commit die één van de 21 bestanden opnieuw aanraakt. De platte tekst die iedereen kon lezen is daarmee nog steeds de platte tekst die in productie geldt.
- Bewijs (alleen paden, hashes en tellingen; geen sleutel- of secretinhoud):
  - `git log --diff-filter=A -- operations-manager/python/tests/test_age_password_decryption.py`: b8cd437dc (2025-07-04); verwijderd in f34f90c9f (2026-09-22, "elke toets maakt zijn eigen AGE-sleutel")
  - `git merge-base --is-ancestor 278588e29 origin/main`: waar; `git show 278588e29:<pad> | grep -c AGE-SECRET-KEY-1` = 1; `age-keygen -y` accepteert die regel als sleutel
  - de publieke helft van die sleutel is gelijk aan `recipient:` in `00abace25^:bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production/operations-manager-env-secrets.yaml`; `git grep -l <recipient> 00abace25^ -- '*.yaml'` = 21 bestanden (lijst in de beschrijving), `HEAD` = 0
  - `gh repo view --json visibility`: `PUBLIC`
  - `git show 00abace25` (2026-09-24, "De platformsleutel en het git-wachtwoord vervangen"): "92 velden omgesleuteld ... alleen de encryptie veranderde, en waar de token vervangen werd veranderde die met opzet"
  - `git log 00abace25..HEAD -- 'bootstrap/**/*.sops.yaml' 'infrastructure/**/*.sops.yaml' ...`: leeg
  - `plans/de-blast-radius-van-een-sleutel.md:5` "de platform-AGE-sleutel is blootgesteld geweest"
- Aanvalsscenario: iemand cloont de publieke repo op een commit van voor 22 september, haalt de sleutel uit het testbestand en ontsleutelt de 21 bestanden op een commit van voor 24 september. Dat levert vandaag nog het Keycloak-adminwachtwoord van productie, het Postgres-superuserwachtwoord, de MinIO-rootcredentials, het ArgoCD-adminwachtwoord, de TransIP-API-sleutel en de `SECRET_KEY` waarmee OPI-sessies worden gesigneerd. Daarnaast: het voorbeeldprojectbestand in `projects/` ging mee op de sleutel, en projectbestanden in zad-projects van voor 24 september dragen `config.age-private-key` onder dezelfde recipient (D-5). Forks, caches, zoekmachines en secret-scanners van derden hebben de sleutel met zekerheid gezien; GitHub push protection greep niet in omdat de sleutel er al stond.
- Aanbeveling: behandel dit als een lopend incident en niet als een afgeronde rotatie. (1) Alle waarden die voor 24 september onder deze recipient stonden vervangen, in volgorde van schade: Keycloak-admin en `KEYCLOAK_MASTER_OIDC_CLIENT_SECRET`, Postgres-admin, MinIO-admin, ArgoCD-admin, TransIP, vault-init, `SECRET_KEY`, `GRAFANA_TOKEN`, Redis, pgadmin, mail; daarna de projectsleutels (#183) en wat daaronder hangt. (2) Meting: audit-logs van Keycloak, ArgoCD, TransIP en de Postgres-superuser over de periode oktober 2025 tot nu op onbekende logins. (3) Het incident vastleggen (post-mortem in `docs/post-mortems/`, melding volgens het eigen incidentproces, en aan ODC-Noord). (4) Structureel: `--history` van de scanner in de wekelijkse workflow en een pre-receive hook op Forgejo (D-20), en voortaan geen SOPS-ciphertext van productie in een publieke repo zonder dat de recipient aantoonbaar nooit in git heeft gestaan.

### D-2: Ontsleutelde helmfile env-vars worden plat in zad-deployments gecommit

- Ernst: Hoog
- Zekerheid: BEVESTIGD in code en documentatie; niet in een clone van zad-deployments nagemeten (geen git-toegang in deze toets).
- BIO2: 8.24 (cryptografie), 8.12 (gegevenslekken), 8.04 (broncode en repositories)
- Beschrijving: bij een helmfile-deployment leest OPI `env-vars` uit de projectdefinitie, ontsleutelt AGE-versleutelde waarden met de projectsleutel en schrijft ze als `KEY=plaintext` in `.cmp-env` in de deploymentmap. Die map wordt met `git add -A` gecommit en gepusht. De enige plaintext-guard kijkt alleen naar `*.to-sops.yaml`. De CMP-sidecar sourcet `.cmp-env` uit de checkout, dus het bestand moet in git staan om te werken. De featuredoc noemt de waarde `<encrypted>`, de code doet het tegenovergestelde.
- Bewijs:
  - `operations-manager/python/opi/manager/project_manager.py:4928` `cmp_env_path = os.path.join(target_path, ".cmp-env")`
  - `project_manager.py:4947-4948` `decrypted_value = await decrypt_age_content(value, env_private_key)` gevolgd door `cmp_env_vars.append(f"{key}={decrypted_value}")`
  - `project_manager.py:4952-4954` schrijft `cmp_env_vars` naar `cmp_env_path`
  - `project_manager.py:3899` `commit_changes(...)` na `_process_deployment_manifests`; `opi/connectors/git.py:1428` `add_cmd = ["add", "-A"]`
  - `git.py:1196-1202` de guard filtert op `name.endswith(".to-sops.yaml")`, niets anders
  - `bootstrap/rig-system/kustomize/configmap-sops-plugin.yaml:26-30` "Do NOT dump the contents of .cmp-env: it holds configuration secrets" en `source .cmp-env`
  - `features/argocd-render-error-surfacing.md:61` "`.cmp-env` contents (which hold secrets)"
  - `features/helmfile-single-app-deployment.md:183-187` toont `MIJNBUREAU_MASTER_PASSWORD=<encrypted>`
  - `tests/test_helmfile_env_vars_decrypt.py:103-105` verwacht de ontsleutelde waarde in `.cmp-env`
- Aanvalsscenario: iedereen met leesrecht op zad-deployments (en op elke clone, backup of mirror ervan) leest deze waarden zonder enige sleutel, ook uit de historie. Een master-wachtwoord van een helmfile-applicatie is daarmee zo geheim als de repo zelf.
- Aanbeveling: `.cmp-env` als `.to-sops.yaml`-achtig bestand behandelen: versleuteld met de projectsleutel committen en in de CMP na het lezen van `SOPS_AGE_KEY` ontsleutelen (`sops -d` of `age -d` op een `.cmp-env.sops`), of de env-vars als KSOPS-Secret meegeven. De guard in `git.py:1196-1202` uitbreiden met een test op `KEY=value`-bestanden die een geheim dragen. De historie van zad-deployments na de fix beoordelen: de bestaande plaintext blijft daar staan tot een rewrite of totdat de betreffende waarden zijn vervangen (zie D-5).

### D-3: Eén platformsleutel ontsluit alles, en hij ligt op meer plekken dan de kluis

- Ernst: Hoog (ontwerp-blast-radius; de rotatie is inmiddels ingericht)
- Zekerheid: BEVESTIGD
- BIO2: 8.24.01/02 (registratie en actueel houden van cryptografie, sleutelbeheer), 5.17
- Beschrijving: de platformsleutel versleutelt niet alleen de platformsecrets maar ook elke projectsleutel (`config.age-private-key`) en het git-wachtwoord in elk projectbestand, en daarmee transitief alle projectgeheimen. De sleutel staat in de OPI-pod als omgevingsvariabele, in twee Kubernetes-namespaces, in een handmatige kopie die in geen manifest voorkomt, en op laptops. Er is geen KMS/HSM, geen scheiding tussen "kan projectsleutels lezen" en "kan platformsecrets lezen", en geen tijdgebonden toegang. De blootstelling die `plans/de-blast-radius-van-een-sleutel.md` beschrijft was daarom een blootstelling van alles.
- Bewijs:
  - `features/sops-sleutel-roteren.md:13-29` (boom van wat de sleutel vasthoudt, inclusief "de EIGEN sleutel van dat project, en daaronder al zijn andere geheimen")
  - `opi/utils/age.py:486` `decrypt_age_content(encoded_private_key, settings.SOPS_AGE_PRIVATE_KEY)` (projectsleutel opent met de platformsleutel)
  - `opi/utils/project_utils.py:344` `encrypt_age_content(private_key, settings.SOPS_AGE_PUBLIC_KEY)`
  - `bootstrap/rig-system/kustomize/operations-manager/base/deployment.yaml:117-121` `SOPS_AGE_KEY_CONTENT` via `secretKeyRef`
  - `instructions/sops-sleutel-in-het-cluster.md:23` de kopie in `rig-prd-ron` "kan niet uit git komen"
  - `docs/sops-en-age-met-de-hand.md:16-18` "`security/` is een tussenoplossing ... (RC-222)"
  - `plans/de-blast-radius-van-een-sleutel.md:5` "de platform-AGE-sleutel is blootgesteld geweest"
- Aanvalsscenario: één lek (pod-compromise, laptopdiefstal, een verkeerde `kubectl get secret -o yaml` in een ticket) plus leesrecht op zad-projects geeft elk wachtwoord, elke API-key en elke PAT van het platform, ook met terugwerkende kracht op oude commits.
- Aanbeveling: het lopende RC-222-onderzoek afronden met minimaal (1) de projectsleutels niet langer met de platformsleutel maar met een aparte "projectsleutel-KEK" versleutelen die alleen OPI heeft, zodat de bootstrap-sleutel voor `bootstrap/` niets over projecten zegt; (2) de sleutel uit de omgevingsvariabele halen en per aanroep uit een gemount Secret-bestand lezen, zodat hij niet met `os.environ.copy()` naar elk kindproces gaat (`git.py:386`); (3) de handmatige kopie in `rig-prd-ron` vervangen door een eigen sleutel voor de mailrelay.

### D-4: Eén gedeelde GitHub-PAT met schrijfrechten staat in elk projectbestand en elke ArgoCD-repository-secret

- Ernst: Hoog
- Zekerheid: BEVESTIGD (kopieerpad); de scope van de PAT zelf (fine-grained of classic, welke repos) is niet uit de code af te leiden.
- BIO2: 5.17 (authenticatie-informatie), 8.24, 8.04
- Beschrijving: bij projectaanmaak kopieert OPI `settings.PROJECT_REPO_PASSWORD` in `repositories[].password` van het nieuwe projectbestand. Vervolgens ontsleutelt OPI dat wachtwoord en schrijft het plat in een per-project ArgoCD-repository-Secret, dat als SOPS-bestand (platformsleutel) naar zad-argo-user-applications gaat. Dezelfde PAT, met schrijfrechten (OPI pusht ermee), leeft dus in circa evenveel kopieën als er projecten zijn, op twee plekken per project. Een projectsleutel-lek levert daarmee ook schrijftoegang tot de deployments van alle projecten op. Rotatie vraagt daarom een drie-plaatsen-ronde en een dag wachttijd.
- Bewijs:
  - `operations-manager/python/opi/utils/project_utils.py:360` `repo_password = settings.PROJECT_REPO_PASSWORD` en `:466-474` (`"repositories": [{... "password": repo_password ...}]`)
  - `opi/manager/argo_manager.py:331-334` `decrypt_password_smart(...)` en `:343-350` `"password": decrypted_password`
  - `manifests/argo-repository-https.yaml.jinja:15` (wachtwoordveld in het Secret)
  - `features/sops-sleutel-roteren.md:27-28,165-166` "het repo-wachtwoord staat daar PLAT in het sops-bestand"
  - `scripts/key_rotation.py:661-663,692-696` ("shared GitHub token")
- Aanvalsscenario: een projectbeheerder met de eigen projectsleutel (zichtbaar in de UI) ontsleutelt `repositories[].password` uit zijn eigen projectbestand en heeft daarmee push-rechten op zad-deployments van iedereen. Manifesten van een ander project wijzigen betekent code-uitvoering in diens namespace via ArgoCD.
- Aanbeveling: ArgoCD een eigen read-only credential geven via een repo-credential-template op URL-prefix in de ArgoCD-namespace, zodat er geen per-project repository-Secret met een schrijf-PAT meer nodig is; `repositories[].password` uit het projectbestand halen (OPI is de enige die schrijft en heeft de credential al in zijn eigen config); de resterende PAT fine-grained maken met alleen `contents: write` op zad-deployments.

### D-5: Rotatie hersleutelt maar vervangt niet; de historie blijft leesbaar; er is geen incident-runbook

- Ernst: Hoog
- Zekerheid: BEVESTIGD
- BIO2: 8.24.02, 8.10 (wissen), 5.17
- Beschrijving: de platformrotatie zet ciphertext om naar een nieuwe sleutel en houdt de plaintext gelijk. De projectsleutels worden niet vervangen (issue #183 is buiten scope), zad-deployments ook niet, en de secrets eronder (database-, MinIO-, Keycloak-, API-keys) evenmin. Git-historie wordt niet herschreven. Wie de oude platformsleutel had en een clone van zad-projects heeft, ontsleutelt daaruit nog steeds elke projectsleutel en daarmee alle projectgeheimen, ook na de rotatie. Dat is inherent aan een git-gebaseerd secretmodel en hoort als restrisico expliciet te zijn geaccepteerd, met een runbook voor "sleutel gelekt" dat verder gaat dan de kwartaalprocedure. Beide ontbreken in de bindende documentatie.
- Bewijs:
  - `scripts/key_rotation.py:289-294` "Without new_plaintext this is a recrypt: the plaintext stays the same"
  - `scripts/key_rotation.py:931-932` "config.age-private-key is always only re-encrypted"
  - `features/sops-sleutel-roteren.md:33` "Buiten scope: de sleutels per project (issue #183) en de zad-deployments-repo"
  - `scripts/project_rotation.py:103-133` (commit per bestand, geen rewrite); geen `filter-repo`/`filter-branch` in `scripts/`
  - `plans/de-sops-sleutel-vervangen-met-een-script.md:294` benoemt het als open beslissing; `plans/de-blast-radius-van-een-sleutel.md:36-39` als onderzoeksvraag
  - `grep -rln -iE 'gelekt|compromised' docs features instructions`: geen runbook; `docs/post-mortems/` bevat niets over de blootstelling
  - Geen rollbacksectie in `features/sops-sleutel-roteren.md` (alleen `plans/...:90`)
- Aanvalsscenario: de blootstelling van september. Een kopie van sleutel A en een clone van zad-projects van voor 24 september volstaan om vandaag nog elk projectgeheim te lezen dat sindsdien niet is vervangen.
- Aanbeveling: (1) een beslisdocument in `features/` dat het historie-restrisico benoemt en de keuze vastlegt (herschrijven van de private zad-projects-historie na een bevestigd lek, of accepteren); (2) bij een bevestigd lek van de platformsleutel ook de projectsleutels roteren (#183 uitvoeren) en daarna de onderliggende secrets vervangen, met een prioriteitsvolgorde (PAT, Keycloak-admin, database, MinIO, API-keys); (3) een kort incident-runbook en een rollbacksectie; (4) een doorloopverslag in `docs/` van de uitgevoerde rotatie, want dat is het bewijs voor 8.24.02. Nu staat de uitvoering alleen in een ongetrackt plan.

### D-6: Secret-manifesten, inclusief de projectsleutel, gaan via een shell-heredoc door argv

- Ernst: Midden
- Zekerheid: BEVESTIGD
- BIO2: 8.24, 8.12
- Beschrijving: `KubectlConnector._run_kubectl_command` bouwt bij stdin-invoer één shellstring `kubectl ... <<'EOF'\n<manifest>\nEOF` en start die met `create_subprocess_shell`. Het manifest, met de plaintext `stringData`, staat dan als argument van `/bin/sh -c` in `/proc/<pid>/cmdline` zolang kubectl draait. `store_project_sops_key_in_namespace` gebruikt dit pad voor het Secret met de projectsleutel; `apply_manifest` voor elk ander Secret dat OPI direct toepast. De rotatiescripts omzeilen dit bewust en hebben er een test voor; de productiecode niet.
- Bewijs:
  - `operations-manager/python/opi/connectors/kubectl.py:241-247` `shell_cmd = f"{cmd_str} <<'EOF'\n{stdin_input}\nEOF"` en `asyncio.create_subprocess_shell(shell_cmd, ...)`
  - `kubectl.py:375` `_run_kubectl_command(args, stdin_input=manifest_content)` in `apply_manifest`
  - `opi/handlers/sops.py:216,225,236` `full_key_contents = f"...{private_key}\n"` in `secret_pairs`, toegepast via `apply_manifest`
  - `scripts/sops_key_secret.py:112-121` en `tests/test_set_sops_key_secret.py:161` (`test_the_manifest_never_travels_through_argv`)
- Aanvalsscenario: elk proces in de pod met dezelfde uid (een gecompromitteerde bibliotheek, een debug-sidecar) en elke node-agent die procesargumenten verzamelt (auditd, security-agents, `ps` in een node-shell) ziet de projectsleutel en andere secrets voorbijkomen. Ook een regel `EOF` in een secretwaarde breekt de heredoc, wat naar shell-injectie neigt.
- Aanbeveling: `create_subprocess_exec` met `stdin=PIPE` en `communicate(input=manifest)`, precies zoals `age.py:44-54` al doet. Geen shell.

### D-7: De CMP-sidecar draait onder de argocd-server-ServiceAccount en voert repo-inhoud uit

- Ernst: Midden
- Zekerheid: AANNEMELIJK (de ClusterRole van `argocd-argocd-server` wordt door de operator gegenereerd en staat niet in deze repo; dat de plugin clusterbreed secrets moet kunnen lezen volgt uit zijn werking)
- BIO2: 8.24, 8.04, 5.15
- Beschrijving: de plugin leest per render `sops-age-key` uit de doelnamespace, ook uit `rig-prd-operations` (de platformsleutel). Daarvoor heeft de repo-server-pod, en dus de sidecar, leesrecht op Secrets in alle namespaces. Diezelfde sidecar sourcet `.cmp-env` uit de git-checkout, draait `kustomize --enable-exec`, `helm dependency build` en helmfile op inhoud uit git, inclusief helmfile-bronrepos van derden die OPI in de deploymentmap kloont. Wie de renderinvoer beïnvloedt, draait code met een ServiceAccount die de platformsleutel kan lezen.
- Bewijs:
  - `bootstrap/rig-system/kustomize/overlays/odcn-production/argocd-deployment.yaml:127` `serviceaccount: argocd-argocd-server` onder `repo:` en `:128-132` de `cmp-server`-sidecar; sandbox `:89`
  - `bootstrap/rig-system/kustomize/sops-plugin.sh:23` `kubectl get secret ${SOPS_KEY_SECRET} -n ${ARGOCD_APP_NAMESPACE}`
  - `configmap-sops-plugin.yaml:25-32` `source .cmp-env`; `:57` `kustomize build --enable-alpha-plugins --enable-exec`
  - `instructions/sops-sleutel-in-het-cluster.md:19-20` (de plugin rendert ook de platformhouders)
- Aanvalsscenario: een schrijfrecht op zad-deployments (zie D-4 voor hoe breed dat ligt) of op een helmfile-bronrepo wordt via `.cmp-env` of een kustomize-exec-plugin code-uitvoering in de repo-server, waarna `kubectl get secret sops-age-key -n rig-prd-operations` de platformsleutel oplevert.
- Aanbeveling: de RBAC van die ServiceAccount opvragen en vastleggen (dit is de open vraag hieronder); de sidecar een eigen ServiceAccount geven met een Role per namespace die alleen `get` op het ene Secret toestaat, en de platformnamespaces uitsluiten door platformrenders een andere plugin of een aparte repo-server te geven; `.cmp-env` niet sourcen maar parsen.

### D-8: Werkbestanden in `security/` zijn world-readable, waaronder PAT's en oude platformsleutels

- Ernst: Midden
- Zekerheid: BEVESTIGD (alleen `ls -la`; inhoud niet bekeken)
- BIO2: 5.17, 8.10, 8.24
- Beschrijving: de map is gitignored en dus buiten git veilig, maar op de laptop zelf hebben verschillende bestanden mode 0644. Daaronder de twee PAT-bestanden van de rotatieronde, twee oude platformsleutelbestanden, de developer-sleutel voor het TLS-certificaat en een VLAM-sleutelbestand. Alleen `key.txt` en de sandbox-sleutels staan op 0600. De rotatieprocedure zegt dat de tokenbestanden na stap 8 worden opgeruimd; ze staan er nog met een datum van 23 september. Dat kan volgens procedure zijn (stap 8 is "een dag later"), maar 8.10 vraagt dat wissen een afgesproken moment heeft en niet een herinnering.
- Bewijs (padnamen en rechten, geen inhoud): `security/pat_current.txt` en `security/pat_new.txt` (`-rw-r--r--`), `security/old_key.txt` en `security/original_zad_key.txt` (`-rw-r--r--`), `security/developer-key.txt` (`-rw-r--r--`), `security/vlam.txt` (`-rw-r--r--`); `features/sops-sleutel-roteren.md:173-177` (stap 8 en "Daarna pas de twee tokenbestanden opruimen").
- Aanvalsscenario: elke andere gebruiker of elk proces op de laptop leest de PAT en de vorige platformsleutel; die laatste opent nog steeds de volledige historie van zad-projects (D-5).
- Aanbeveling: `chmod 600 security/*`; de rotatiescripts laten weigeren op een sleutel- of tokenbestand dat niet 0600 is (zoals `ssh` doet); een expliciete opruimstap met datum in het runbook, en de oude sleutels daarna alleen nog in een wachtwoordkluis met vervaldatum, niet op schijf.

### D-9: Productie-PAT als ciphertext hardcoded in broncode en migratiescript; zad-deployments-credential ontbreekt in de prod-overlay

- Ernst: Midden
- Zekerheid: BEVESTIGD
- BIO2: 8.04, 8.24, 5.17
- Beschrijving: `PROJECT_REPO_PASSWORD` heeft in `config.py` een default die een `base64+age:`-ciphertext van de productie-PAT is; de odcn-overlay zet de waarde niet, dus productie draait op de default in de code. Hetzelfde staat in een migratiescript. Het is versleuteld, maar de sleutel staat in elke productiepod, de waarde reist mee in elke image en elke clone, en de rotatie moet dit via een handmatige lijst (`LOOSE_VALUE_FILES`) bijhouden.
- Bewijs: `operations-manager/python/opi/core/config.py:236-238`; `operations-manager/python/scripts/migrate_project_to_production.py` (één treffer op het prefix); `scripts/key_rotation.py:653-656` "PROJECT_REPO_PASSWORD is the default odcn-production and local fall back to (only sandboxed-local overrides it)"; `git log -S PROJECT_REPO_URL -- bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production` is leeg.
- Aanvalsscenario: geen direct lek, wel een deploybeslissing die in code verstopt zit en een extra plek die bij rotatie vergeten kan worden.
- Aanbeveling: default op `None` met fail-fast, de waarde expliciet in de odcn-overlay als Secret (niet ConfigMap), en het migratiescript de waarde uit settings laten lezen.

### D-10: Git-PAT in de clone-URL: op schijf in `.git/config`, in argv, en de obfuscatie dekt geen `http://`

- Ernst: Midden (sandbox), Laag (productie, want `https://` en de monitor staat uit)
- Zekerheid: BEVESTIGD
- BIO2: 5.17, 8.12
- Beschrijving: de credential wordt in de URL gezet (`user:pat@host`) en zo aan `git clone` en `git ls-remote` meegegeven. Daardoor staat hij in `remote.origin.url` in `.git/config` van elke clone, waaronder de permanente warme clone van de ProjectStore in `/tmp`, en tijdens de aanroep in `/proc/<pid>/cmdline`. De logobfuscatie matcht alleen `https://`; de sandbox gebruikt `http://` naar Forgejo, dus daar staat de PAT op DEBUG in de podlogs. Twee logregels in het git-monitor-pad gebruiken de URL zonder obfuscatie.
- Bewijs: `operations-manager/python/opi/connectors/git.py:288-330` (`netloc = f"{username}:{password}@{parsed.hostname}"`), `:512,649,727` (argv), `:69` (regex begint met `https://`), `:509` en `:1950` (`repo_url_with_path` direct in `logger.debug`), `opi/services/project_store.py:225,252-254` (warme clone, nooit gesloten), `overlays/sandboxed-local/configmap.yaml:21,29` (`http://forgejo...`), `opi/utils/logging_config.py:57-58,66-67` (logger op DEBUG).
- Aanvalsscenario: `kubectl exec` in de OPI-pod, een leesprimitief, of een logaggregatie met te brede leesrechten levert de PAT.
- Aanbeveling: clonen zonder userinfo en de credential per aanroep via `GIT_ASKPASS` of `http.extraHeader` in de omgeving meegeven; regex op `https?://`; beide logregels door `_obfuscate_git_command`; een unittest op de obfuscator (bestaat niet).

### D-11: Geen zelfbedieningsrotatie voor database-, MinIO-, Keycloak-client- en API-key-secrets

- Ernst: Midden
- Zekerheid: BEVESTIGD
- BIO2: 5.17, 8.24.02
- Beschrijving: databasewachtwoorden en MinIO-credentials worden hergebruikt zolang ze werken; alleen bij een mislukte authenticatie en de vlag `RECREATE_PASSWORD_ON_AUTHENTICATION_FAILURE` (default `False`) volgt een nieuwe waarde. Keycloak-clientsecrets kennen geen regenerate-pad. De project-API-key wordt eenmalig gegenereerd en heeft geen endpoint of UI om te roteren; er is één geldige key zonder overlapvenster. Roteren na een vermoed lek is dus handwerk in het projectbestand of het cluster.
- Bewijs: `opi/manager/database_manager.py:474-500,332-353`; `opi/core/config.py:196-198`; `opi/manager/minio_manager.py:263-345`; `opi/connectors/keycloak.py:551,651-657,757-763` (geen regenerate); `opi/utils/project_utils.py:352` en `opi/forms/editables/fields/config_generated.py:39-42` (generatorveld, niet in formulieren); `opi/api/endpoint_util.py:68-70` (één key).
- Aanbeveling: per dienst een expliciete roteer-actie (UI en API) die de waarde vervangt, het Secret herschrijft en de deployment herstart; voor de API-key twee geldige keys met een korte overlap.

### D-12: Gedeeld realm-adminaccount met gedeelde TOTP-seed

- Ernst: Midden
- Zekerheid: BEVESTIGD
- BIO2: 5.17 (authenticatie-informatie is persoonsgebonden), 5.16
- Beschrijving: per project maakt OPI één Keycloak realm-admin met één wachtwoord en één TOTP-seed, opgeslagen in het projectbestand en getoond aan alle admins en owners. De code-op-dit-moment staat in de HTML van de projectpagina naast het wachtwoord. Dat is een bewuste keuze (gedeeld serviceaccount), maar het betekent dat een tweede factor die iedereen deelt geen tweede factor per persoon is, dat het wachtwoord in elke paginaweergave van een admin staat, en dat vertrek van één beheerder rotatie voor iedereen vraagt (D-11).
- Bewijs: `opi/utils/totp.py:1-8` ("shared service accounts ... Each admin loads the same seed"); `opi/manager/keycloak_manager.py:1892-1908,2051` (`totp_otpauth_uri` met de seed in het resultaat); `opi/web/router.py:1560-1583` (wachtwoord en TOTP-code in de rendercontext).
- Aanbeveling: realm-admins persoonsgebonden maken via federatie met het platform-realm en een groep, zodat een beheerder met zijn eigen SSO-identiteit en eigen tweede factor binnenkomt; het gedeelde account alleen als break-glass met logging bewaren.

### D-13: API-key-lookup met `==` en een dood verificatiepad met de verkeerde sleutel

- Ernst: Laag
- Zekerheid: BEVESTIGD
- BIO2: 5.17
- Beschrijving: `get_project_by_api_key` loopt alle projecten langs met een gewone stringvergelijking. Dat lekt in theorie via timing welke prefix klopt; de daaropvolgende `compare_digest` in de endpoints helpt daar niet meer bij. Daarnaast bestaat `ProjectManager.validate_project_api_key`, dat de opgeslagen key met de platformsleutel probeert te openen terwijl hij met de projectsleutel is versleuteld; het heeft geen aanroepers.
- Bewijs: `opi/services/project_service.py:119-121` `if project.api_key == api_key:`; `opi/manager/project_manager.py:9459-9462` `decrypt_password_smart_auto(...)` gevolgd door `compare_digest`; `grep -rn "validate_project_api_key" opi` levert alleen de definitie.
- Aanbeveling: vergelijken via `compare_digest` op een hash van de key (of keys in een dict op sha256 opslaan zodat lookup constant is); het dode pad verwijderen.

### D-14: DEBUG-logging en een API-key in een debugregel

- Ernst: Laag
- Zekerheid: BEVESTIGD
- BIO2: 8.12, 8.15
- Beschrijving: bij `USE_UNSAFE_API_KEY` logt `generate_api_key` de key zelf. Dat is een dev-vlag (productie zet hem op `false`), maar de logger staat hard op DEBUG, ook in productie, dus elke debugregel telt. Lokaal staat een gitignored logbestand met precies die regel erin, wat het punt illustreert.
- Bewijs: `opi/utils/api_keys.py:39` `logger.debug(f"Using unsafe API key from settings: {api_key}")`; `opi/core/config.py:749` logt de eerste vijf tekens van `API_TOKEN`; `overlays/odcn-production/configmap.yaml:63` `USE_UNSAFE_API_KEY=false`; `opi/utils/logging_config.py:57-58` (DEBUG); `operations-manager/python/log.txt` (lokaal, gitignored via `/operations-manager/python/log*`).
- Aanbeveling: de regel weghalen; het productieniveau op INFO en debug alleen per module aanzetten.

### D-15: De projectpagina ontsleutelt de projectsleutel, de API-key en adminwachtwoorden voor elke rol; alleen het template gaat er niet mee om

- Ernst: Laag
- Zekerheid: BEVESTIGD
- BIO2: 8.12, 5.15
- Beschrijving: de route ontsleutelt voor elke ingelogde projectgebruiker de private key, de API-key, de Keycloak-adminwachtwoorden en de TOTP-code en zet ze in de rendercontext; de rolcontrole zit uitsluitend in het template. Eén nieuw fragment of een `{{ project.config }}`-dump lekt alles naar een gewone member. Een ouder template toont de API-key zonder rolcontrole; het lijkt niet meer gerenderd te worden.
- Bewijs: `opi/web/router.py:1517` (rol opgehaald), `:1545-1556` (private key en API-key ontsleuteld zonder rolcheck), `:1560-1583` (Keycloak); `opi/templates_lotc/bg/project-tabs.html.j2:163` (`{% if user_role in ["admin", "owner"] %}`), `:177,194`; `opi/templates_lotc/bg/project-details.html.j2:89` (geen `user_role` in dat bestand; alleen in commentaar en `lotc_fixtures.py:520` genoemd).
- Aanbeveling: alleen ontsleutelen als `user_role in ("admin", "owner")`, en het ongebruikte template verwijderen.

### D-16: Plaintext is toegestaan in het projectbestand (`plain:`, aliases zonder sleutel, sandbox-credentials in git)

- Ernst: Laag
- Zekerheid: BEVESTIGD
- BIO2: 8.24, 8.04
- Beschrijving: het schema en de parser accepteren het prefix `plain:` voor wachtwoorden, en aliases worden onversleuteld opgeslagen als er geen publieke sleutel is (backward compatible). De API maskeert `plain:` netjes, maar het bestand in zad-projects bevat dan een geheim in klare tekst. In de sandbox-overlay staan Forgejo-credentials plat in git en `plain:`-wachtwoorden in de configmap; het zijn bekende Kind-defaults, geen productiewaarden.
- Bewijs: `opi/utils/age.py:327-328`; `opi/utils/project_utils.py:261-263` (`component_config["aliases"] = aliases_dict` zonder sleutel); `opi/api/v2/project_read.py:52-56`; `overlays/sandboxed-local/argocd-repo-forgejo-secret.yaml:14-15`; `overlays/sandboxed-local/configmap.yaml:23,31,59`.
- Aanbeveling: `plain:` alleen nog toestaan voor de sandbox-clustertypes (valideren op `CLUSTER_MANAGER`), aliases zonder sleutel weigeren in plaats van plat opslaan, en de sandbox-defaults via `@secret-gen:random` genereren zodat de scanner geen uitzondering hoeft te kennen.

### D-17: Sleutelmateriaal in de procesomgeving en op de emptyDir van de node

- Ernst: Laag
- Zekerheid: BEVESTIGD
- BIO2: 8.24
- Beschrijving: de platformsleutel staat als omgevingsvariabele in het OPI-proces en gaat via `os.environ.copy()` mee naar elk git- en kubectl-kindproces; `sops` krijgt de sleutel via `SOPS_AGE_KEY` in zijn omgeving. Tijdelijke sleutelbestanden voor `age -d` landen in `/tmp`, een `emptyDir` zonder `medium: Memory`, dus op de schijf van de node. De rotatiedoc laat de operator `export SOPS_AGE_KEY=...` in de interactieve shell zetten, waarna elk later kindproces van die shell de sleutel erft.
- Bewijs: `deployment.yaml:117-121,144-146` (`emptyDir: sizeLimit: 500Mi`, geen `medium`); `opi/core/config.py:288` (`TEMP_DIR = "/tmp"`); `opi/utils/age.py:38` (`NamedTemporaryFile(... suffix=".key")`); `opi/utils/sops.py:139`; `git.py:386`; `features/sops-sleutel-roteren.md:91`.
- Aanbeveling: `emptyDir: {medium: Memory}` voor `/tmp`; de sleutel als gemount bestand en `SOPS_AGE_KEY_FILE` in plaats van een env-var; in de doc de variabele per commando meegeven.

### D-18: Restpunten (Info)

- Fingerprint van de rotatie is een ongezouten sha256 van de plaintext; voor een zwak wachtwoord is dat een offline-orakel, voor sleutels en PAT's niet. Staat buiten git. `scripts/key_rotation.py:262-264`; de doc noemt het "bevat geen geheim" (`features/sops-sleutel-roteren.md:189`).
- Git-credentials van OPI in productie komen uit een ConfigMap (ciphertext) en niet uit een Secret: `overlays/odcn-production/kustomization.yaml`, `configmap.yaml:14,22`. Verdedigbaar, maar ConfigMaps hebben in de regel bredere leesrechten en verschijnen in `describe`-output en diffs.
- Een schema-foutmelding in de rotatiescripts kan een veldwaarde echoën als dat veld de eerste fout is (`scripts/key_rotation.py:856`, `opi/core/project_schema.py:246`). Raakt alleen platte waarden.
- Het aantal namespaces in de docs loopt uiteen (12 in `scripts/sops_key_secret.py:10`, 16 in `instructions/sops-sleutel-in-het-cluster.md:36`); de productie-uitvoering van de rotatie staat alleen in een ongetrackt plan.
- De `metadata.name` van SOPS-secrets wordt mee versleuteld (geen `encrypted_regex`); dat maakt `git diff` en code review op zad-deployments minder leesbaar, maar is geen zwakte.

### D-19: Een OpenSSH-privésleutel stond op zes paden in de historie van de publieke repo

- Ernst: Midden
- Zekerheid: BEVESTIGD (identieke blokhash op zes paden, `ssh-keygen -y` accepteert hem; of de publieke helft nog ergens als deploy key staat is niet getoetst)
- BIO2: 8.24, 5.17, 8.04
- Beschrijving: één ed25519-privésleutel voor de lokale test-gitserver zat van juni 2025 tot mei 2026 in `keys/git-server-key`, `output.yaml`, twee `argo-local-test-git-repo.yaml`-overlays en `manifests/argo-repository.yaml(.jinja)`, en het commit van voor de verwijdering staat op `origin/main`. De verwijdercommit noemt hem zelf "gecompromitteerd".
- Bewijs: `git log --all -S'PRIVATE KEY-----' --name-only`: eerste 81dd75f41 (2025-06-23), laatste verwijdering a7d5511c9 (2026-05-17) en ad98a1b2a/0a46ff6cc (2026-05-22); `.gitignore:12` (`/keys/`) en `tests/test_argo_repository_no_hardcoded_key.py` als regressietest.
- Aanbeveling: vaststellen en vastleggen dat de publieke helft nergens meer in `authorized_keys`, Forgejo deploy keys of een git-daemon staat.

### D-20: De bindende secret-scan draait alleen op GitHub, het werk loopt via Forgejo, en de historie wordt nergens gescand

- Ernst: Midden
- Zekerheid: BEVESTIGD (workflow en hook), AANNEMELIJK (Forgejo-serverconfig niet inzichtelijk)
- BIO2: 8.04, 8.28 (veilig ontwikkelen), 8.24
- Beschrijving: `scan-secrets.py` draait als pre-commit hook (lokaal, `--no-verify` is staande praktijk en in de documentatie geaccepteerd) en in `.github/workflows/security.yml` op push naar main, PR en wekelijks. Er is geen Forgejo Actions-workflow en geen pre-receive hook, terwijl de branches en PR-refs via Forgejo lopen (125+ `refs/remotes/forgejo-pr/*`). De `--history`-stand bestaat, maar "is meant to be run deliberately, not in CI", en er ligt geen resultaat van een historiescan vast. D-1 laat zien wat dat kost: een sleutel die al in de boom staat wordt door een boomscan pas gemeld als de regel bestaat die hem herkent, en door push protection nooit meer.
- Bewijs: `.github/workflows/security.yml:3-8,38-53`; `.pre-commit-config.yaml:49-53`; `scripts/secret_scan.py:3-6,361`; `features/sops-sleutel-roteren.md:201` ("met `--no-verify` te omzeilen, en dat is geaccepteerd"); geen `.forgejo/` of `.gitea/` in de repo; geen aanroep in `Taskfile.yaml`.
- Aanbeveling: dezelfde boomscan als Forgejo Actions-job of pre-receive hook; `--history` in de wekelijkse cron op een clone met alle refs, met het verdict als artefact; de oude PR-refs en branches op Forgejo opruimen (D-22).

### D-21: De scanner mist PEM-sleutels binnen base64 en een reeks tokenvormen

- Ernst: Midden
- Zekerheid: BEVESTIGD
- BIO2: 8.04, 8.28
- Beschrijving: de scanner is goed ontworpen (semantische validatie in plaats van een allowlist, een base64-pass voor k8s `Secret.data`), maar de multiline-regels (PEM) draaien alleen over ruwe tekst en niet over de gedecodeerde base64-blob. Een privésleutel in `Secret.data` of een `client-key-data` in een kubeconfig blijft daardoor onzichtbaar, en de test die "elke regel reikt door base64" belooft, parametriseert alleen over de één-regel-regels. Verder ontbreken `BEGIN ENCRYPTED PRIVATE KEY`, GitLab `glpat-`, Forgejo/Gitea-tokens, AWS secret access keys, credentials in URL's (`https://user:pw@host`) en entropiedetectie. Bestanden groter dan 2 MB en niet-UTF-8 worden overgeslagen, maar netjes gemeld.
- Bewijs: `scripts/secret_scan.py:96-104` (één-regel-regels), `:113-122` (PEM, zonder ENCRYPTED), `:208-215` (multiline alleen op ruwe tekst), `:52,262-268` (grootte en encoding); `operations-manager/python/tests/test_secret_scan.py:311-331` (alleen `RULES` door base64), `:919-930` (erkent de 32-hex-dev-token als niet gedekt).
- Aanbeveling: de gedecodeerde blob ook langs `MULTILINE_RULES`, met een test met een base64-gecodeerde PEM in `Secret.data`; `ENCRYPTED` in de PEM-groep; een generieke URL-credential-regel; en gitleaks of trufflehog als tweede, bredere laag in dezelfde workflow.

### D-22: Nog drie andere geldige AGE-sleutels in de publieke historie, en 537 van 548 lokale refs dragen er nog een

- Ernst: Midden
- Zekerheid: BEVESTIGD (elke kandidaat met `age-keygen -y` getoetst; alleen booleans genoteerd)
- BIO2: 8.24, 8.10
- Beschrijving: naast D-1 hebben `sops-sandbox/sops-key.txt` (twee commits op `origin/main`), `operations-manager/python/sources/agekey.txt` (vier commits, in augustus 2026 als "ingetrokken ontwikkelsleutel" verwijderd, nooit recipient van een SOPS-bestand) en testsleutels in `tests/e2e/testserver.py` en `tests/test_sops_skip_unchanged.py` in de historie gestaan. De huidige boom van `main`, `origin/main` en `forgejo/main` is schoon (de scanner zegt CLEAN over 2730 van 2882 bestanden), maar 537 van de 548 lokale refs, waaronder alle `refs/remotes/forgejo-pr/*`, dragen nog minstens één geldige sleutel in hun boom en 10 refs de SSH-sleutel uit D-19. Een `git push --all` of `--mirror` naar een nieuwe remote neemt dat allemaal mee.
- Bewijs: `git log --all -S'AGE-SECRET-KEY-1' --name-only`: c185c6618 tot 5654c93ff (`sops-sandbox/sops-key.txt`), fd2f1263e tot 04ca3bc24 (`sources/agekey.txt`), 476ca593b en 24c1de34e tot f34f90c9f (tests); `git for-each-ref` plus `git grep` per ref (telling); `.gitignore:73` (`*agekey*.txt`, na het lek toegevoegd).
- Aanbeveling: per sleutel vaststellen of hij ooit recipient was van iets dat nog leeft (de sandbox-sleutel in het bijzonder) en dat vastleggen; oude branches en PR-refs op Forgejo opruimen; de historiescan uit D-20.

### D-23: Restpunten uit de repo-hygiëne (Laag)

- Het vaste sandbox-wachtwoord staat als literal op circa twintig plekken: `Taskfile.yaml:883,2197,2213,2741,2754,3072,3087,3125,3134,3148,3227`, `overlays/sandboxed-local/configmap.yaml:23,31,37,59,65`, `overlays/sandboxed-local/argocd-repo-forgejo-secret.yaml:15`, `archive/private-container-registries.md:161`. Sandbox-only en de host resolveert naar 127.0.0.1, maar het leert dat een wachtwoord in een Taskfile normaal is. Eén bron (`FIXED_PASSWORD` of een gegenereerde waarde in `security/`) volstaat.
- `docker-compose.dev.yaml:64` heeft een vaste `SECRET_KEY` voor lokale sessies; `.github/workflows/integration-tests.yaml:115` een dev-placeholder in een `DATABASE_URL`. Uit een lokaal `.env` halen.
- `operations-manager/python/opi/configs/keycloak/algoritmeregister.yaml:32,41,50,59`: vier korte wachtwoorden voor `@example.com`-testgebruikers. Testwaarde, acceptabel; wel opvallend kort als iemand het bestand ooit tegen een echt realm gebruikt.
- Lokaal (niet in de repo): `git remote get-url forgejo` bevat een token in de URL, dus in `.git/config` en in elke terminal-log. Credential helper of `~/.netrc`.
- `tests/`: 96 credential-achtige literalen van 16+ tekens, allemaal placeholders of lage entropie; niets dat op een echte credential lijkt. `opi/configs/` bevat verder alleen env-verwijzingen en lege waarden.

## Open vragen

0. Zijn de 21 secrets uit D-1 sinds 24 september buiten git om vervangen (Keycloak-admin, Postgres-admin, MinIO-admin, ArgoCD-admin, TransIP, vault-init, `SECRET_KEY`, enz.)? In git is daar geen spoor van, en met GitOps zou dat spoor er moeten zijn. Zolang dit niet met een datum en een lijst is beantwoord, geldt D-1 als open incident.
1. Welke ClusterRole heeft `argocd-argocd-server` op ODCN, en dekt die `secrets: get` in alle namespaces? Dat bepaalt of D-7 van AANNEMELIJK naar BEVESTIGD gaat. Niet in deze repo; opvragen met `kubectl get clusterrolebinding -o wide | grep argocd-argocd-server` (read-only).
2. Wie heeft leesrecht op zad-projects, zad-deployments en zad-argo-user-applications op GitHub (organisatie, teams, externe collaborators)? De docs zeggen er niets over; het bepaalt de ernst van D-2 en D-5.
3. Is de PAT uit D-4/D-9 fine-grained en beperkt tot zad-deployments, of een classic token met `repo`-scope op de hele organisatie?
4. Is stap 8 van de rotatie van 24 september (`--remove-old-key`, opruimen van `pat_*.txt` en `old_key.txt`, intrekken van de oude token bij GitHub) uitgevoerd? De bestanden in `security/` suggereren van niet, maar dat is geen bewijs.
5. Staat er op dit moment een `.cmp-env` met plaintext in de productie-zad-deployments (D-2)? Eén `git ls-files '**/.cmp-env'` in een clone beantwoordt dat; zo ja, dan zijn die waarden als gelekt te beschouwen zolang de historie bestaat.
6. Is encryption-at-rest voor etcd op ODCN ingeschakeld? Elk `sops-age-key`-Secret en elk door ArgoCD aangemaakt Secret staat anders plat in etcd-backups.
7. Krijgen projectleden op ODCN kubectl-rechten in hun eigen namespace via Capsule (tenant owner)? Dan kunnen zij `sops-age-key` en alle andere Secrets in die namespace lezen. Dat is consistent met wat de UI al toont, maar hoort in het model te staan.
