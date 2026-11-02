# Aanvalsproef (ZAD Attack Proxy)

Een herhaalbare proef die een instantie van OPI onder bekende aanvalspatronen legt en faalt
zodra OPI zwicht. Elke run levert een **groene lijst**: alles wat gemeten is staat erop met
zijn uitkomst, en een afwijking staat erop met een uitleg (comply or explain). Een grendel in
de code bepaalt welk doel beproefd mag worden.

## De drie doelen

| doel | wat er draait | hoe |
|---|---|---|
| **lokaal** | een wegwerp-instantie op `127.0.0.1:8311`, met nagemaakte buitenwereld | `task -x security-proof` |
| **sandbox** | de geclaimde sandbox in het kind-cluster, met de echte keten (ingress, Keycloak, rig-db, object storage, registry) | `task security-proof:sandbox`, dan `task -x security-proof` |
| **staging** | een afgestemde handrun door de opdrachtgever, met een venster | `features/aanvalsproef-staging.md`; de code weigert staging zelf |

Productie is geen doel, ook niet via de grendel.

## Groen of comply or explain

Elke laag (de gerichte proeven, schemathesis, ZAP) levert per regel een uitkomst:

| uitkomst | betekent |
|---|---|
| groen | gemeten, en in orde |
| afwijking met uitleg | gemeten, wijkt af, en staat met een reden in `bekende-bevindingen.tsv` |
| bevinding | gemeten, wijkt af, en er is geen uitleg: de run faalt |
| niet gemeten | de laag of de toets kwam er niet aan toe, met de reden; dit leest nooit als groen |

Dat geldt ook voor de waarschuwingen van ZAP: elke alert vanaf riskcode 1 (WARN) is een
bevinding tenzij hij in de baseline staat, onder proefnaam `zap-<pas>` en kenmerk
`<pluginid>:<route>`. Een INFO blijft informatie. En de andere richting: een regel in de
baseline die niets meer dekt laat de run falen met "OPGELOST: haal de regel weg".

Een baselineregel geldt per doel: de kolom `doel` is leeg voor elk doel, of noemt `lokaal` of
`sandbox`. Zo neemt een reden als "draait niet in de wegwerp-sandbox" tegen de geclaimde
sandbox geen bevinding weg. Een ZAP-pas waarvoor dit doel geen herkenning heeft (geen
projectsleutel, geen ondertekende sessie) staat als "niet gemeten" in de lijst en geeft
exitcode `3`, niet `1`.

## Gebruik

```bash
task -x security-proof         # alles: sandbox op, beproeven, rapport, sandbox af
task security-proof:up         # alleen de wegwerp-sandbox, op http://127.0.0.1:8311
task security-proof:fuzzers    # de gerichte proeven tegen een al draaiende sandbox
task security-proof:down       # de wegwerp-sandbox afbreken
task security-proof:sandbox    # maak de geclaimde sandbox het doel (token in de pod)
task security-proof:sandbox-af # haal het proeftoken uit de sandbox
```

Het proeftoken komt in de sandbox via het env-secret `operations-manager-env-secrets`, dat geen
ArgoCD-eigenaar heeft, dus het staat nergens in git: `:sandbox` munt het per run, patcht het
secret, en `:sandbox-af` maakt het weer leeg. OPI weigert te starten met een gezet
`SECURITY_PROOF_TOKEN` op elk cluster behalve `sandboxed-local`.

Niet-nul exit zodra er een bevinding is die niet in
`operations-manager/security-proof/bekende-bevindingen.tsv` staat, dus dit kan als gate
draaien: `0` groen, `1` een bevinding, `3` NIET GEMETEN (een laag kon niets beproeven, en dat
leest dus niet als groen). De `-x` geeft die code door; zonder die vlag maakt `task` van elke
mislukte taak zijn eigen 201. Het rapport komt in `operations-manager/security-proof/report/aanvalsproef.md`
(git-ignored), met per bevinding de BIV-as, de componenten die hij raakt, en het commando om
hem na te doen.

## Waar wat staat

| pad | wat |
|---|---|
| `operations-manager/security-proof/README.md` | de volledige uitleg: de grendel, de proeven, en hoe je er een toevoegt |
| `operations-manager/security-proof/bin/` | de nagebootste binaries die hun argumentenlijst wegschrijven |
| `operations-manager/security-proof/bekende-bevindingen.tsv` | wat we nu accepteren, met de reden |
| `operations-manager/python/security_tests/` | de grendel, de wegwerp-sandbox, de proeven en het rapport |
| `operations-manager/python/security_tests/test_sandboxdoel.py` | de proeven die alleen tegen een sandbox in het cluster iets meten |
| `features/aanvalsproef-staging.md` | het draaiboek voor een stagingrun |
| `operations-manager/security-proof/.proef/opi.log` | het logboek van OPI tijdens de run, waar het rapport het WAAROM van een bevinding uit leest |

## Afhankelijkheden

* `schemathesis` (dependency-group `test`) voor de property-based verzoeken uit `/openapi.json`
* docker voor de ZAP-basislaag: op Linux met host-netwerk, op macOS via
  `host.docker.internal` op dezelfde poort. Zonder docker, of zonder dat de container het
  proeftoken van de sandbox terugkrijgt, meldt die laag "niet gemeten" met de gemeten reden
* de BIV-inventaris (`architecture/biv-inventaris.yaml`) voor de dekkingstabel in het rapport,
  zodat er geen tweede componentlijst ontstaat; zie `features/biv-inventaris.md`

Het rapport noemt per bevinding niet alleen de assertie, maar ook waarom OPI zwichtte: de
route, de traceback en de logregels eromheen uit `.proef/opi.log`, geredigeerd volgens
`opi/utils/logging_redact.py`. Elke `Exception in ASGI application` krijgt een uitspraak mee
(bevinding, of de verwachte uitkomst van een wegwerp-sandbox zonder database), en een proef die
zelf omvalt staat erin met zijn exitcode en de laatste logregels.

Het rapport (`report/aanvalsproef.md`) opent met de uitkomst, en bevat de groene lijst per
laag, de bevindingen met hun reproductiepad, en de dekkingstabel: per component uit de
BIV-inventaris welke proef hem dekt en tegen welk doel dat te meten is.

Alle details, en de grenzen van wat de proef wel en niet kan zeggen, staan in
`operations-manager/security-proof/README.md`.
