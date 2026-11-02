# De aanvalsproef: meer wapens naast de containerscan

De scanners die het platform op zichzelf afvuurt, en de poort waarachter hun uitkomst
staat. BIO2 8.08 (kwetsbaarhedenbeheer) en 8.28 (veilige ontwikkeling).

De containerscan en pip-audit kijken naar wat we meenemen (`features/security-scanning-pipeline.md`).
De scanners hieronder kijken naar wat we zelf maken: de code, de gegenereerde manifests, de
historie van de repo, en het draaiende portaal.

## Wat er draait

| Stage | Werktuig | Wat het ziet | Waar |
|---|---|---|---|
| `gitleaks` | gitleaks | sleutelvormen in de boom, met vendor- en entropieregels | `security.yml`, elke PR |
| `gitleaks-history` | gitleaks | dezelfde regels over elke blob die ooit bestond | `security.yml`, wekelijks |
| `sast-semgrep` | semgrep | `p/python`, `p/owasp-top-ten` plus de eigen ZAD-regels | `security.yml`, elke PR |
| `sast-codeql` | CodeQL | taint-analyse van invoer naar de argumentenlijst van een extern programma | `security.yml`, elke PR |
| `manifest-posture` | kube-linter, checkov | de manifests die ZAD voor elke tenant genereert | `security.yml`, elke PR |
| `nuclei` | nuclei | ruim 11.000 templates tegen een zelf opgezet portaal, met en zonder sessie | `aanvalsproef.yml`, elke PR |

De twee schotten op het cluster staan apart: `features/kyverno-admission-audit.md` en
`features/falco-runtime-detectie.md`. De dekkingskaart over alle toetsen heen is
`features/asvs-zelfbeoordeling.md`.

## De runner staat dicht

Elke job hier begint, net als elke andere job in deze repo, met
`step-security/harden-runner` op `egress-policy: block` en de allowlist uit
`env.HARDEN_ENDPOINTS` van die workflow (RC-369, zie features/security-scanning-pipeline.md). `aanvalsproef.yml` heeft
een eigen lijst, omdat deze jobs dingen ophalen die de andere workflows niet nodig hebben:
de nuclei-templates, de kind-binary met zijn node-image, de Kyverno-images uit `install.yaml`
en het Falco-image. Boven die lijst staat per endpoint waarom hij erbij hoort.
`security.yml` kreeg er om dezelfde reden `semgrep.dev` bij: daar komen de twee
registry-regelsets vandaan.

Wat de scanners zelf naar het doelwit sturen valt hier niet onder: dat gaat naar de loopback
van de runner en komt nooit op een netwerk.

Deze twee workflows zijn op een echte GitHub-run nog niet gelopen, dus de allowlists zijn
afgeleid uit de documentatie en de bron van elk werktuig, niet gemeten. Blijkt er een
endpoint te missen, dan staat hij als annotatie van harden-runner in de run, en hoort hij in
`env.HARDEN_ENDPOINTS` van die workflow.

## De veiligheidsgrendel

Een scanner die echt aanvalsverkeer afvuurt mag alleen een instantie raken die de run zelf
heeft opgezet. Daarom:

- het doelwit is `operations-manager/python/tests/e2e/testserver.py`, dezelfde testserver als de E2E-suite: de echte
  app met gemockte externe systemen, zonder cluster, database of Keycloak;
- hij bindt op `127.0.0.1` van de runner en verdwijnt met de job;
- en voor de scan draait `uv run python -m opi.tools.aanvalsproef grendel <url>`, die elk
  doelwit weigert dat geen loopback is. `tests/test_aanvalsproef.py` bewaakt dat die stap
  voor de scan staat, zodat een verschrijving nooit op productie of op de gedeelde sandbox
  kan landen.

De scan draait twee keer: een keer anoniem, en een keer met een getekend sessiecookie
(dezelfde helper die de E2E-tests gebruiken), omdat het oppervlak achter de inlogmuur het
grootste deel van de app is.

## De poort: een bevinding is nieuw of hij staat in de lijst

Alle gates lopen door een module: `opi/tools/aanvalsproef.py`. Die leest het rapport van de
scanner (vier formaten), legt de drempel erop, en haalt eraf wat in
`aanvalsproef/uitzonderingen.yaml` staat. Wat overblijft maakt de run rood.

```bash
uv run python -m opi.tools.aanvalsproef nuclei nuclei.jsonl --drempel medium
uv run python -m opi.tools.aanvalsproef kube-linter kube-linter.json
uv run python -m opi.tools.aanvalsproef checkov results_json.json
uv run python -m opi.tools.aanvalsproef semgrep semgrep.json
```

Een uitzondering heeft een reden (`statement`) en een vervaldatum (`expired_at`), net als in
`.trivyignore.yaml`. Een verlopen regel zwijgt niet meer, dus de bevinding komt vanzelf
terug in plaats van stil te verdwijnen. `tests/test_aanvalsproef.py` weigert een regel
zonder die twee, en een vervaldatum verder dan een jaar vooruit.

De allowlists in `aanvalsproef/gitleaks.toml` hebben dezelfde eigenschap. gitleaks kent zelf
geen reden en geen vervaldatum, dus die staan als `# reden:` en `# vervalt:` in het blok zelf,
en dezelfde toets laat ze verlopen.

Die lijst heeft twee helften. De eerste gaat over de boom, de tweede over de blobs die alleen
nog in de historie staan: de ontwikkelsleutel van de oude lokale git-daemon, de AGE-sleutels
van de eerste sandbox, een meegecommitte sandboxsessie, een ArgoCD-projecttoken uit de oude
`HOW.md`, de projectsleutel in het doorloopverslag van RC-108, en twee valse treffers van de
entropieregel. Elk blok noemt waarom die waarde vandaag niets meer opent, met de commit die
hem eruit haalde. Een pad dat uit de boom verdwenen is, is op pad uitgezonderd; voor een
bestand dat nog bestaat zet een stopword alleen die ene waarde stil, zodat het pad zelf in de
poort blijft. Staat het bestand nog in de boom maar is de waarde daar al geredigeerd, dan is
een stopword er niet meer en begrenst `targetRules` het blok tot de regel die de historische
treffer gaf. Dat is nodig onder `docs/`, waar gitleaks de enige laag is: `secret-scan` trieert
elk pad dat `(^|/)docs?/` matcht weg als voorbeeldpad, dus een blok op pad alleen zet zo'n
bestand voor elke regel blind. Gemeten op 22 oktober 2026 met gitleaks 8.30.1:
`gitleaks git .` is schoon over 9885 commits, `gitleaks dir .` over de boom ook, en een
nieuwe credential in een bestand met een stopword of met `targetRules` wordt nog steeds
gevangen.

De drempel verschilt per scanner, en met een reden:

- **nuclei** heeft eigen ernstniveaus; de gate staat op `medium`. De bevindingen op
  `info`-niveau (ontbrekende headers op het http-doelwit, `tech-detect`, de vindbare
  `/docs`) zeggen iets over de meetopstelling of staan achter de inlogmuur.
- **kube-linter** en **checkov** geven geen bruikbare ernst mee. Daar is de *selectie* de
  drempel: `aanvalsproef/kube-linter.yaml` en `aanvalsproef/checkov.yaml` noemen expliciet
  welke checks meedoen, en elke bevinding daaruit telt even zwaar.
- **semgrep** vertaalt INFO, WARNING en ERROR naar de schaal van de drempel.

## Wat de eigen semgrep-regels toevoegen

`aanvalsproef/semgrep-zad.yaml` houdt drie dingen vast die ruff niet kan zien:

- `zad-webroute-zonder-sso`: een route in `opi/web/` zonder `@requires_sso`. De bewuste
  uitzonderingen zeggen dat zelf met `BEWUST ZONDER` in hun docstring; dat is de
  ontsnapping, en hij is permanent in plaats van verlopend, want een publieke pagina blijft
  publiek. `opi/web/lotc_router.py` staat als geheel buiten de regel: die proefopstelling is
  publiek bij ontwerp en wordt alleen op local en sandboxed-local geregistreerd.
- `zad-subprocess-buiten-connector` en `zad-subprocess-via-een-shell`: de connector-afspraak
  uit `CLAUDE.md`. Dit is het scherpste pad van dit platform: een veld uit een projectbestand
  dat in de argumentenlijst van `kubectl`, `git`, `sops` of `mc` belandt. De vier bestaande
  crypto- en CLI-helpers (`opi/utils/sops.py`, `opi/utils/git_signing.py`,
  `opi/handlers/sops.py`, `opi/core/cluster_config.py`) staan buiten de regel: ze roepen hun
  binary met een vaste argumentenlijst aan en zijn ouder dan de afspraak. De regel is er voor
  nieuwe aanroepsites.

Waarom dit naast ruff staat en niet erin: `pyproject.toml` zet `S603`, `S607` en `S608` voor
heel `opi/` uit. Dat is precies het injectiepad, en een taint-bewust werktuig is wat dat gat
dekt. Bandit zou de rest van de `S`-regels dubbel doen en is daarom niet toegevoegd.

Een regel die niets meer vangt is een gate die groen staat zonder te kijken. Daarom staat er
een stap in `sast-semgrep` die `aanvalsproef/semgrep-voorbeelden.py` tijdelijk naar `opi/web/`
kopieert en eist dat alle drie de regels vuren.

## Wat de gate op de manifests meet

Gescand wordt `operations-manager/python/tests/golden/manifests`: de gerenderde manifests die
ZAD voor elke tenant genereert, bijgehouden door de golden-tests. Dat is het oppervlak waar
een fout zich honderd keer vermenigvuldigt.

De platformcomponenten onder `infrastructure/` zitten er niet in. Daar liggen losse
overlay-patches naast de bases, en een patch is geen volledig manifest: de gate zou de patch
meten en niet de uitkomst. Een scan van die map geeft 58 bevindingen die bijna allemaal
daarover gaan. Die kant dekken de Kyverno-policies bij admission, op de echte pod.

Twee bevindingen staan structureel in de uitzonderingslijst:

- **read-only rootfs** (`no-read-only-root-fs`, `CKV_K8S_22`): het image van een tenant is
  niet van ons, en een read-only rootfs afdwingen breekt elke applicatie die naar `/tmp`
  schrijft. De Kyverno-policy rapporteert het in audit-modus, zodat de omvang zichtbaar is.
- **een NetworkPolicy per pod** (`non-isolated-pod`, `CKV2_K8S_6`): ZAD genereert die wel,
  maar in een ander bestand dan de golden-snapshot van de workload
  (`features/restrictive-network-policies.md`).

De derde (`CKV_K8S_153`) is een echte bevinding met een kortere vervaldatum: de gegenereerde
ingress zet de internet.nl-headers ook via een `nginx.ingress.kubernetes.io/configuration-snippet`.
Het snippet zelf is ons sjabloon; de twee waarden die erin komen (het pad en de
rewrite-basis van een component) komen wel van de tenant en worden aan de bron op charset
getoetst (`SAFE_PATH_PATTERN` in `opi/utils/url_paths.py`), zodat er geen aanhalingsteken,
witruimte of newline in de nginx-config kan belanden. Wat erna overblijft is dat de
annotatie alleen werkt zolang de controller snippets cluster-breed toestaat, en dat is de
vraag die daarachter hoort.

## Een uitzondering toevoegen

1. Dicht de bevinding als dat kan. Een uitzondering is de tweede keuze.
2. Anders: zet hem in de juiste sectie van `aanvalsproef/uitzonderingen.yaml` met het id dat
   de scanner noemt (`template-id`, de check-naam, `check_id`, of het semgrep-regel-id), een
   `statement` dat de afweging uitlegt, en een `expired_at` binnen een jaar.
3. Draai de gate opnieuw. Komt de bevinding niet terug, dan was het id goed.

## De versies van de scanners

De scanners komen als binary of als pakket binnen, met de versie als env-pin bovenaan de
workflow en een `# renovate:`-annotatie erboven. De custom manager in `renovate.json` houdt
die pins bij. Dat is niet cosmetisch: de uitzonderingslijsten zijn gemeten met die versies
(22 oktober 2026: gitleaks 8.30.1, semgrep 1.182.0, kube-linter 0.7.1, checkov 3.3.30,
nuclei 3.11.1, Kyverno 1.19.2, kind 0.24.0).

Een pin hoort geen bekend advisory te dragen. Gemeten tegen de OSV-API staan alle pins schoon,
op Kyverno na: drie SSRF- en cross-namespace-advisories hebben in geen enkele release een fix,
dus die zijn niet weg te bumpen. De aanvaarding staat daarom als `# reden:` en `# vervalt:`
boven de pin in `aanvalsproef.yml`, en
`tests/test_aanvalsproef.py::test_een_aanvaard_advisory_op_een_gepinde_scanner_verloopt` laat
hem verlopen. De kern van die reden: alle drie vragen dat de aanvaller zelf een
(Cluster)Policy kan aanmaken, en Kyverno staat alleen op het wegwerp-Kind-cluster van de job
en op wat `task schotten:kyverno` met de hand raakt. Wie dit schot naar een cluster met
tenants brengt, moet eerst `kyverno:rbac:admin:policies` uit `install.yaml` halen: die
ClusterRole aggregeert naar de ingebouwde `admin`, en dan mag een namespace-admin wel een
Policy aanmaken.

## Wat er nog niet in zit

- **Enforce-modus** van de admission-policies: zie `features/kyverno-admission-audit.md`.
- **CodeQL kan niet poorten.** De SARIF gaat naar het Security-tabblad; falen op een nieuwe
  bevinding vraagt GitHub Advanced Security op deze repo.
- **De posture-gate op `infrastructure/`**, om de reden hierboven. Dat vraagt een gerenderde
  overlay, en die heeft de SOPS-sleutels nodig die CI niet heeft.
- **Schemathesis** (property-based API-fuzzing uit de OpenAPI) en een handmatige ronde met
  Burp horen bij de aanvalsproef, maar niet bij deze stages.
