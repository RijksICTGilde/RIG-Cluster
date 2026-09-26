# Deel E: injectie en invoervalidatie

Whitebox-onderzoek op code, alleen gelezen. Geen kubectl, geen git-wijzigingen, geen payloads uitgevoerd. Paden zijn relatief aan `operations-manager/python/` tenzij anders vermeld. Toetskader: BIO2 v1.3 8.28 (veilig coderen) en 8.26 (toepassingsbeveiligingseisen), OWASP injectie-klassen. Datum: 2026-09-26, branch `main` (e6c08b22c).

Zekerheid: BEVESTIGD betekent dat de keten van bron tot sink in de code is gelezen en er geen sanitatie tussen zit. AANNEMELIJK betekent dat een schakel (meestal: wie het veld in de praktijk mag zetten, of het gedrag van een externe component) niet in deze repo te lezen was.

## Onderzocht

- Subprocess-sinks: alle 15 genoemde modules plus een grep over heel `opi/` op `create_subprocess_shell|subprocess.run|shell=True|os.system|os.popen|shlex`. Resultaat: precies één shell-sink (`opi/connectors/kubectl.py:246`), de rest is argumentlijsten. `opi/core/metrics.py`, `opi/services/project_store.py` en `opi/web/router.py` bevatten zelf geen subprocess-aanroep. Nergens `kubectl exec`, `port-forward` of `cp`.
- Template-sinks: `opi/generation/manifests.py` (twee `Environment`-instanties), `opi/connectors/kubectl.py:333` (kale `jinja2.Template`), alle 39 templates in `manifests/`, de call sites in `opi/manager/*`, `opi/services/catalog/*`, `opi/manager/backup/*`, plus `tests/test_authwall_banner_yaml_injection.py`.
- Validatiemodel: `opi/core/project_schema.py`, `opi/schemas/project_v2.json`, `opi/manager/project_validation.py`, `opi/services/project_store.py`, `opi/core/git_monitor.py`, `opi/manager/project_manager.py` (`process_project_from_git`, `save_and_commit_project`), `opi/api/router.py`, `opi/api/validation.py`, `opi/api/endpoint_util.py`, `opi/middleware/authorization.py`, `opi/web/router_wizard.py`.
- YAML-parsing en uploads: grep op `yaml.load|full_load|unsafe_load|Loader=|typ=|YAML(`, `pickle|marshal|eval(|exec(|__import__|import_module|Template(|.format(`; `opi/utils/yaml_util.py`, `opi/services/upload_staging.py`, `opi/web/router_attachments.py`, `opi/web/router_wizard_attachments.py`, `opi/api/v2/router.py` (FILE-velden), `opi/api/image_router.py`, `opi/services/catalog/attachments/*`.
- SQL: `opi/connectors/postgres.py`, `opi/manager/database_manager.py`, `opi/core/database_pool(s).py`, `opi/services/postgres_scope.py`, `opi/manager/clone_validation.py`, `opi/manager/db_console_manager.py`, `opi/core/tracing.py`, `opi/migrations/`.
- Padopbouw: `opi/services/project_store.py`, `opi/handlers/project_file_handler.py`, `opi/services/config_location.py`, `opi/services/upload_staging.py`, `opi/services/persistence/`, `opi/connectors/git.py`, `opi/manager/project_manager.py` (helm-charts, helmfile), `opi/api/restore_router.py`, `opi/api/backup_router.py`, `opi/web/lotc_router.py`, `opi/utils/project_names.py`, `opi/utils/naming.py`.
- SSRF en uitgaand: `opi/connectors/http.py`, `opi.py`, `skopeo.py` plus `opi/services/registry.py` en `features/image-registries.md`, `subdomain.py`, `transip.py`, `prometheus.py`, `grafana_prometheus.py`, `mail.py`, `chisel_connector.py`, `opi/core/federation_service.py`, `opi/manager/keycloak_manager.py`, `opi/connectors/keycloak.py`, `opi/web/metrics_explorer_router.py`, `opi/web/router_usage.py`, `opi/api/invite_routes.py`.
- Overig: `opi/utils/logging_config.py`, `opi/core/i18n.py`, `opi/forms/i18n.py`, regexes op user-invoer, resource-plafonds (`opi/core/cluster_config.py`, `opi/forms/editables/validators.py`), LimitRange/ResourceQuota in `manifests/`, `infrastructure/`, `bootstrap/`.

## Validatiemodel zoals het in de code staat

**Waar het projectbestand gevalideerd wordt.**

1. `opi/core/project_schema.py:221` `validate_project_schema(data, schema_version=None)`: JSON Schema Draft 2020-12 tegen `opi/schemas/project_v2.json` (of een legacy-patch voor een oudere versie). De validator wordt gebouwd op regel 157 als `Draft202012Validator(schema)` **zonder `format_checker`**. Gevolg: `"format": "email"` op `users[].email` (schema regel 74) en `"format": "date-time"` worden niet afgedwongen; alleen `pattern`, `enum`, `type`, `maxLength`, `additionalProperties` werken.
2. `opi/manager/project_validation.py:875` `validate_project_structure(data, previous)`: referenties, uniciteit, gereserveerde namen, service-configs tegen hun pydantic-model (`validate_service_configs`, regel 415), RC-168 setting-latitude.
3. `opi/services/project_store.py:673` `_validate(data, enforce, previous)` draait beide en is de chokepoint voor `create` (regel 369), `save` (regel 459, elke poging opnieuw) en `mutate` (regel 528). Met `enforce=False` (elf aanroepers: auto-tune, oom-watcher, restore, keycloak, mail, auto-migrate) wordt een schema- of structuurfout **alleen gelogd** (regel 705) en gaat de write door; alleen plaintext waar AGE verplicht is blijft geweigerd (regel 697-703).
4. `opi/manager/project_manager.py:1898` `save_and_commit_project` is het enige geschreven pad vanuit managers en routeert naar `store.save`.
5. Git-pad: `opi/core/git_monitor.py:143` `validate_declared_project_schema(content)` **vóór** migratie, tegen de versie die het bestand zelf declareert; een afwijzing stopt de verwerking en logt op ERROR (regel 150). `process_project_from_git` (`project_manager.py:3044`) valideert `current_yaml` **na** de in-memory migratie van `read_project_file` en vóór de auto-migrate-save (die met `enforce_validation=False` draait, regel 3065). Dat is conform de geheugennotitie "valideer NA `migrate_to_latest()`".
6. Cache-pad: `project_store.py:832` `_read_committed` parset en migreert (`migrate_to_latest`, regel 842) maar **valideert niet**; `_reload_changed_into_cache` (regel 1069) en `load_project_from_data` zetten het bestand ongevalideerd in de cache. Wie daaruit rendert zonder `process_project_from_git` (`opi/manager/db_console_manager.py:224,354`, `opi/manager/job_manager.py:98,238`) werkt dus met ongevalideerde data als het bestand rechtstreeks in git is aangepast.
7. Wizard: `opi/web/router_wizard.py:2305` valideert het complete bestand vlak voor de store, expliciet dezelfde regels als de store. API: `/api/`-routes slaan de sessie-middleware over (`opi/middleware/authorization.py:93`) en zijn beveiligd met `@validate_api_token` (`opi/api/endpoint_util.py:66-74`), dat de `project_name` uit de URL vervangt door de canonieke naam uit de store. Bodyvelden gaan door `validate_api_payload` met dezelfde `Editable`-validators als de formulieren (`opi/api/validation.py:116`), en daarna door `save_and_commit_project`.

**Welke regels het schema afdwingt (project_v2.json).** Injectie-uitsluitende patterns op: `name` (regel 11, `^[a-z][a-z0-9-]*\Z`, max 30), component/deployment `name`, `cluster`, `namespace` (230, 295-297, DNS-1123), `image` (232, 348), `component-path.match/rewrite` (151-152), `env-vars` van een deployment-component (366-369: sleutel `^[A-Za-z_][A-Za-z0-9_]*\Z`, waarde zonder CR/LF/NUL), `repository.url` (95, geen whitespace of `"`), `repository.path` (102, geen control chars), `attachment id` (138), `command` (276: max 10 x 4096). Alle patterns zijn `\Z`-anchored, geen geneste kwantoren.

**Zonder pattern (vrije tekst)**: `display-name`, `description` (12-13, gaan nergens een template in), `resources.cpu/memory` (160-188, alleen `string`), `repository.name/username/branch` (94-98), `repository.password` (mag `plain:`), `users[].email` (format niet afgedwongen), `user-env-vars` in objectvorm (257, 363: kaal `object`), `aliases` objectvorm (283), `helm-values` (leeg schema), `helm-chart.name/git-url/git-ref/chart-path` (497-510), `helmfile.ref/path/files` (518-530), `remote-source-service.host/database/schema/bucket` (558-573), `deployment-service.config` (bewust open, per-service pydantic-model). Voor de service-configs geldt: sommige modellen hebben strakke patterns (`cross_domain_access`, `image_registries`, `attachments`), andere expliciet niet (`opi/services/catalog/shared/postgres.py:85-108`: "Not pattern-constrained", `metrics_scraper/config_model.py:27`).

**Conclusie over het model.** De architectuur is goed: één chokepoint, fail-closed op de gebruikerspaden, validatie na migratie, het git-pad expliciet als vijandige bron behandeld. De zwakte zit in de dekking: het schema beschermt alleen waar een pattern staat, en de manifest-templates vertrouwen daar impliciet op. Voor de velden zonder pattern is er geen tweede laag op de sink (zie de templatetabel), en de `format`-checker staat uit.

## Sterke plekken

- Geen enkele `shell=True` behalve `kubectl.py:246`; overal argumentlijsten. Geen `kubectl exec`/`port-forward`. `-c` in `kubectl logs` is de constante `APPLICATION_CONTAINER_NAME` (`kubectl.py:975`).
- WebSocket-logs (`opi/api/logs_websocket_router.py:332`): Origin-check, sessie, allowlist, `is_user_authorized_for_project`, `_pod_belongs_to_component` ook bij `switch` (regel 925), `lines` via `Query(ge=1, le=1000)`.
- Postgres: één connector, elke identifier door `_validate_identifier` (`postgres.py:123`, `^[a-zA-Z_][a-zA-Z0-9_$]*$`, max 63, reserved words) én `_quote_identifier` (314); lookups met `$1`; `PGPASSWORD` in env; `_validate_in_cluster_host` (382), poort-allowlist `{5432}`, `_validate_pg_password_safe`; pg_dump-pipeline zonder shell (1968-1983). Geen geïnterpoleerde SQL buiten `postgres.py`, geen ORM raw queries.
- YAML: nergens PyYAML `load`/`FullLoader`/`UnsafeLoader` of ruamel `typ='unsafe'`; overal ruamel round-trip of `safe`. Geen `eval`, `exec`, `pickle`, `marshal`, dynamische imports. Manifest-templates komen van schijf, gebruikersdata gaat er als variabele in, nooit als templatetekst.
- Templates: `yaml_scalar` (`manifests.py:38-56`, ruamel-emitter met `default_style='"'`) is een correcte quoting-filter. `deployment.yaml.jinja` gebruikt `tojson` voor `env_vars`, `command`, storage-namen/mount-paths, `host_aliases`, attachment-mounts, labels. `sidecar-authorization-wall.yaml.jinja` is volledig op `tojson` en heeft regressietests. `ingress.yaml.jinja` dekt de nginx-snippet-vectoren dubbel (schema-pattern 151-152 en `_SAFE_PATH_PATTERN`, `project_file_handler.py:172`).
- Skopeo: authfile `O_EXCL|0600` in `TemporaryDirectory` (`skopeo.py:55-63`), `_mask_userinfo` op stderr, `_destination_refused` (74-88) weigert niet-globale adressen met uitleg waarom niet `is_private`; de push-bestemming is altijd `settings.REGISTRY_URL` (215, 305), platform-credentials gaan nooit naar een user-host.
- age/sops: private key via `NamedTemporaryFile` (0600) of `SOPS_AGE_KEY` in env, nooit op argv. `git build_commit`: eigen `GIT_INDEX_FILE` uit `mkstemp`, content via `hash-object --stdin`, `--` bij paden (`git.py:966,1600,1687,1722`).
- Paden: `validate_api_token` vervangt de URL-naam door de canonieke naam; `ProjectStore` gebruikt `os.path.basename` en `mutate` eist bestaan; staging-token, versietoken en flow-id zijn strikte hex-regexes (`upload_staging.py:36-41`, `project_store.py:94`, `session.py:40`); `/bg/{slug}` heeft een allowlist. `_parse_git_url` weigert `file://`. Attachment-filename weigert `/`, `\`, `.`, `..` (`catalog_model.py:97-105`).
- Upload-staging: uuid4-token, dir 0700, bestanden 0600, TTL-sweep. Image-upload (`image_router.py:77-91`) streamt chunked met limiet tijdens het lezen: het referentievoorbeeld.
- Mail: geen eigen SMTP-headers (JSON naar Stalwart), `_NAAM_VERBODEN` met control chars, `\A`/`\Z`-anchoring met uitleg (`mail.py:126-130`), domein-allowlist. Metrics-explorer: selectors zijn constanten, admin-only. Federatie: peers alleen uit `FEDERATION_PEERS`, routes achter master key. Geen webhook-/callback-URL's in schema of config.
- Log-hygiene: `sanitize_for_log` (`api/router.py:44`) en `_describe_violation` (`project_schema.py:191`) laten bewust de waarde weg; `logging_redact` voor headers.
- Restore-routes: `_require_namespace_owned_by_project` (`restore_router.py:68-92`) sluit cross-tenant toegang af; kopia `snapshot delete` alleen na lookup plus tagcontrole (`kopia.py:636`, `backup_router.py:908-920`).

## Bevindingen

### E-1. YAML-injectie in de ArgoCD app-of-apps repo via `repositories[].username`, `.password` en `.branch`

- **Ernst**: Hoog. **Zekerheid**: BEVESTIGD (bron tot sink, geen sanitatie). Cluster-brede escalatie AANNEMELIJK (AppProject-gedrag niet uitgevoerd).
- **BIO2**: 8.28 (veilig coderen: output encoding), 8.26 (toepassingsbeveiligingseisen), 8.2 (beheer van speciale toegangsrechten: ArgoCD-controller).
- **Bron tot sink**: projectbestand `repositories[]`. Schema `project_v2.json:94-98`: `name`, `username`, `branch` kaal `string`; `password` mag `plain:` (regel 65). Wie: iedereen met schrijfrecht op `zad-projects` (git-pad, gevalideerd door `git_monitor` en `process_project_from_git`, maar het schema laat deze waarden door). Een UI-pad bestaat als formuliermodel (`opi/forms/models/project_file.py:400-437`: `username`, `password`, `branch` als tekstvelden zonder validator), maar ik vond geen route die dit model rendert; de wizard seedt `repositories` uit een template (`router_wizard.py:431`). Sink: `opi/manager/argo_manager.py:318-320` (`username = repository.get("username")`, `password = repository.get("password")`), na decrypt ongewijzigd in de dict (345-346), gerenderd door `manifests/argo-repository-https.yaml.jinja:13-16` (`url: {{ repository_url }}`, `name: {{ name }}`, `username: {{ username }}`, `password: {{ password }}`, alle kaal) naar een `.to-sops.yaml` in `zad-argo-user-applications`; SOPS versleutelt waarden maar behoudt structuur. Zelfde voor `branch`: `argo_manager.py:636,695,871` `target_revision=repo_info.get("branch", "main")` naar `manifests/argocd-application.yaml.jinja:20` `targetRevision: {{ targetRevision }}` (kaal, ongesopst).
- **Bewijs**: `manifests/argo-repository-https.yaml.jinja:15-16` `username: {{ username }}` / `password: {{ password }}`; `manifests/argocd-application.yaml.jinja:19-21` `repoURL: {{ repoURL }}` / `targetRevision: {{ targetRevision }}` / `path: {{ repoPath }}`; `opi/generation/manifests.py:260-262` schrijft de gerenderde tekst rauw naar disk (geen parse-en-herdump); `bootstrap/rig-system/kustomize/overlays/odcn-production/argocd-application-user-applications.yaml:10` `project: default` met prune en selfHeal. Vergelijk `manifests/argo-repository.yaml.jinja`, dat dezelfde waarden wel via `tojson` rendert.
- **Aanvalsscenario**: `branch: "main\n---\napiVersion: rbac.authorization.k8s.io/v1\nkind: ClusterRoleBinding\n..."` (of `username`) levert een extra document in de app-of-apps repo op. Die repo wordt gesynchroniseerd door de Application `user-applications` in AppProject `default`, die standaard elke destination en elk resource kind toestaat; de ArgoCD-controller draait cluster-admin. Wie zad-projects mag schrijven, wordt daarmee cluster-admin. Zonder `---` volstaan extra keys in het repository-Secret (`insecure`, `project`).
- **Aanbeveling**: beide templates volledig op `| yaml_scalar` (of `tojson`, zoals `argo-repository.yaml.jinja` al doet); patterns op `repository.name`, `username`, `branch` in het schema (bijv. `^[A-Za-z0-9._/@-]+\Z`) en het ontsleutelde wachtwoord weigeren bij `\r`/`\n`. Los daarvan: `user-applications` uit AppProject `default` halen naar een AppProject met `clusterResourceWhitelist: []` en een namespace-lijst; dat maakt deze klasse fouten niet-escalerend.

### E-2. YAML-injectie in de CNPG `Cluster`-CR via de dedicated-Postgres serviceconfig

- **Ernst**: Hoog. **Zekerheid**: BEVESTIGD.
- **BIO2**: 8.28, 8.26.
- **Bron tot sink**: service `postgresql-database` (scope `project`) of `namespace-postgresql-database`, velden `storage`, `image`, `resources.requests/limits.cpu/memory`, `postInitSQL[]`. Model `opi/services/catalog/shared/postgres.py:60-61, 85-108`: `str` zonder pattern (regel 105 letterlijk "Not pattern-constrained"). Wie: projectlid via de service-config API (`PUT /api/v2/projects/{p}/services/.../config`, API-key) of het servicedeel van de wizard. `validate_service_configs` valideert tegen dit model, dus het schema laat de waarde door. Sink: `opi/manager/project_manager.py:2528-2534` naar `manifests/postgresql-cluster.yaml.jinja` regel 19 `imageName: {{ database_config.image }}`, 35 `size: {{ database_config.storage }}`, 41-45 `memory: {{ database_config.resources.requests.memory }}` enz., 58 `- {{ sql }}`; allemaal kaal; naar `zad-deployments`, ArgoCD past toe.
- **Bewijs**: `manifests/postgresql-cluster.yaml.jinja:35` `size: {{ database_config.storage }}`; `:58` `- {{ sql }}`; `opi/services/catalog/shared/postgres.py:105` commentaar "Not pattern-constrained".
- **Aanvalsscenario**: `storage: "1Gi\n  hostNetwork: true\n  #"` injecteert siblings in de `Cluster`-spec; een `postInitSQL`-item met newline injecteert op documentniveau, inclusief een `---` tweede document. Begrenzing: de per-project AppProject (`manifests/argocd-appproject.yaml.jinja:16-20`) pint `destinations.namespace` en zet `clusterResourceWhitelist: []`, dus alleen namespaced resources in de eigen namespace. Dat is nog steeds elk Secret, Role, RoleBinding, Pod-spec of NetworkPolicy in die namespace, buiten wat OPI en de goedkeuringen toestaan (bijv. de tenant-baseline netpol overschrijven, of een pod met `hostPath` als PSA dat toelaat).
- **Aanbeveling**: velden via `| yaml_scalar` renderen en quantity-/charset-patterns in het pydantic-model (`^[0-9]+(\.[0-9]+)?(m|[EPTGMK]i?)?\Z` voor quantities, image-pattern zoals schema regel 232, `postInitSQL` beperken tot een regex zoals `^CREATE EXTENSION IF NOT EXISTS [a-z0-9_-]+;?\Z` of expliciet in het trust-model opnemen, zie E-17).

### E-3. Manifest-injectie in direct toegepaste restore/backup-pods via API-body en URL-padsegmenten

- **Ernst**: Hoog. **Zekerheid**: BEVESTIGD (geen sanitatie); impact begrensd tot de eigen namespace door de ownership-check.
- **BIO2**: 8.28, 8.26.
- **Bron tot sink**: `opi/api/restore_router.py`: `DatabaseRestoreRequest` (281-305: `target_db_host/name/user/password`), `BucketRestoreRequest` (367-388), `RestoreRequest.storage_size/storage_class` (137-138), alle `str`/`str | None` zonder pattern; `reference_name` en `pvc_name` zijn ongevalideerde URL-padsegmenten (337, 420, 658). Wie: houder van de project-API-key (projectlid). Route: `database_backup._create_database_restore_pod` (~605), `bucket_backup`, `pvc_backup._create_project_restore_pvc` (~1066), gerenderd met de kale `jinja2.Template` uit `opi/connectors/kubectl.py:333` (`_template_manifest`, `opi/manager/backup/base.py:574`) en toegepast met `kubectl apply -f -` door OPI's eigen serviceaccount (`database_backup.py:~634`).
- **Bewijs**: `manifests/restore-database-pod.yaml.jinja:239-243` `value: "{{ target_db_host }}"` / `value: "{{ target_db_name }}"` (naive quotes); `manifests/restore-target-pvc.yaml.jinja:25,29` `storageClassName: {{ storage_class_name }}` / `storage: {{ size }}` (kaal); `restore-bucket-pod.yaml.jinja` en `restore-pod.yaml.jinja` idem voor `reference_name`/`snapshot_id`/`target_*`. Op ODCN gaat de tekst nog door `apply_rules_to_document` (`opi/services/catalog/image_registries/manifest_pass.py:126-134`), maar bij een parsefout gaat de rauwe tekst door.
- **Aanvalsscenario**: `storage_class` = `x\n  hostPath:\n    path: /\n  #` of `target_db_host` met een newline injecteert siblings of een extra document in een pod/PVC die OPI direct toepast. `_require_namespace_owned_by_project` (68-92) houdt het in de eigen namespace en de pod draait onder het project-serviceaccount, dus geen cluster-admin-escape, wel door de aanvaller gekozen pod-inhoud (extra volume, extra container met een andere image) buiten de goedkeuringen om.
- **Aanbeveling**: alle client-geleverde restore-velden via `| tojson` in de templates; patterns op de pydantic-velden (host als DNS-naam, `storage_size` als quantity, `storage_class` en namen als DNS-1123) en op de URL-padsegmenten; `_template_manifest` op dezelfde `Environment` met `yaml_scalar` laten draaien als `manifests.py`.

### E-4. `kubectl` met stdin loopt door `sh -c` met een heredoc; secrets staan in de argv van de shell

- **Ernst**: Midden. **Zekerheid**: BEVESTIGD.
- **BIO2**: 8.28, 8.24 (cryptografie: sleutelbeheer), 8.15 (logging: geen geheimen in argv-capture).
- **Bron tot sink**: `opi/handlers/sops.py:214` (`full_key_contents = ... {private_key}`) en `:236` `apply_manifest(...)`; `keycloak_manager.py:2268` (client-secret), `mail_manager.py:379` (`"password": password`), `run_support.py:164`, alle backup-manifests. Sink `opi/connectors/kubectl.py:239-246`.
- **Bewijs**: `kubectl.py:241` `shell_cmd = f"{cmd_str} <<'EOF'\n{stdin_input}\nEOF"`; `:246` `process = await asyncio.create_subprocess_shell(shell_cmd, ...)`; `:231` `cmd_args_str = " ".join([f'"{arg}"' if " " in arg else arg for arg in args])` (alleen quoting bij een spatie). Vergelijk `opi/connectors/minio_mc.py:157-166`, dat exec plus pipe gebruikt.
- **Waarom nu niet direct exploiteerbaar**: de args op dit pad zijn constant (`apply -f -`, `-n <schema-gevalideerde namespace>`); een `EOF` op kolom 0 in de body zou de rest als shell uitvoeren, maar alle user-tekst die dit pad bereikt is `tojson` (`job-pod.yaml.jinja:38,46`), `indent(4, true)` of anderszins ingesprongen. De bescherming zit in de templates, niet in de connector; E-3 laat zien dat die templates niet allemaal dicht zijn.
- **Aanvalsscenario**: (a) de complete `sh -c`-string staat in `/proc/<pid>/cmdline`: de AGE-private key van elk project, Keycloak-clientsecrets en het mailwachtwoord zijn zichtbaar voor elk proces in dezelfde PID-namespace en voor argv-loggende monitoring. (b) Latent: één kale interpolatie van vrije tekst met regeleinden op kolom 0 in een template op dit pad (E-3 zit dichtbij: `restore-*` gaan via `kubectl apply`) is RCE als OPI-serviceaccount. (c) `MAX_ARG_STRLEN` (128 KiB) breekt grote manifesten met E2BIG.
- **Aanbeveling**: `create_subprocess_exec("kubectl", *args, stdin=PIPE)` met `communicate(input=...)`, exact zoals `minio_mc.py:159`.

### E-5. Externe clone-endpoints: OPI verbindt naar elke host:poort en geeft een onderscheidend foutantwoord (SSRF-orakel)

- **Ernst**: Midden. **Zekerheid**: BEVESTIGD.
- **BIO2**: 8.28, 8.26, 8.20 (netwerkbeveiliging), 8.22 (netwerksegmentering).
- **Bron tot sink**: `POST /api/projects/{p}/deployments/{d}/:clone-database-from-external` en `:clone-bucket-from-external` (`opi/api/router.py:2535, 2700`, `@validate_api_token`, projectlid). Body `sourceHost`, `sourcePort`, creds (`router.py:698-703`, geen validator). Sink: `opi/manager/database_manager.py:1768-1774` `asyncpg.connect(host=source_host, port=source_port, ...)`; `opi/manager/minio_manager.py:1462-1472` `mc alias set` naar `{source_host}:{source_port}`; `opi/connectors/chisel_connector.py:100-116` `chisel client --auth user:pw <serverUrl>` met `serverUrl` uit `clone_data.tunnel` (`router.py:2629, 2792`).
- **Bewijs**: `database_manager.py:1796-1804`: drie verschillende fouten, `f"Source database '{source_database}' does not exist at {source_host}:{source_port}"`, `f"Authentication failed for {source_username}@{source_host}:{source_port}"`, `f"Failed to connect to external source ...: {e!s}"`. Ter vergelijking heeft de interne clone wel `_validate_in_cluster_host` (`postgres.py:382`), de externe route niet.
- **Aanvalsscenario**: een projectlid richt de call op `rig-db-rw.rig-system.svc:5432`, Keycloak of MinIO en gebruikt de drie fouten als orakel: poort open of dicht, wachtwoord goed of fout, database bestaat of niet. Poortscan en wachtwoordraden op andermans databasegebruikers vanuit de OPI-pod, zonder rate-limit. Chisel maakt bovendien een uitgaande websocket naar een willekeurige URL (blinde SSRF met de creds van de aanroeper).
- **Aanbeveling**: dezelfde toets als `skopeo._destination_refused` op `sourceHost` en `serverUrl` (of een allowlist per cluster); één generieke fout naar de client, details in de log; een limiet op externe-clone-pogingen per project.

### E-6. Helm-chart-verwerking: `rmtree`/`copytree` op ongevalideerde padcomponenten, ook uit een externe repo

- **Ernst**: Midden. **Zekerheid**: BEVESTIGD (geen sanitatie); bron is git-only, wat de praktische ernst drukt.
- **BIO2**: 8.28, 8.26, 8.30 (uitbestede ontwikkeling / supply chain).
- **Bron tot sink**: `helm-charts[].name` en `.chart-path` (schema `project_v2.json:497-510`, geen pattern; geen API- of formulierpad gevonden, dus alleen via git) en `dependencies[].name`/`repository: file://...` uit de `Chart.yaml` van de door de gebruiker opgegeven chart-repo. Sink `opi/manager/project_manager.py:4445-4481`.
- **Bewijs**: `:4445` `source_chart_path = os.path.join(temp_dir, chart_path)` (een absolute `chart_path` overschrijft `temp_dir`); `:4455-4458` `dest_chart_path = os.path.join(charts_dir, chart_name)` ... `shutil.rmtree(dest_chart_path)` ... `shutil.copytree(source_chart_path, dest_chart_path)`; `:4474-4481` `dep_source = os.path.join(temp_dir, chart_path, rel_path)` ... `dep_dest = os.path.join(charts_dir, dep_name)` ... `rmtree`/`copytree`. Geen `realpath`/`is_relative_to`.
- **Aanvalsscenario**: (1) `chart-path: /var/run/secrets/kubernetes.io/serviceaccount` met `name: sa`: de SA-token van OPI wordt gekopieerd en gecommit naar `zad-deployments`. (2) Supply chain zonder toegang tot zad-projects: de eigenaar van een legitiem geconfigureerde chart-repo zet `dependencies: [{name: "../../../../tmp/x", repository: "file://../../.."}]` in `Chart.yaml`; `rmtree` en `copytree` lopen buiten `charts_dir` op het OPI-bestandssysteem.
- **Aanbeveling**: `chart_name`, `dep_name`, `rel_path` afdwingen als één padsegment zonder `/`, `..` of leidende `.`; `source`/`dest` na `os.path.realpath` toetsen met `is_relative_to(temp_dir)` resp. `is_relative_to(charts_dir)`; `git-url` en `chart-path` een schema-pattern geven zoals `repository.path` al heeft (regel 102).

### E-7. Upload wordt volledig in geheugen gelezen voordat de groottelimiet geldt

- **Ernst**: Midden. **Zekerheid**: BEVESTIGD.
- **BIO2**: 8.26, 8.6 (capaciteitsbeheer), 8.28.
- **Bron tot sink**: multipart body van `POST /forms/wizard/{flow}/attachments/stage` (elke SSO-gebruiker met wizard-sessie) en van de v2 FILE-velden (`opi/api/v2/router.py:3730-3732`, API-key). Sink: `await file.read()` zonder limiet, daarna pas `len(raw) > MAX_ATTACHMENT_BYTES` (64 KB).
- **Bewijs**: `opi/web/router_wizard_attachments.py:237-238` `raw = await file.read()` / `if len(raw) > MAX_ATTACHMENT_BYTES:`; `opi/services/catalog/attachments/api.py:237` idem voor de API. Geen app-brede body-limiet; sandbox-ingress `proxy-body-size: "0"` (`bootstrap/rig-system/kustomize/operations-manager/overlays/sandboxed-local/ingress.yaml:7`), de odcn-overlay heeft geen body-size-annotatie.
- **Aanvalsscenario**: een multipart van enkele GB; Starlette spoolt naar schijf maar `read()` haalt alles als één `bytes` in het geheugen. OPI wordt OOMKilled (bekend pijnpunt, zie geheugennotitie kubectl-uitwaaiering). Herhaalbaar en goedkoop.
- **Aanbeveling**: chunked lezen met teller en afbreken bij `MAX_ATTACHMENT_BYTES + 1`, zoals `image_router.py:80-91`; body-limiet op de OPI-ingress in productie.

### E-8. YAML-injectie via `components[].resources` in `deployment.yaml.jinja` (naive quotes, geen schema-pattern)

- **Ernst**: Midden. **Zekerheid**: BEVESTIGD voor het git-pad; via API en wizard door Editables geblokkeerd.
- **BIO2**: 8.28, 8.26.
- **Bron tot sink**: `resources.requests/limits.cpu/memory` (schema 160-188, alleen `string`). `project_file_handler._parse_resources_block` (160-165) doet alleen `str(...)`; regel 349 "an unparsable quantity is not ours to judge here". `project_manager.py:6192-6195` naar `manifests/deployment.yaml.jinja:182-186` `memory: "{{ resources_requests_memory }}"` (naive quotes: een `"` in de waarde breekt eruit). Wie: op de API (`UPSERT_DEPLOYMENT_VALIDATORS`, `api/validation.py:66-67, 86-87`) en in de wizard gaan `cpu_limit` door `AllowedValuesValidator(["500m","1"])` en `memory_limit` door `parse_k8s_memory_to_mi` (`resource_analyzer.py:46`, `^(\d+(?:\.\d+)?)\s*([A-Za-z]*)$` na `strip()`), die een newline-payload afwijzen. Via git (schrijfrecht op zad-projects) is er geen rem: de store valideert alleen het schema.
- **Bewijs**: `manifests/deployment.yaml.jinja:182` `memory: "{{ resources_requests_memory }}"`; `project_v2.json:160` `"cpu": { "type": "string" }`.
- **Aanvalsscenario**: `memory: '64Mi"\n          hostPath:\n            path: /\n            #'` in git injecteert siblings in de container-spec (eigen namespace via ArgoCD). Zelfde begrenzing als E-2.
- **Aanbeveling**: `resources_*` via `| yaml_scalar`; quantity-pattern op de vier velden in het schema, zodat de rem niet alleen in de Editables zit.

### E-9. Sleutels in `generic-secret.yaml.to-sops.jinja` staan kaal; de objectvorm van `user-env-vars` valideert de sleutel niet

- **Ernst**: Midden. **Zekerheid**: BEVESTIGD voor het git-pad; tekstvorm (wizard, API) is veilig.
- **BIO2**: 8.28.
- **Bron tot sink**: `user-env-vars` in objectvorm (schema 257, 363: kaal `object`, geen `propertyNames`). `opi/utils/env_vars.py:169-171`: `if isinstance(env_vars_text, CommentedMap): return dict(env_vars_text)` zonder keycontrole; de tekstvormen valideren wel (`:109` en `:214`, `^[A-Za-z_][A-Za-z0-9_]*$`). Sink `manifests/generic-secret.yaml.to-sops.jinja:31,35,39` `{{ key }}: ...` (kaal; de waarde gaat wel door `yaml_scalar`).
- **Aanvalsscenario**: een sleutel `x\n  injected: value` via git; extra keys in het Secret (SOPS versleutelt values, niet de structuur die ksops teruggeeft).
- **Aanbeveling**: `{{ key | yaml_scalar }}` in het template; `propertyNames`-pattern in het schema voor de objectvorm, of keyvalidatie in de `CommentedMap`-tak.

### E-10. `users[].email` staat kaal in een block scalar en `format: email` wordt niet afgedwongen

- **Ernst**: Midden. **Zekerheid**: BEVESTIGD.
- **BIO2**: 8.28, 8.26, 5.15 (toegangsbeveiliging: de db-console-allowlist).
- **Bron tot sink**: `users[].email` (schema 74, `format: email`), validator zonder `FormatChecker` (`project_schema.py:157`). Wie: projectlid dat teamleden beheert (formulier heeft `EmailValidator`, `^[^@\s]+@[^@\s]+\.[^@\s]+$`, die geen whitespace toelaat; het git-pad en elke route zonder die Editable niet). Sink `opi/manager/db_console_manager.py:436` `emails = sorted({u.email.lower() ...})` naar `manifests/db-console-emails-configmap.yaml.jinja:16-19`, block scalar `emails.txt: |` met kale `{{ email }}` per regel, toegepast via `apply_bundle`.
- **Aanvalsscenario**: een e-mail met newline en passende indentatie breekt uit het block scalar; belangrijker: `emails.txt` is de `--authenticated-emails-file` van de db-console, een extra regel verruimt de allowlist.
- **Aanbeveling**: `Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)` en een `pattern` op `email` in het schema; de bron valideren is hier beter dan het block scalar quoten.

### E-11. `metrics-scraper` `path` naive gequote in een pod-annotatie

- **Ernst**: Midden (beperkt tot annotaties). **Zekerheid**: BEVESTIGD.
- **BIO2**: 8.28.
- **Bron tot sink**: `MetricsScraperConfig.path` (`opi/services/catalog/metrics_scraper/config_model.py:27`, `str | None`, geen pattern; `health_check` heeft er wel een, regel 49). Projectlid via serviceconfig. `metrics_scraper/__init__.py:101` naar `manifests/deployment.yaml.jinja:39` `prometheus.io/path: "{{ metrics_config.path ... }}"`.
- **Aanvalsscenario**: `path` = `/m"\n        injected.io/x: "y` voegt een annotatie toe aan de pod-template (bijv. een annotatie die een admission-webhook of controller interpreteert).
- **Aanbeveling**: `| tojson` en een pad-pattern in het model.

### E-12. Keycloak: externe of wildcard redirect URIs en webOrigins zijn door een projectlid te zetten

- **Ernst**: Midden. **Zekerheid**: BEVESTIGD.
- **BIO2**: 8.26, 8.5 (beveiligde authenticatie), 5.15.
- **Bron tot sink**: `additional_redirect_uris: list[str]` (`opi/services/catalog/keycloak/config_model.py:168`), via API zonder validatie en via formulier alleen `UrlValidator` (`validators.py:353-359`: `startswith("http://")`/`"https://"`) naar `keycloak_manager.py:1119-1128` en `keycloak.py:704-716` (`redirect_uris_set.add(uri)`, `web_origins_set.add(origin)`). `additional-clients` zonder `redirect-uris`: `keycloak_manager.py:2169` `redirect_uris = client_config.get("redirect-uris", ["*"])` en `keycloak.py:560-561` `"redirectUris": redirect_uris or ["*"], "webOrigins": web_origins or ["*"]`.
- **Aanvalsscenario**: een projectlid voegt `https://attacker.example/*` toe of maakt een extra client zonder redirect-uris; een phishinglink naar `/realms/<project>/protocol/openid-connect/auth?...&redirect_uri=https://attacker.example/` levert codes/tokens van de eindgebruikers van dat project op. Het gaat om het eigen realm, maar op het platform-Keycloak-domein.
- **Aanbeveling**: redirect URIs beperken tot de ingress-hosts van het project plus `http://localhost*`; `["*"]`-default vervangen door een verplicht veld; `webOrigins` nooit `"*"`.

### E-13. git-credentials in de clone-URL: in argv en in `.git/config`, geen `GIT_TERMINAL_PROMPT=0`

- **Ernst**: Laag. **Zekerheid**: BEVESTIGD.
- **BIO2**: 8.24, 8.15.
- **Bewijs**: `opi/connectors/git.py:316-318` `netloc = f"{username}:{password}@{parsed.hostname}"`; `:644-651` `clone_cmd = ["clone", ..., self.repo_url_with_path, "."]` naar `:413` `create_subprocess_exec(*cmd)`. `_obfuscate_git_command` (57-76) maskeert alleen de logregel. Grep op `GIT_TERMINAL_PROMPT|GIT_ASKPASS` in `opi/` en de Dockerfile: geen treffers. Bronnen: platform-tokens (`git.py:2170-2193`) en `repositories[].password` (2143-2151).
- **Aanbeveling**: credential via `GIT_CONFIG_COUNT`/`http.extraHeader` of `GIT_ASKPASS` uit env; `GIT_TERMINAL_PROMPT=0` in `cmd_env` (regel 386).

### E-14. kopia, chisel en mc krijgen wachtwoorden en access keys op argv

- **Ernst**: Laag. **Zekerheid**: BEVESTIGD.
- **BIO2**: 8.24, 8.15.
- **Bewijs**: `opi/connectors/kopia.py:350-362` `"--access-key", config.s3_access_key, "--secret-access-key", config.s3_secret_key, "--password", config.password` (ook 436-448, 516-528, 586-598); `chisel_connector.py:64` `self.auth = f"{username}:{password}"` en `:103-105` `"--auth", self.auth`; `minio_mc.py:270` `["alias", "set", alias, endpoint, access_key, secret_key]`, `:364` `["admin", "user", "add", alias, validated_username, secret_key]`. Elk van de drie leest een env-variant (`KOPIA_PASSWORD`/`AWS_*`, `AUTH`, `MC_HOST_<alias>`).
- **Aanbeveling**: env in plaats van argv, per tool zoals hierboven.

### E-15. Log-injectie via ongeauthenticeerde invite-routes; geen newline-escaping in de formatter

- **Ernst**: Laag. **Zekerheid**: BEVESTIGD.
- **BIO2**: 8.15 (logging), 8.28.
- **Bewijs**: `opi/api/invite_routes.py:222` `logger.debug(f"Invite key '{key}' not found in any project")`, `:490` `logger.warning(f"Invalid IDP alias '{idp_alias}' for invite '{key}'. Valid: {valid_aliases}")`; routes zonder auth (314-330). Formatter `opi/utils/logging_config.py:48` `%(message)s` zonder escaping, `opi`-logger op DEBUG (59). Starlette decodeert `%0A` in padsegmenten. `sanitize_for_log` bestaat (`api/router.py:44`) maar wordt hier niet gebruikt; `log_watcher.py:57-65` leest op regelniveau.
- **Aanbeveling**: een `logging.Filter`/Formatter die `\r`/`\n` in `record.getMessage()` vervangt (of JSON-logging); de invite-key in logregels inkorten (het is ook een geheim).

### E-16. Rolwachtwoord staat letterlijk in de DDL-tekst; asyncpg is geïnstrumenteerd

- **Ernst**: Laag. **Zekerheid**: BEVESTIGD voor de query-tekst; AANNEMELIJK voor de span-export (gedrag van de OTel-instrumentatie niet in deze repo).
- **BIO2**: 8.24, 8.15.
- **Bewijs**: `opi/connectors/postgres.py:665-666` `escaped_password = validated_password.replace("'", "''")` / `await conn.execute(f"ALTER USER {quoted_username} WITH PASSWORD '{escaped_password}'")` (ook `:511`); `opi/core/tracing.py:75` `AsyncPGInstrumentor().instrument()`, actief bij `OTEL_ENABLED=true` (`config.py:272`, default uit); server-side `log_statement=ddl` logt hetzelfde. Wachtwoorden zijn nooit gebruikersinvoer, dus geen injectie; het gaat om blootstelling.
- **Aanbeveling**: `ALTER ROLE ... PASSWORD 'SCRAM-SHA-256$...'` met een vooraf berekende verifier; of `SET log_statement = 'none'` in dezelfde sessie plus een span-sanitizer; minimaal documenteren dat OTEL niet aan mag met de huidige DDL.

### E-17. `postInitSQL` draait als CNPG-superuser zonder filter of approval

- **Ernst**: Laag (tenant-eigen cluster). **Zekerheid**: BEVESTIGD.
- **BIO2**: 8.26, 8.2.
- **Bewijs**: `opi/services/catalog/shared/postgres.py:108` `list[str]` zonder pattern; `database_manager.py:891, 996` naar `postgres.py:788-815` `conn.execute(sql)` met de superuser uit het infra-namespace (`database_manager.py:183`). Shared scope is afgeschermd (`database_manager.py:1483-1486`, `SharedScopeConfig` `extra="forbid"`).
- **Aanvalsscenario**: `COPY ... TO PROGRAM` geeft commando-uitvoering in de eigen CNPG-pod (SA-token van die pod). Binnen de tenantgrens; `privileges: SUPERUSER` is op hetzelfde pad al toegestaan. Los hiervan is dit veld de YAML-injectievector uit E-2.
- **Aanbeveling**: expliciet in het trust-model opnemen, of beperken tot `CREATE EXTENSION IF NOT EXISTS <naam>` met een regex.

### E-18. Database-console: rechten zijn die van de rol, geen statement-scope; PUBLIC CONNECT niet ingetrokken

- **Ernst**: Laag. **Zekerheid**: BEVESTIGD voor de fallback; AANNEMELIJK voor cross-database CONNECT (niet getoetst op een draaiende server).
- **BIO2**: 5.15, 8.3.
- **Bewijs**: `opi/manager/db_console_manager.py:386-397` verbindt als `{user}_ro` en valt bij ontbrekende ro-credentials terug op de read-write gebruiker (met warning). Grep op `REVOKE` in `postgres.py`, `infrastructure/`, `bootstrap/`, `images/`: geen `REVOKE CONNECT ON DATABASE ... FROM PUBLIC`. `postgres_scope.py` bepaalt alleen placement, geen consolerechten.
- **Aanbeveling**: `REVOKE CONNECT ... FROM PUBLIC` in `create_database`; de rw-fallback laten falen in plaats van degraderen.

### E-19. Geen ResourceQuota/LimitRange per namespace en geen `maxItems` op arrays

- **Ernst**: Laag. **Zekerheid**: AANNEMELIJK (DoS/kosten).
- **BIO2**: 8.6.
- **Bewijs**: per container is het plafond goed (`cluster_config.py:59-64`, 4096Mi/4000m; `api/validation.py:76-88` legt uit waarom de PATCH-route dat nu ook toetst). Maar `components`, `deployments`, `user-env-vars`, `allowed-subdomains` hebben geen maximum (schema 30-40, 368-369) en er is geen `LimitRange`/`ResourceQuota` in `manifests/`, `infrastructure/` of `bootstrap/`. Totale claim = componenten x deployments x plafond, onbegrensd.
- **Aanbeveling**: één `ResourceQuota`-template per projectnamespace; `maxItems` op de arrays.

### E-20. `_wizard_token` is een ongebonden bearer

- **Ernst**: Laag. **Zekerheid**: BEVESTIGD (code); uitbuiting vereist een tokenlek.
- **BIO2**: 8.5, 8.26.
- **Bewijs**: `opi/forms/wizard/session.py:213-232` `get_modal_state_by_token` laadt de state van elke geldige uuid4 zonder eigenaarschap te toetsen; stage/unstage/remove-routes doen alleen `@requires_sso`.
- **Aanbeveling**: user-id in de state opslaan en vergelijken bij `_load_state_by_token`.

### E-21. Usage-pagina zet de `namespace`-parameter letterlijk in een PromQL-regex (alleen platform-admin)

- **Ernst**: Laag. **Zekerheid**: BEVESTIGD.
- **BIO2**: 8.28.
- **Bewijs**: `opi/web/router_usage.py:116-120` retourneert `namespace` ongewijzigd; `:187, 222` `query_template.format(namespace_filter=...)`; `available_namespaces` (228) wordt berekend maar niet als allowlist gebruikt; route achter `require_platform_admin` (218). Elders (`prometheus.py`, `grafana_prometheus.py`, `router.py:949-1077`) komen namespace en pod uit schema-gepatternde namen, dus daar geen injectie; wel geen escaping op de sink zelf.
- **Aanbeveling**: waarden buiten `available_namespaces` weigeren; een `promql_quote`-helper op de sink.

### E-22. Info-bevindingen (geen actie vereist, wel benoemd)

- `opi/connectors/skopeo.py:74-88` `_destination_refused` resolvet vooraf met `getaddrinfo`; skopeo resolvet daarna zelf en volgt redirects. DNS-rebinding of een 30x naar een intern adres omzeilt de toets; alleen de eigen creds van de gebruiker gaan mee en het resultaat is booleaans. AANNEMELIJK, Info.
- `opi/connectors/git.py:997-1008` `get_absolute_file_path` heeft geen `is_relative_to`-guard; alle huidige callers geven een opgeschoond pad. Een centrale guard is goedkoop.
- `opi/connectors/postgres.py:1815-1890` gebruikt ongequote `{source_schema}`/`{target_owner}` in `psql -c` vóór het "stacked defense"-validatieblok op 1933-1947; beide publieke ingangen valideren eerder (1458-1470, 1636-1640), dus nu geen gat. Verplaats het blok naar de top en gebruik `_quote_identifier`.
- `opi/core/templates_lotc.py:190` `env.from_string(str(html))` (filter `process_components`) compileert een samengestelde string als template; alleen nog aangeroepen uit `templates_lotc/wizard/wizard_step.html.j2:45,59` en `project-form-demo/_formulier.html.j2:8`, geen route rendert die. Dood SSTI-patroon: verwijderen.
- `opi/utils/sops.py:309-313` `age-keygen -o <gettempdir>/age_key_<uuid4>` in plaats van `mkstemp`; `age-keygen` opent zelf met `O_EXCL` en 0600.
- `opi/api/logs_websocket_router.py:473` `f"app={deployment},{LABEL_RUN}"`: `deployment` is een ongevalideerde query-string, een komma voegt selector-termen toe; levert niet meer op dan pods in de eigen geautoriseerde namespace.
- Schema `repository.branch` (98) en `helmfile.ref` (522) zonder pattern en zonder `--` in `git.py:762, 782, 788, 838`; met de URL-schema's `https?|ssh|git` draait een `--upload-pack=` op de remote, niet lokaal. Git-only bron.
- `KubectlConnector.get_pod_image` (`kubectl.py:1193`, jsonpath-f-string met `container_name`) heeft geen aanroeper.
- `manifests/issuer-letsencrypt.yaml.jinja:7` `contact_email` kaal; alleen bereikbaar via clusterconfig (schema `config` heeft `additionalProperties: false` zonder `contact-email`).
- `manifests/argocd-application.yaml.jinja:21` `repoPath` bevat `repository.path`; het pattern (102) sluit newlines uit, maar `: ` of `#` kan het manifest breken (geen injectie, wel DoS van de eigen Application).
- `manifests/configmap.yaml.jinja` (sleep-mode): `title_template.format(...)` op een user-string (`sleep_mode/manifests.py:197-198`); formatstring is platform-eigendom, user-data alleen als waarde.

## Tabel: subprocess-aanroepen

| module:regel | commando | shell? | user-gestuurde argumenten | oordeel |
|---|---|---|---|---|
| kubectl.py:118, 157 | `kubectl auth whoami` | argv | geen | OK |
| kubectl.py:246 | `sh -c "kubectl <args> <<'EOF' ... EOF"` | **shell** | args: `-n <namespace>` (schema-pattern); stdin: complete manifesten incl. secrets | E-4 |
| kubectl.py:267 | `kubectl <args>` | argv | namespace, resourcenamen, `-l` selector (`logs_websocket_router.py:473` met query-param `deployment`) | OK / Info (E-22) |
| kubectl.py:739 | `sops --encrypt --age <pub> <file>` | argv | public key uit projectbestand | OK |
| kubectl.py:996 | `kubectl logs -f <pod> -c application -n <ns> --tail=N` | argv | pod/deployment/component (query, geautoriseerd), `lines` (1..1000) | OK |
| git.py:413 | `git clone/fetch/checkout/push/branch/ls-remote/log/show/...` | argv, `cwd` per repo | URL met `user:pass@` (git.py:318), branch/ref zonder pattern en zonder `--`, paden achter `--` | E-13, Info |
| kopia.py:208 | `kopia --version` | argv | geen | OK |
| kopia.py:281 (346-366, 430-452, 510-532, 580-602) | `kopia repository connect s3 --bucket .. --access-key .. --secret-access-key .. --password ..` | argv | bucket/prefix afgeleid van projectnaam; credentials platform op argv | E-14 |
| kopia.py:466, 547 | `kopia snapshot delete <id> --delete` | argv | `snapshot_id` na lookup en tagcontrole | OK |
| minio_mc.py:74, 159 | `mc --version` | argv | geen | OK |
| minio_mc.py:210 | `mc <args>` met stdin via pipe | argv+pipe | policy-JSON | OK (voorbeeld) |
| minio_mc.py:228 (270, 364, 404, 494, 540, 672, 713) | `mc alias set`, `mc admin user add`, `mc mb/rb/version` | argv | bucket/username/policy door `_validate_*` (91-146); secrets op argv | OK / E-14 |
| postgres.py:1817, 1833, 1844, 1863, 1877, 2052, 2075, 2091, 2120 | `psql -d <db> -c "<SQL met identifiers>"` | argv, `PGPASSWORD` env | db-/schema-/rolnamen (gevalideerd op de ingang) | OK / Info (volgorde) |
| postgres.py:1968, 1974, 1983 | `pg_dump | sed | psql` | Popen argv, OS-pipes | host (in-cluster allowlist), poort `{5432}`, creds via env | OK |
| skopeo.py:132 | `skopeo --version` | argv | geen | OK |
| skopeo.py:251 | `skopeo list-tags --authfile <tmp> docker://<repo>` | argv | repository, eigen creds van de gebruiker; `_destination_refused` | OK / Info |
| skopeo.py:314 | `skopeo copy --dest-authfile <tmp> docker-archive:<tar> docker://<REGISTRY_URL>/<ORG>:<tag>` | argv | tag via `validate_push_target`; bestemming uit settings | OK |
| cluster_config.py:1192 | `openssl x509 -hash -noout -in <cert>` | argv | geen (clusterconfig) | OK |
| handlers/sops.py:447 (68, 124, 173) | `age-keygen`; `sops --encrypt/--decrypt` met `SOPS_AGE_KEY` env | argv | public key | OK |
| age.py:44, 178 | `age -d -i <tmpfile>` met ciphertext op stdin | argv+stdin | private key in mkstemp 0600 | OK |
| age.py:98, 138 | `age --armor -r <pub>` met plaintext op stdin | argv+stdin | public key | OK |
| utils/sops.py:66, 91, 134, 211 | `sops --decrypt/--encrypt [--in-place] [--age <pub>] <f>` | argv | paden door OPI gegenereerd | OK |
| utils/sops.py:313 | `age-keygen -o <tmpdir>/age_key_<uuid4>` | argv | geen | Info |
| chisel_connector.py:116 | `chisel client --auth <user>:<pw> <server_url> <local>:<host>:<port>` | Popen argv | server_url, creds, remote host/port uit API-body of projectbestand | E-5, E-14 |
| logs_websocket_router.py, metrics.py, project_store.py, web/router.py | geen eigen subprocess (delegeren aan kubectl/git connectors) | n.v.t. | n.v.t. | OK |

## Tabel: templates

Renderers: `opi/generation/manifests.py:151,201` (`autoescape=False`, geen `StrictUndefined`, filter `yaml_scalar`, output rauw naar disk en git) en `opi/connectors/kubectl.py:333` (kale `Template`, output naar `kubectl apply -f -`). "naive" = `"{{ x }}"` (breekt bij `"` of newline in x). "kaal" = `{{ x }}`.

| template | user-gestuurde variabelen | gequote? | schema/model dekt? | oordeel |
|---|---|---|---|---|
| argo-repository-https | `repository_url`, `username`, `password` | kaal | url ja; username, password nee | E-1 |
| argo-repository | zelfde | tojson | n.v.t. | OK |
| argocd-application | `repoURL`, `targetRevision` (= branch), `repoPath` | kaal | url ja; branch nee; path geen newline | E-1 (branch), Info (path) |
| argocd-appproject | project-/namespace-namen | kaal/naive | ja | OK (en begrenst E-2/E-3/E-8) |
| backup-bucket-mirror-pod | endpoints, keys, bucketnamen | naive, kaal in shellscript | platformgegenereerd; remote-source niet getraceerd | AANDACHT |
| backup-bucket-pod | idem + `reference_name` in shell en labels | naive/kaal | patterned namen | AANDACHT |
| backup-clone-pvc | pvc-namen, size, storage class | naive | platform (live PVC) | OK |
| backup-database-pod | `db_host/db_name/db_user/db_password` | naive | uit DatabaseSecret (platform) | AANDACHT |
| backup-pod, backup-snapshot | platformnamen | naive | ja | OK |
| binary-secret.to-sops | `name`, `data_pairs` (key = attachment-id, value = base64) | naive | id-pattern (catalog_model) | OK |
| configmap (sleep-mode) | `config.title`, `config.description` | tojson | n.v.t. | OK (Info) |
| db-console-emails-configmap | `emails` | kaal in block scalar | `format: email` niet afgedwongen | E-10 |
| db-console-pod, db-console-secret | tool image/args, labels, poort | tojson, int | platform | OK |
| decrypt-sops, kustomization, pod-security-context | geen | n.v.t. | n.v.t. | OK |
| deployment | `resources_*` (naive 182-186), `metrics_config.path` (naive 39), `command`/`env_vars`/storage/probes/host_aliases/attachments (tojson), `imagePullSecretsMap` (yaml_scalar), `security.*` (int), namen (patterned) | gemengd | resources nee; metrics path nee; rest ja | E-8, E-11 |
| generic-secret.to-sops | `secret_pairs` keys kaal, values yaml_scalar of block+indent | keys kaal | tekstvorm ja, objectvorm nee | E-9 |
| ingress | path/rewrite, hostname, namen, issuer, ip_whitelist, dns-target, tls-secrets | pattern + tojson; namen via `sanitize_kubernetes_name` | ja | OK |
| issuer-letsencrypt | `contact_email` | kaal | clusterconfig | Info |
| job-pod | `command`, `image`, annotaties | tojson | n.v.t. | OK |
| list-snapshots-pod | platform S3 | naive | n.v.t. | OK |
| namespace | namespace | kaal | ja | OK |
| network-policy | `pod_selector` (altijd None), `ports` (vaste ints) | indent/kaal | n.v.t. | OK |
| postgresql-cluster | `storage`, `image`, `resources.*`, `postInitSQL[]` | kaal | pydantic zonder pattern | E-2 |
| project-serviceaccount | naam | naive | ja | OK |
| pvc | `size` (kaal), storage class, access modes, source pvc | kaal | quantity-regel in storage.py | OK |
| quay-proxy-organization | `upstream` (yaml_scalar), afgeleide namen (naive) | ja | `UPSTREAM_PATTERN` | OK |
| restore-bucket-pod | `target_minio_endpoint`, `target_bucket_name`, `target_*_key`, `snapshot_id`, `reference_name` | naive; `reference_name` kaal in shell | nee (pydantic `str`, padsegment ongevalideerd) | E-3 |
| restore-database-pod | `target_db_host/name/user/password`, `snapshot_id`, `reference_name` | naive | nee | E-3 |
| restore-pod | `target_pvc_name`, `pvc_name` (pad), `snapshot_id` | naive | nee | E-3 |
| restore-target-pvc | `size`, `storage_class_name` | kaal | nee | E-3 |
| service-network-policy | peer namespace/labels/ports | naive/kaal | pydantic `DNS1123_LABEL`, poort 1..65535 | OK |
| service | namen, poorten | tojson, int | ja | OK |
| sidecar-authorization-wall | issuer, client_id, hostname, banner, secretnamen | tojson | n.v.t. | OK (gefixt, met tests) |
| tenant-baseline-network-policy | deployment-naam, namespaces | naive | ja | OK |
| vpa | namen | naive | ja | OK |

Over de authwall-fix (`tests/test_authwall_banner_yaml_injection.py:14-15`): "The fix renders every user-derived scalar via `| tojson`". De fix is per template gedaan en niet centraal; er is geen lint of test die kale/naive interpolatie in andere templates afvangt. Dat is het structurele patroon achter E-1, E-2, E-3, E-8, E-9, E-10, E-11.

## Open vragen

1. Wie heeft schrijfrecht op `zad-projects` buiten OPI? Dat bepaalt of E-1 (git-pad naar cluster-admin via AppProject `default`) door een projectlid of alleen door een platform-admin te bereiken is. Als projectleden via Forgejo kunnen pushen, is E-1 Kritiek.
2. Rendert een route het formuliermodel `opi/forms/models/project_file.py` (repositories met `username`/`password`/`branch`)? Ik vond geen import in `opi/web` of `opi/api`; als er wel een pad is, geldt E-1 ook via de UI.
3. Welke Pod Security Admission-modus staat op de projectnamespaces? Dat bepaalt of een geïnjecteerde `hostPath` of `hostNetwork` (E-2, E-3, E-8) door de apiserver wordt geweigerd.
4. Is de odcn-ingress van OPI ergens anders begrensd op body-size (E-7)? De overlay-annotaties bevatten hem niet; een clusterbrede nginx-default is niet in deze repo te lezen.
5. Zet `opentelemetry-instrumentation-asyncpg` de volledige statement op de span (E-16)? Niet in deze repo verifieerbaar; OTEL staat standaard uit.
6. Worden `remote-sources` (schema 558-573, geen patterns op `host`/`database`/`bucket`) ooit in de backup-pod-templates gebruikt? Nu niet getraceerd; als dat gebeurt, geldt E-3 ook daar.
7. Is `REVOKE CONNECT ... FROM PUBLIC` misschien in de CNPG-bootstrap of een init-script buiten deze repo geregeld (E-18)?
