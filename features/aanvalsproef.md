# Aanvalsproef (ZAD Attack Proxy)

Een herhaalbare proef die een wegwerp-instantie van OPI op je eigen machine onder bekende
aanvalspatronen legt en faalt zodra OPI zwicht. Groen betekent: de patronen die we kennen zijn
doorstaan. Het harnas draait alleen tegen een instantie die het zelf opzet en afbreekt; een
grendel in de code weigert elk ander doel.

## Gebruik

```bash
task -x security-proof         # alles: sandbox op, beproeven, rapport, sandbox af
task security-proof:up         # alleen de wegwerp-sandbox, op http://127.0.0.1:8311
task security-proof:fuzzers    # de gerichte proeven tegen een al draaiende sandbox
task security-proof:down       # de wegwerp-sandbox afbreken
```

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

Alle details, en de grenzen van wat de proef wel en niet kan zeggen, staan in
`operations-manager/security-proof/README.md`.
