# Het webadres hoort bij de dienst

Hoe een deployment zijn webadres opbouwt, is configuratie van **publish-on-web** en staat
sinds schemaversie 2.7 ook daar: onder de dienst, op de deployment waar de waarde geldt.

## Wat er veranderde

Zeven velden stonden los in de wortel van een deployment. Ze beschrijven samen precies een
ding — hoe publish-on-web een hostnaam samenstelt en met welk certificaat — en zijn verhuisd
naar de dienstconfiguratie van die deployment.

**Voor (t/m schemaversie 2.6):**

```yaml
deployments:
  - name: productie
    cluster: odcn-production
    namespace: rig-prd-x
    domain-format: component-deployment-project
    base-domain: rijksapp.nl
    subdomain: wies
    issuer: letsencrypt
    root-component: frontend
    expose-component-on-bare-domain: frontend
```

**Na (schemaversie 2.7):**

```yaml
deployments:
  - name: productie
    cluster: odcn-production
    namespace: rig-prd-x
    services:
      - reference: publish-on-web
        config:
          domain-format: component-deployment-project
          base-domain: rijksapp.nl
          subdomain: wies
          issuer: letsencrypt
          root-component: frontend
          expose-component-on-bare-domain: frontend
```

De betekenis van elk veld is onveranderd; alleen de plek is anders. Zie
[domain-configuration.md](domain-configuration.md) en [domain-format.md](domain-format.md)
voor wat de velden doen.

## Waarom

De consolidatie van dienstconfiguratie was voor publish-on-web halverwege blijven staan.
De TLS-modus stond al onder de dienst bij het component, het `domains:`-goedkeuringsblok was
in v2.5 naar de dienst verhuisd, maar de zeven deploymentvelden niet. Een projectbestand
liet dat direct zien: `tls` netjes onder de dienst, `domain-format` los in de wortel
ernaast.

## De drie lagen

publish-on-web draagt configuratie op drie niveaus, elk met een eigen vraag en een eigen
model (`config_model_for(layer)`):

| Laag | Vraag | Velden |
|---|---|---|
| project | Op welke domeinen mag dit project publiceren? | `domains`, plus de projectbrede `tls`/`attachment`-standaard |
| deployment | Hoe wordt het adres van deze deployment samengesteld? | de zeven hierboven |
| component | Gebruikt dit component de dienst, en hoe wordt TLS afgehandeld? | `tls`, `attachment` |

Een model per laag, niet een zak van tien optionele velden: zo houdt niets tegen dat `tls`
op een deployment belandt of `subdomain` op een component.

Dat `tls` ook op projectniveau staat is geen restant: laag 3 van de certificaatcascade in
`_resolve_publish_on_web_config` is de projectingang, dus een project kan er een
standaard-TLS-modus zetten voor elk component dat hem niet overschrijft.

### Het schema volgt de laag

De API leidt zijn request-body per laag af uit `config_model_for(layer)`, dus die stond al
goed. Het VASTGELEGDE fragment niet: dat werd alleen uit `config_model` gerenderd, en
beschreef dus een van de drie vormen terwijl de andere twee nergens stonden.

Een dienst committeert nu een fragment per laag zodra die laag een ander model heeft:

```
opi/services/catalog/publish_on_web/publish-on-web.v1.0.json             # config_model (component)
opi/services/catalog/publish_on_web/publish-on-web.project.v1.0.json
opi/services/catalog/publish_on_web/publish-on-web.deployment.v1.0.json
```

Regenereren met `uv run python -m opi.services.config_schema`;
`tests/test_service_config_schema.py` laat de suite falen als een fragment achterloopt op
zijn model. Een dienst waar elke laag hetzelfde model gebruikt krijgt geen extra bestanden —
identieke kopieën die je in de pas moet houden zijn precies wat dit mechanisme voorkomt.

Dit bracht ook de ontbrekende fragmenten van `persistent-storage`, `temp-storage` en
`postgresql-database` aan het licht: die hadden al een laag-eigen model (per-mount
clone-state, respectievelijk clone-state op de deployment) waarvan de vorm nergens
vastlag.

## Lezen en schrijven

`opi/services/catalog/publish_on_web/domain_config.py` is de enige autoriteit over de
locatie — dezelfde rol die `connectors/subdomain.py` speelt voor het `domains:`-blok.

```python
from opi.services.catalog.publish_on_web.domain_config import (
    DomainSetting,
    get_domain_setting,
    pop_domain_setting,
    set_domain_setting,
)

base_domain = get_domain_setting(deployment, DomainSetting.BASE_DOMAIN)
set_domain_setting(deployment, DomainSetting.SUBDOMAIN, "wies")
pop_domain_setting(deployment, DomainSetting.ISSUER)
```

- **Lezers** kijken eerst in de dienstconfiguratie en dan in de deploymentwortel, zodat een
  nog niet gemigreerd bestand blijft werken.
- **Schrijvers** landen altijd op het dienstpad en halen de wortelkopie weg, zodat de staat
  niet kan splitsen.
- `tests/test_publish_on_web_one_read_path.py` scant de broncode en laat de suite falen als
  er ergens weer rechtstreeks `deployment.get("base-domain")` verschijnt.

Voor het formulier bestaat `domain_setting_path(setting, deployment_index=None)`: dat levert
het `yaml_path` waarop de editable en de resolvermap allebei sleutelen.

## Migratie

De stap v2.6 → v2.7 (`relocate_domain_settings_to_service`) verplaatst de velden bij het
laden van een bestand en haalt de wortelkopie weg. Hij is idempotent en bewust smal: een
deployment zonder webadres krijgt **geen** lege `publish-on-web`-ingang, want die zou lezen
als "deze deployment gebruikt de dienst".

Een bestand dat nog op 2.6 staat, valideert tegen het schema van 2.6 (de legacy patch
`opi/schemas/project_legacy/v2.6.json` bevat de zeven velden nog in de deploymentwortel) en
verhuist bij zijn volgende laadbeurt. Op de nieuwste versie worden ze in de wortel
afgewezen: zolang beide vormen valideren kan een nieuwe schrijver de oude terugzetten.

## API

De velden zijn bereikbaar via de generieke dienstconfiguratie-endpoints op de
deploymentlaag:

```
GET    /api/v2/projects/{project}/services/publish-on-web/config?target=deployment
PUT    /api/v2/projects/{project}/deployments/{deployment}/services/publish-on-web/config
DELETE /api/v2/projects/{project}/deployments/{deployment}/services/publish-on-web/config
```

De exacte routes staan in de OpenAPI-spec van een draaiende instantie; ze worden gegenereerd
uit wat de dienst per laag declareert, niet met de hand onderhouden.

## Bijgeleverde reparaties

Drie dingen die stil kapot waren en dit werk blokkeerden:

- `delete_value` in de padtaal negeerde een `{filter}`-segment, waardoor `remove_when_none`
  niets deed op elk dienstconfigpad: een leeggemaakt veld hield zijn oude waarde.
- Het `{K}`-filter kende alleen de legacy vorm `{K: {...}}` en niet het uniforme record
  `{reference: K, config: ...}` — precies wat de v2.4-migratie van elke component- en
  deploymentingang maakt.
- `_extract_section_data` behandelde `services{X}` als een gewoon veld, waardoor een
  formuliersectie de hele dienstenlijst van een deployment verving. Clone-state en een
  cross-domain-patch konden daarmee verdwijnen; die lijst wordt nu per naam samengevoegd.

## Twee gevolgen van de verhuizing die eigen poorten kregen

**Een clone erft het webadres niet.** `upsert_deployment` kopieerde de brondeployment en
sloot daarbij de vijf wortelsleutels uit (`subdomain`, `base-domain`, `domain-mode`,
`domain-format`, `issuer`). Zodra die waarden onder de dienst staan is dat een no-op: ze
reizen mee in het `services`-blok, dat als geheel gekopieerd wordt. Het webadres wordt nu
**na** de kopie verwijderd, met `clear_domain_settings()` — dezelfde autoriteit over de
locatie als de lezers en schrijvers — en de door de aanroeper gevraagde instellingen worden
daarna opnieuw geschreven, want de kopie liep er anders overheen. Een clone landt dus op het
clusteradres, niet op de hostnamen van de bron. Merk op dat de clone nu ook
`root-component` en `expose-component-on-bare-domain` laat vallen; die werden voorheen
geërfd en daarna voorwaardelijk opgeruimd.

**Het kale domein wordt op elke schrijfweg getoetst.** `expose-component-on-bare-domain` is
sinds deze verhuizing ook via de dienstconfiguratie-PUT te zetten, en die body hoeft geen
`domain-format` te bevatten. De regel "kaal domein alleen voor eigen domeinen, nooit voor een
platformdomein" stond in `DomainConfigEnforcer` achter de vroege `if not domain_format:
return` en was daarmee alleen vanuit de wizard bereikbaar. Hij staat nu vóór die uitstap —
de regel hangt niet van het formaat af — en wordt bovendien op het publicatiepad afgedwongen,
vlak voor `register_bare_domain` en voor het renderen van de apex-ingress. Eén regel,
`validate_bare_domain_allowed()` in `connectors/subdomain.py`, aangeroepen door beide.
