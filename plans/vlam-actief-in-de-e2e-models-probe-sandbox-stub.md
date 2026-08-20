# VLAM actief in de E2E: models-probe + sandbox-stub

**Status**: plan, klaar voor orch ship
**Datum**: 2026-08-20
**Context**: vervolg op RC-142 (gemerged, `48df79d5`). De vlam-dienst bestaat en is getest op bedrading; wat ontbreekt is een ACTIEVE E2E-controle die de keten echt aanroept, en een sandbox waarin die keten überhaupt bestaat. Zie `plans/vlam-in-cluster-ontsluiten-proxy-intern-zad-service.md` voor het geheel, `features/vlam-service.md` en `images/e2e-allservices/README.md` voor de bouwstenen.

## Wat we bouwen

Twee dingen, samen één doel: `GET $VLAM_API_URL/v1/models` wordt een staande, automatisch draaiende assertie in plaats van een handmatige curl.

1. De e2e-allservices-testpod krijgt een **actieve vlam-probe** (nu is vlam alleen `kind: metadata`: "de env-var is gezet", zonder aanroep).
2. De **sandbox krijgt een vlam-stub** op precies de placeholder-coördinaten uit de clusterconfig, zodat de hele ZAD-keten (dienst → env-var → egress-netpol → wildcard-ingress → antwoord) daar écht loopt. Nu wijst de placeholder nergens heen en kan de sandbox alleen bedrading testen.

Expliciet besloten en al zo gebouwd (niet aan tornen): **geen API-key in de dienst.** De key is een per-project geheim en hoort in `user-env-vars`; de hulptekst zegt dat al. Onderdeel 3 hieronder voegt er alleen één zin aan toe die `user-env-vars` bij naam noemt.

## Onderdeel 1: actieve probe in e2e-allservices

- Nieuwe probe-kind in het Go-image (`images/e2e-allservices/checks.go`), naast sql/redis/s3/oidc/path/metadata. Werknaam `vlam` of iets generieker als `http-api`; implementer kiest, maar één nieuwe kind, geen aanpassing aan bestaande kinds.
- Gedrag: `GET {VLAM_API_URL}/v1/models` met een korte timeout (enkele seconden; het is een in-cluster hop). Zelfde env-var-voorrang als het bestaande metadata-target (`APP_VLAM_API_URL`/`VLAM_API_URL`).
  - **Groen**: HTTP 200 met een JSON-lichaam dat een `data`-lijst bevat, ÓF een 401/403. Die laatste twee bewijzen dat de keten staat en alleen VLAM zelf de deur dichthoudt; `/v1/models` is vandaag key-loos maar dat is VLAM's keuze, niet de onze.
  - **Rood**: connectiefout, timeout, 5xx, of een 200 zonder herkenbare JSON. Een rood resultaat benoemt welke hop verdacht is (DNS/verbinding = netpol of stub weg; 5xx = proxy of upstream), want dit wordt vanaf de consumer-kant gedebugd.
- `scripts/generate_probe_spec.py`: vlam verhuist in de hand-onderhouden kind-mapping van `metadata` naar de nieuwe kind; `probe_spec.json` regenereren. `tests/test_probe_spec_drift.py` moet groen.
- Go-kant volgt de bestaande stijl van het image (logregels per stap, checkResult); als het image unit-tests heeft, krijgt de nieuwe kind er een.

## Onderdeel 2: sandbox-stub op de placeholder-coördinaten

De sandbox-clusterconfig heeft al een `vlam`-entry (project `vlam-wt8`, deployment `productie`, component `vlam-proxy-intern`, poort 8081). De stub maakt die coördinaten waar.

- **De stub** is een ZAD-project `vlam-wt8` in de sandbox met één component `vlam-proxy-intern`: `docker.io/library/haproxy:lts-alpine` met een config-heredoc die statisch antwoordt, geen upstream:
  - `monitor-uri /healthz` (voor de health-check-service als probe),
  - op `path /v1/models` een `http-request return status 200 content-type application/json` met een klein vast lichaam als `{"data":[{"id":"vlam-stub","object":"model"}],"object":"list"}`,
  - al het andere 404.
- **Plus de wildcard-inbound-regel** in dat stubproject (`cross-domain-access`, `from: {project: '*'}`, `to: {component: vlam-proxy-intern, port: 8081}`). Daarmee test de sandbox ook de wildcard-rendering en de open-ingress echt; het runbook bevestigt dat NetworkPolicies in de sandbox worden afgedwongen.
- **Aanmaak door de E2E-suite zelf** (module-fixture in of naast `tests/e2e/test_sandbox_vlam.py`), via de bestaande helpers. De wildcard-config gaat via de v2 service-config-API, want de UI biedt `*` bewust niet aan. De naam ligt vast (moet exact de clusterconfig matchen): de fixture moet een achtergebleven `vlam-wt8` van een vorige run netjes hergebruiken of eerst verwijderen, geen flakiness op naambotsing.
- **De URL nooit hardcoden** in tests: afleiden via `vlam_endpoint("sandboxed-local")`, dan kan de config niet stilletjes van de test wegdrijven. Let op de namespace-prefix van de sandbox (get_prefixed_namespace bepaalt).
- `tests/e2e/test_sandbox_vlam.py` uitbreiden met twee asserties bovenop de bestaande drie:
  1. Vanuit de consumer-pod slaagt de models-call naar `$VLAM_API_URL` (exec + wget, of door de consumer het e2e-allservices-image te geven en het probe-resultaat te lezen; implementer kiest de minst gekunstelde).
  2. Negatief: een pod in een project zónder de vlam-dienst krijgt een timeout op hetzelfde adres (egress dicht; de baseline staat alleen 80/443 toe).

## Onderdeel 3 (klein): hulptekst

Eén zin in `opi/services/catalog/vlam/help.md` die `user-env-vars` bij naam noemt als de plek voor de eigen `VLAM_KEY`. Niets anders aan de tekst.

## Aandachtspunten

- **Volgorde op productie blijft zoals in het hoofdplan**: de prod-e2e-pod mag de vlam-dienst pas afnemen nadat de nieuwe OPI is uitgerold én de wildcard-regel in het echte `vlam-wt8.yaml` gepusht en gereprocessed is. Dit plan verandert daar niets aan; het maakt alleen de controle actief zodra dat gebeurd is.
- **De sandbox-E2E-run vereist de sandbox**: gebruik `orch sandbox claim`/`release` (turn-taking) rond de run, of lever de test af met een aantoonbaar groene run en meld het tijdslot.
- Raak de bestaande probe-kinds en hun targets niet aan; de wijziging aan `probe_spec.json` hoort alleen het vlam-target te verplaatsen.

## Verify

1. `go build` (en `go test` indien aanwezig) van `images/e2e-allservices`; `tests/test_probe_spec_drift.py` groen.
2. Sandbox-E2E: de uitgebreide `test_sandbox_vlam.py` groen op een echte sandbox, inclusief de negatieve egress-test.
3. `uv run ruff check`, `ruff format --check`, `pyright` schoon op de python-kant.

---

## Uitvoering (RC-144, PR #140)

Alle drie de onderdelen zijn geleverd. Eén afwijking, en die is inhoudelijk:

**De stub kon geen ZAD-project zijn.** Een technische projectnaam is op dit platform niet te
kiezen -- `generate_project_name()` plakt er op elke aanmaakweg een willekeurig postfix van drie
tekens achter -- terwijl de naam `vlam-wt8` in het pod-label `project` zit waar de uitgaande
regel van de afnemer op selecteert. De stub gaat daarom met `kubectl` neer, mét exact de labels
en de servicenaam die `vlam_endpoint()` noemt. De inkomende wildcard-regel wordt wel door de
cross-domain-access-dienst zelf gerenderd, zodat de sandbox de echte YAML afdwingt.

De aanname "het runbook bevestigt dat NetworkPolicies in de sandbox worden afgedwongen" bleek
omstreden (twee documenten spraken elkaar tegen), dus de suite meet het nu: met de open regel
weggehaald loopt de aanroep vast in een time-out. De sandbox handhaaft ze dus wel, en de
verkeerde regel in `features/cross-domain-access.md` is gecorrigeerd.

Verder liep de sandbox zelf vast: `SSL_CERT_FILE` wees naar een configmap met één dev-CA en
verving daarmee de hele truststore, waardoor geen enkele nieuwe OPI-pod nog Ready werd. Dat is
gerepareerd (bundel met de systeemroots erbij) -- zie de PR-comments.
