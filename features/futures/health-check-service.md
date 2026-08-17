# Health-check service

**Status**: Voorstel (niet geïmplementeerd)
**Prioriteit**: Hoog, en het tijdvenster sluit (zie "Waarom nu")
**Aangemaakt**: 2026-07-28
**Basisbranch**: `uniform-declarative-platform-services`. Dat is de enige branch
waar het servicesysteem (`opi/services/catalog/`) leeft; op `main` bestaat het
niet, en dus ook niet in het draaiende productie-image. Bouw hier niet op main,
en niet op de sleep-mode-branch.

## Samenvatting

Maak van de health-check een component-level service, `health-check`, met
`scheme`, `port`, `liveness-path` en `readiness-path` als config. De service
overschrijft het standaardgedrag van het platform; wie de service niet kiest,
merkt er niets van en houdt de huidige TCP-probe.

Vervangt het bestaande `probe:` blok op componentniveau, dat nog door geen
enkel project gebruikt wordt.

## Aanleiding

FSC-componenten serveren hun health-endpoints op een aparte monitoring-poort
(8080) zonder mTLS, terwijl hun functionele poort (8443) wél mTLS afdwingt.

Concreet, project `mft-tp9`, component `dirmgr`:

```yaml
ports:
  inbound:
    - 8443        # mTLS, FSC-verkeer
# 8080 is de monitoring-poort en is niet als inbound gedeclareerd
```

De FSC-manager gebruikt zelf:

```yaml
readinessProbe:
  httpGet:
    path: /health/ready
    port: monitoring
livenessProbe:
  httpGet:
    path: /health/live
    port: monitoring
```

`mft-tp9` heeft geen probe-configuratie en valt dus terug op de default
`tcp`. De kubelet opent daardoor elke 5 seconden een kale TCP-verbinding naar
8443 en verbreekt hem weer. Dat is precies wat een mTLS-server als
handshake-fout logt, en het dirmgr-log staat er vol mee. Bijkomend: een
TCP-probe zegt alleen dat de socket open is, niet dat de applicatie gezond is.

Zusje `dirui` (inbound `[8080]`) heeft de poort niet nodig, maar wel de paden.

## Wat er nu is

Commit 8b91f13c (2 juli 2026) leverde een afgeslankte versie van
[configurable-health-probes.md](configurable-health-probes.md): een `probe:`
blok als directe eigenschap van een component, naast `ports:` en `services:`.

```yaml
components:
  - name: dirmgr
    probe:
      scheme: none | tcp | http | https   # default tcp
      liveness-path: /...                 # default /
      readiness-path: /...                # default /
```

Twee problemen:

1. **Geen poort.** De probe gebruikt onvoorwaardelijk `application_port`, en
   dat is hardgecodeerd de *eerste* inbound-poort
   (`opi/manager/project_manager.py:5049`). De monitoring-poort is niet te
   targeten. Het oorspronkelijke ontwerp noemde die poort wel, maar hij is niet
   meegegaan in de implementatie.
2. **Verkeerde plek.** Het is een los pod-spec-knopje aan de wortel van het
   component, met eigen schema, eigen extractor en straks eigen maatwerk in de
   wizard. Precies het soort ruis waar het servicesysteem voor bestaat.

## Waarom een service

### Het precedent

`metrics-scraper` is structureel hetzelfde geval en is al een service:
component-level, provisioneert niets, en de hele config is een poort plus een
pad met een fallback op de applicatiepoort.

```python
class MetricsScraperConfig(BaseModel):
    port: int | None = None   # None -> valt terug op de applicatiepoort
    path: str | None = None   # None -> valt terug op "/metrics"
```

Met `scope="component"`, een `config_component_layout()` die een fieldset in
het componentformulier hangt, en een `contribute_manifest_context()` die er
template-variabelen van maakt. Vervang "metrics" door "probe" en je hebt deze
service. `authorization_wall` en `sleep-mode` zijn eveneens gedragsservices
zonder externe provisioning; dit is een patroon, geen uitzondering.

### Het principe: een service mag het standaardgedrag aanpassen

Dit staat al letterlijk in `opi/services/catalog/base.py:180`:

> Two merge semantics, matching "a service may *add to* and *override* the base
> manifest". [...] `template_vars` is an **override** (the loop `update`s the
> base template context, so e.g. auth-wall replaces `service_port` 8080 -> 4180).

De auth-wall doet dit dus al: hij verlegt de Service-poort van de applicatie.
`project_manager.py:5498` legt de contributies met `dict.update` over de
basis-context. Een health-check-service die `probe_scheme` en `probe_port`
overschrijft, gebruikt dat contract precies zoals het bedoeld is.

De winst is wie je er níét mee lastigvalt. Health checks zijn er altijd, maar
de overgrote meerderheid van de projecten hoeft er niets van te weten en ziet
er ook niets van. Wie het wel weet, en dat is precies het soort partij dat een
mTLS-poort naast een monitoring-poort draait, regelt het via de service. Het
component blijft schoon voor iedereen die de vraag niet heeft.

### De kanttekening, en waarom die aanvaard is

Bij elke andere service betekent afwezigheid "uit". Geen `metrics-scraper` is
geen scraping. Hier betekent afwezigheid "TCP-probe op de applicatiepoort",
want een health check is er altijd. Dit is dus de eerste service waarvan het
weglaten niet neutraal is.

Dat is een bewuste keuze, geen omissie. Twee consequenties om te kennen:

- De servicebeschrijving moet expliciet zeggen dat er zonder deze service ook
  gecontroleerd wordt, alleen dan op TCP-niveau. Anders leest de `/services`
  pagina als "health checks aanzetten", wat misleidend is.
- Probes helemaal uitzetten doe je door de service tóé te voegen met
  `scheme: none`. Iets uitschakelen door iets in te schakelen leest raar, maar
  het is expliciet en zichtbaar in het projectbestand, en dat is de betere
  eigenschap.

### Wat we expliciet niet doen

Geen generieke "override-service" waarin allerlei gedrag te configureren valt.
Dat wordt een configuratie-emmer die zich voordoet als service. Elke override
verdient een eigen naam, icoon en beschrijving op de `/services` pagina; dat is
juist waarom services werken.

## Waarom nu

Geen enkel projectbestand in de projects-repo gebruikt `probe:`. Nul. Het blok
zit wel in het draaiende productie-image (pin `2026.07.27.0941-9d9c0764`), maar
is nooit uitgeoefend.

De migratiekosten van "verplaats het naar een service" zijn daarmee op dit
moment nul, en dat venster sluit zodra het eerste project het in gebruik neemt.
Het slechtste scenario is: nu snel een `port` aan `probe:` plakken, mft-tp9
erop zetten, en er later alsnog een service van maken. Dan migreer je wel
echte projectbestanden en heb je een periode met twee manieren voor hetzelfde.

## Ontwerp

```yaml
components:
  - name: dirmgr
    ports:
      inbound:
        - 8443
    services:
      - publish-on-web:
          config:
            tls: passthrough
      - health-check:
          config:
            scheme: http
            port: 8080
            liveness-path: /health/live
            readiness-path: /health/ready
```

| Veld | Verplicht | Default | Betekenis |
|---|---|---|---|
| `scheme` | Nee | `tcp` | `none`, `tcp`, `http` of `https` |
| `port` | Nee | de eerste inbound-poort | Poort waarop geprobed wordt |
| `liveness-path` | Nee | `/` | Pad voor liveness én startup; genegeerd bij `tcp` |
| `readiness-path` | Nee | `/` | Pad voor readiness; genegeerd bij `tcp` |

Gebruik de `config:` wikkel zoals `publish-on-web` en `attachments` in datzelfde
bestand. Niet de inline vorm van `metrics-scraper` kopiëren
(`- metrics-scraper: {port: 8000}`); die noemt zijn eigen `config_model.py`
"unusual" en is de uitzondering, niet het model.

### Bewuste keuzes

**Integer, geen poortnaam.** FSC schrijft `port: monitoring`, een verwijzing
naar een benoemde `containerPort`. Wij genereren die namen zelf (`http` voor de
eerste inbound-poort, `p<nummer>` voor de rest), dus een naam zou de gebruiker
aan onze generatielogica koppelen. Een nummer is ondubbelzinnig.

**De poort hoeft niet in `ports.inbound`.** De kubelet probet het pod-IP
rechtstreeks; een `containerPort`-declaratie is daarvoor niet nodig. 8080
toevoegen aan `inbound` zou juist schade doen: het levert een extra
Service-poort op, en als eerste in de lijst zou het de Service en de ingress
van de functionele poort afhalen.

**Eén poort voor alle drie de probes.** Het oude ontwerp had een poort per
probe. Geen enkel bekend geval heeft liveness en readiness op verschillende
poorten. Splitsen kan alsnog als dat geval zich meldt.

**Timings blijven vast.** Onze template hanteert eigen waarden (startup 5s/5s
met 36 pogingen, liveness 5s/30s, readiness 0s/5s). De FSC-waarden
(`initialDelaySeconds` 3 en 10, `periodSeconds` 10) nemen we niet over; de onze
zijn strenger en in productie bewezen. Instelbare timings nodigen uit tot
verkeerd afstellen, met flapperende liveness als gevolg.

**Geen inhoudscontrole.** Een `httpGet`-probe rekent 200 tot en met 399 als
geslaagd. Dat is precies wat gevraagd is en het kost geen regel code.

**Alleen `ConfigLayer.COMPONENT`.** Een health-endpoint is een eigenschap van
het image, niet van de omgeving. Geen override per deployment.

### Waar het standaardgedrag blijft wonen

Belangrijk voor de opzet: de service overschrijft alleen. De basis blijft in
generieke code, want die moet ook gelden voor de honderd componenten die de
service niet kiezen.

- `project_manager.py` zet als basis `probe_scheme = "tcp"`, en `"none"` zodra
  het component geen inbound-poort heeft. Die vangnetregel bestaat al
  (`project_manager.py:5057`) en blijft ongewijzigd: een component zonder
  inbound-poort krijgt geen probe, ook niet als de service er staat. De kubelet
  kan daar niets bereiken.
- De template houdt zijn eigen defaults (`| default('/')`) zodat een halve
  config nooit een kapotte probe rendert.
- De service levert uitsluitend de sleutels die de gebruiker heeft ingevuld.

## Implementatie

Volgt de checklist uit `instructions/services.md` ("Adding a service").

1. **Identiteit.** `ServiceType.HEALTH_CHECK` in
   `opi/services/services_enums.py`, plus een `ServiceDefinition` in
   `opi/services/services.py` (`scope="component"`, geen `variables`) en één
   regel in `opi/services/registry.py`.
   *Verifieer*: `tests/test_service_providers.py`.

2. **Pakket.** `opi/services/catalog/health_check/__init__.py` met een
   `Service` subclass. Geen `provision`, geen `cleanup_manager_key`, geen
   `manifest_secret_class`: dit is een gedragsservice. Wel
   `contribute_manifest_context()` die `probe_scheme`, `probe_port`,
   `probe_liveness_path` en `probe_readiness_path` in `template_vars` zet, naar
   het model van `metrics_scraper/__init__.py`. Lees de identiteit met
   `service_entry_name()`, nooit via de dict-sleutels.

3. **Config.** `config_model.py` met een Pydantic-model (`extra="forbid"`, alle
   velden optioneel met `None`), `config_model` + `config_schema_version = "1.0"`
   op de service, daarna `uv run python -m opi.services.config_schema` en het
   fragment `health-check.v1.0.json` committen.
   *Verifieer*: `tests/test_service_config_schema.py` (drift-lock).

4. **UI.** `editables.py` + `visualizers.py` + `config_component_layout()`,
   gekopieerd van `metrics_scraper`. Let op de trap uit `instructions/services.md`:
   *geen* config-defaults zaaien op een component dat de service niet gekozen
   heeft, want het `{K}` pad-filter materialiseert de service dan als
   neveneffect in de lijst en een default wordt stilletjes een selectie.
   Dit is meteen de wizard-ondersteuning die Mark gevraagd is; die komt hier
   uit de declaraties rollen in plaats van uit maatwerk.

5. **Template.** In `manifests/deployment.yaml.jinja:166-205` de zes
   `{{ application_port }}` op de probe-regels (170, 175, 183, 188, 196, 201)
   vervangen door `{{ probe_port | default(application_port, true) }}`. De
   `true` is nodig zodat ook `None` op de default valt, niet alleen
   "niet gedefinieerd".
   *Verifieer*: de golden manifests blijven ongewijzigd, want geen enkel
   testproject selecteert de service.
   Let op: de sleep-mode-waker bestaat nog niet op deze basisbranch. Hij zet
   straks zelf `application_port` en géén `probe_port`, dus met de
   `default(application_port, true)` blijft hij vanzelf correct renderen zodra
   die branch merget. Ga er in dit werk niet naar zoeken.

6. **Opruimen.** Verwijder het `probe:` blok uit `opi/schemas/project_v2.json:407`
   en `extract_component_probe()` uit
   `opi/handlers/project_file_handler.py:640`, plus de aanroep op
   `project_manager.py:5053`. Geen migratie nodig, zie "Waarom nu". Eén
   uitzondering: als de tussenoplossing hieronder is toegepast, staat er één
   `probe:` blok in mft-tp9 dat in dezelfde uitrol weg moet.

Merk op dat stap 6 de globale `$defs` raakt, terwijl `instructions/services.md`
zegt dat je die niet zou moeten hoeven aanraken. Dat klopt hier ook: het is een
verwijdering van een oude vorm, geen speciaal geval voor deze service.

### Guardrails

```bash
cd operations-manager/python
uv run pytest tests/test_service_providers.py tests/test_service_config_schema.py \
              tests/test_golden_manifests.py tests/test_flow_registry_snapshot.py -q
uv run ruff check . --fix && uv run ruff format . && uv run pyright
```

Aanvullend: een project zonder `health-check` moet een byte-identieke Deployment
opleveren. Dat is de belangrijkste regressietest, want het is de belofte aan de
honderd componenten die hier niets van willen weten.

## Uitrolvolgorde, en waarom die kritisch is

Het schema staat op `additionalProperties: false`. Zet je de nieuwe service in
een projectbestand terwijl het draaiende OPI-image hem nog niet kent, dan faalt
`validate_project_schema` en zijn **alle** deploys van dat project stil
geblokkeerd. Dat is exact het dp-bn7-scenario: een schemagat dat elke reprocess
liet falen zonder dat iemand het merkte.

Daar komt een tweede horde bij die groter is dan de eerste: het servicesysteem
zelf zit nog niet in productie. `opi/services/catalog/` bestaat niet op `main`
en niet in het gepinde image (`2026.07.27.0941-9d9c0764`). Deze service kan dus
pas naar productie als `uniform-declarative-platform-services` (RC-5) gemerged
en uitgerold is. Voor mft-tp9 betekent dat: de tussenoplossing hieronder is
voorlopig het enige dat helpt, en die werkt juist omdat het oude `probe:` blok
wél op main staat.

Dus: code, image, pin in de odcn-overlay, uitrollen via ArgoCD, en pas daarna
`mft-tp9.yaml`. Nooit andersom.

## Tussenoplossing voor vandaag

Marks handshake-fouten hoeven niet op deze bouw te wachten. Dit werkt met het
huidige productie-image en vereist geen code en geen uitrol:

```yaml
components:
  - name: dirmgr
    probe:
      scheme: none
```

Dat verwijdert alle drie de probes op dirmgr. Het log wordt stil, maar dirmgr
heeft dan geen enkele health-check meer: een vastgelopen proces wordt niet
herstart en een niet-gereed pod krijgt gewoon verkeer. Op een testnetwerk te
verdedigen als tijdelijke maatregel. Het is wel meteen het enige gebruik van
`probe:` in de hele projects-repo, dus stap 6 hierboven moet die regel in
dezelfde uitrol weer weghalen.

## Eindtoestand voor mft-tp9

```yaml
components:
  - name: dirmgr
    ports:
      inbound:
        - 8443
    services:
      - publish-on-web:
          config:
            tls: passthrough
      - postgresql-database
      - attachments:
          config: [...]
      - health-check:
          config:
            scheme: http
            port: 8080
            liveness-path: /health/live
            readiness-path: /health/ready

  - name: dirui
    ports:
      inbound:
        - 8080
    services:
      - publish-on-web:
          config:
            tls: standard
      - attachments:
          config: [...]
      - health-check:
          config:
            scheme: http
            liveness-path: /health/live
            readiness-path: /health/ready
```

### Open vraag bij dirui

Serveert `docker.io/federatedserviceconnectivity/directory-ui` daadwerkelijk
`/health/live` en `/health/ready`? Voor `dirmgr` is dat onderbouwd, de snippet
komt uit de FSC-manager zelf. Voor `dirui` is het een aanname
("dirui dan ook gelijk maar").

Dat is geen detail: bestaat het pad niet, dan faalt de startupProbe 36 keer met
5 seconden ertussen en gaat dirui in een herstartlus. Verifiëren vóór het
aanzetten, bijvoorbeeld tegen een draaiende pod:

```bash
kubectl -n mft-tp9 exec deploy/<dirui-pod> -- \
  wget -S -O /dev/null http://127.0.0.1:8080/health/ready
```

Zet dirui desnoods in een tweede commit, nadat dirmgr groen staat.

## Verwante documenten

- [configurable-health-probes.md](configurable-health-probes.md) - het
  oorspronkelijke ontwerp waarvan 8b91f13c een deel leverde
- `instructions/services.md` - het servicesysteem en de checklist die stap 1 tot
  en met 4 volgen
- [fsc-mtls-attachments.md](fsc-mtls-attachments.md) - de mTLS-certificaten die
  dirmgr op 8443 gebruikt
