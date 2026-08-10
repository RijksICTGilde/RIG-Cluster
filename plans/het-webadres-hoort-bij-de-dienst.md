# Het webadres hoort bij de dienst

Status: plan, 10 augustus 2026. Alle getallen gemeten op `operations-manager/python` op branch `naar-het-nieuwe-componentensysteem`.

## Wat er aan de hand is

De consolidatie van dienstconfiguratie naar een `config`-blok onder de dienst is halverwege blijven staan. Voor publish-on-web is een deel verhuisd en een deel niet, en wie een projectbestand openslaat ziet dat meteen. In `hwt-nqi.yaml` staat de TLS-instelling netjes onder de dienst bij het component, en staat `domain-format` los in de wortel van de deployment:

```yaml
components:
  - name: component1
    services:
      - reference: publish-on-web
        config:
          tls: standard          # onder de dienst, zoals bedoeld
deployments:
  - name: test
    domain-format: component-deployment-project   # los in de wortel
```

Dat is niet één keer een uitschieter. Vijf velden in `$defs/deployment` beschrijven samen precies één ding, namelijk hoe publish-on-web een hostnaam samenstelt en met welk certificaat: `base-domain`, `subdomain`, `domain-mode`, `domain-format` en `issuer`. Daar komen `root-component` en `expose-component-on-bare-domain` bij, die alleen betekenis hebben binnen een gekozen `domain-format`. Zeven velden dus, allemaal eigendom van één dienst, allemaal buiten die dienst opgeslagen.

## Wat er al wel verhuisd is, en wat niet

Wel verhuisd, in twee stappen:

- **Componentniveau**: `tls` en `attachment`, in `components[*]/services{publish-on-web}/config/...`. Gedeclareerd in `catalog/publish_on_web/editables.py`, getypeerd in `config_model.py`.
- **Projectniveau**: het `domains:`-goedkeuringsblok, verplaatst van de projectwortel naar `services/[publish-on-web]/config/domains` door migratie v2.4 naar v2.5 (`normalize_domains_location`, RC-5). `connectors/subdomain.py` is sindsdien de enige autoriteit over die locatie, en lezers accepteren beide plekken zodat een niet-gemigreerd bestand blijft werken.

Niet verhuisd: de zeven deploymentvelden hierboven. Er is geen migratiestap voor. De keten in `schema_migration.py` loopt van 2.1 tot 2.6 en alleen 2.5 (domains) en 2.6 (invites) verplaatsen iets naar een dienstconfiguratie.

## Waarom het zo gelopen is

Chronologie, geen ontwerp. `domain-format` is geïntroduceerd op 13 maart 2026 (commit `0019be5f`, de edit wizard). Dienst-eigen configuratiemodellen bestaan pas sinds 1 augustus 2026 (commit `0884800f`, "laat elke service zijn eigen config beschrijven"). De velden waren er dus bijna vijf maanden voordat er een plek onder een dienst was om ze in te zetten.

Er staat één expliciete rechtvaardiging in de code, in de moduledocstring van `catalog/publish_on_web/__init__.py`:

> Still NOT owned here: cross-project platform infrastructure the service depends on but does not own: the deployment-level "Webadres" domain wizard (DOMAIN_SECTION), (...) and ingress generation (project_manager / naming.py).

En in `catalog/publish_on_web/editables.py`:

> the deployment-level domain wizard and root `domains:` handling are platform-infra, not owned by this service

Die tweede zin is inmiddels half achterhaald, want het `domains:`-deel is er in v2.5 juist wél uit gehaald. Wat overeind blijft is een reëel argument: hostnaamsamenstelling is deployment-breed, ingressgeneratie zit in `project_manager` en `naming.py`, en de goedkeuringsstroom raakt de globale subdomeinregistratie. Maar dat is een argument over wie de *code* bezit, niet over waar de *waarde* wordt opgeslagen. Voor `domains:` gold precies hetzelfde bezwaar en dat blijkt geen bezwaar te zijn geweest.

## Wat er onderweg boven kwam: een schemagat

`expose-component-on-bare-domain` wordt geschreven door `DOMAIN_BARE_DOMAIN_COMPONENT_EDITABLE` (`forms/editables/fields/domains.py:78`) en gelezen op zes plekken, waaronder `project_manager.py:5142`, `project_manager.py:5455` en `keycloak_manager.py:163`. Het staat **niet** in `$defs/deployment`, en dat def heeft `additionalProperties: false`.

Gereproduceerd tegen het echte schema:

```
FAALT: Additional properties are not allowed ('expose-component-on-bare-domain' was unexpected)
```

Dit is dezelfde klasse als de dp-bn7-storing: een veld dat de code schrijft en het schema afwijst, waarna elke reprocess stil op `validate_project_schema` valt en deploys blokkeert zonder dat iemand een foutmelding ziet. Het veld is binnengekomen met PR #38 en komt in geen enkel voorbeeldproject in `projects/` voor, dus de kans is groot dat het pad nooit in productie gelopen heeft. Dat maakt het niet minder een gat.

Dit hoort niet te wachten op de rest van dit plan, en het moet vooraan omdat elke migratie die deployments aanraakt er anders overheen struikelt.

## De doelvorm

Alles wat over het webadres gaat onder de dienst, op het niveau waar de waarde geldt:

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

Dat pad is geen nieuwe uitvinding. `catalog/base.py:121` kent de laag al:

```python
ConfigLayer.DEPLOYMENT: "deployments[*]/services{{{svc}}}",
```

En `config_path(ConfigLayer.DEPLOYMENT, ServiceType.PUBLISH_ON_WEB, "config", "domain-format")` levert precies het pad hierboven. De padtaal in `forms/editables/path.py` ondersteunt de `{K}`-filtersyntaxis in gemengde lijsten, `apply_virtualize` vervangt het eerste `services`-segment op elke diepte, en `validate_service_configs` valideert de deploymentlaag al (`project_validation.py:175-182`). De API kent de laag ook: `_CONFIG_WRITE_LAYERS` in `api/v2/router.py:2055` bevat `ConfigLayer.DEPLOYMENT`.

Er is bovendien een werkend voorbeeld van precies deze vorm. `cross_domain_access` bezit een volledige deployment-formuliersectie via `deployment_form_section(deployment_index)`, gebouwd op `config_path(ConfigLayer.DEPLOYMENT, ...)` (`catalog/cross_domain_access/editables.py:172`) en ingehangen in `forms/visualizers/flows.py:431-440`. Dat is het patroon dat publish-on-web hier hoort te volgen.

## Wat het raakt, gemeten

Directe dict-toegang op de zeven velden (`.get("x")` of `["x"]`) in `opi/`, zonder tests:

```
114 plaatsen in 22 bestanden

 29  opi/manager/project_manager.py
 26  opi/web/router.py
 14  opi/utils/project_utils.py
  5  opi/connectors/subdomain.py
  5  opi/api/v2/router.py
  4  opi/services/catalog/publish_on_web/urls.py
  4  opi/manager/keycloak_manager.py
  3  opi/forms/visualizers/display_blocks.py
  3  opi/forms/editables/generators.py
  3  opi/forms/editables/enforcers.py
  2  opi/services/persistence/subdomain_registry.py
  2  opi/services/catalog/publish_on_web/__init__.py
  2  opi/manager/project_validation.py
  2  opi/manager/bootstrap_manager.py
  2  opi/forms/editables/hooks.py
  2  opi/core/backup_tasks.py
  + zes bestanden met er één
```

Niet elk van die 114 is een deploymentveld, want `subdomain` en `issuer` zijn ook woorden in de subdomeinregistratie en in de clusterconfiguratie. Dat scheiden is het eerste werk van fase 1 en niet iets om nu te schatten.

Daarnaast de formulierlaag:

```
8  editables met deze yaml_path   (fields/domains.py 4, fields/deployments.py 4)
5  depends_on die ernaar verwijzen (fields/domains.py)
4  layoutverwijzingen in DOMAIN_SECTION (wizard_sections.py:274-281)
```

Dat `fields/domains.py` en `fields/deployments.py` **allebei** een editable definiëren voor `deployments[*]/subdomain`, `base-domain` en `domain-format`, met verschillende `values_provider`s en verschillende validators, is een tweede vondst. Twee definities voor hetzelfde pad in twee stromen is precies het soort duplicatie dat deze verhuizing zou moeten opruimen, en het is ook een risico bij de verhuizing zelf: wie er één omzet en de ander vergeet, krijgt een stroom die naar het oude pad blijft schrijven.

Tests die de velden noemen: `domain-format` 53, `base-domain` 96, `domain-mode` 25, `subdomain` 130, `issuer` 36. Die getallen overlappen en zijn ruw, maar ze geven de orde: dit is geen wijziging van een handvol asserties.

## De fasering

**Fase 0: het schemagat dichten.** `expose-component-on-bare-domain` toevoegen aan `$defs/deployment`, met het type dat de lezers verwachten (een componentnaam als string, of `false`, want `keycloak_manager.py:163` leest hem met een `False`-default). Verifieerbaar: een test die een deployment met dat veld door `validate_project_schema` haalt en groen is, plus een test die faalt zolang het veld ontbreekt in het schema. Eerst uitzoeken of de wizard hem daadwerkelijk kan wegschrijven, want als het pad onbereikbaar is verandert dat de urgentie maar niet het besluit. Dit is los shipbaar en hoort niet te wachten.

**Fase 1: inventariseren welke van de 114 echt deploymentvelden zijn.** Een lijst per bestand met per plaats: leest hij een deployment, of iets anders dat toevallig zo heet. Zonder die scheiding is elke verdere fase gokwerk. Verifieerbaar: de lijst is compleet, en het restant (`subdomain` in de registratie, `issuer` in de clusterconfiguratie) is expliciet als "raakt dit niet" gemarkeerd.

**Fase 2: één leespad.** Voordat er iets verhuist, gaat elke lezer door een functie die op beide plekken kijkt: eerst de dienstconfiguratie, dan de deploymentwortel. Precies het patroon van `get_domains_config` in `connectors/subdomain.py`, en om dezelfde reden: een niet-gemigreerd bestand moet blijven werken. Deze functies horen in `catalog/publish_on_web/`, want de dienst bezit het contract. Verifieerbaar: geen enkele plaats uit fase 1 leest nog rechtstreeks uit de deploymentwortel, met een test die dat afdwingt zoals `test_service_package_is_self_contained.py` dat voor de pakketgrens doet.

**Fase 3: één schrijfpad.** Dezelfde beweging voor de schrijvers, met een `ensure_`-functie als enige autoriteit over de locatie, zodat de migratie en de looptijd het nooit oneens kunnen zijn. De zwaarste schrijvers zitten in `web/router.py:2688-2710` (de modal-edit van het webadres, die velden ook `del`t) en `utils/project_utils.py:402-457` (die ze bij projectcreatie zet, twee keer, in twee bijna identieke blokken). Verifieerbaar: een schrijfactie via de wizard en via de API landt aantoonbaar op het dienstpad, gemeten aan het opgeslagen bestand en niet aan de code.

**Fase 4: de migratie v2.6 naar v2.7.** Eén stap in `MIGRATION_STEPS`, die de zeven velden van de deploymentwortel naar `deployments[*]/services{publish-on-web}/config/` verplaatst, de dienstingang aanmaakt als hij er niet is, en de wortelkopie weghaalt zodat de staat niet splitst. Idempotent, met dezelfde vorm als `normalize_domains_location`. Let op de bestaande guard: `SCHEMA_VERSIONS` en `MIGRATION_STEPS` moeten allebei bij, anders faalt de import. Verifieerbaar: een echt projectbestand (`hwt-nqi.yaml`) door `migrate_to_latest()` en dan valideren, in die volgorde, want dat is de les uit dp-bn7.

**Fase 5: de formulierlaag.** De acht editables en de vijf `depends_on` naar het nieuwe pad, met `virtualize=SERVICE_VIRTUALIZE`, en de dubbele definities in `fields/domains.py` en `fields/deployments.py` samenvoegen tot één set die de dienst bezit. `DOMAIN_SECTION` wordt gebouwd door de dienst, zoals `cross_domain_access.deployment_form_section` dat doet, en `registry.py` krijgt de `deployment_service_editables()` die er nu nog niet is (er is alleen een `deployment_component_service_editables`). Verifieerbaar: de bestaande e2e-wizardtests blijven groen, en er komt er één bij die de *uitkomst* controleert en niet alleen de stap, conform de eerdere les dat die tests groen bleven op een kapotte create.

**Fase 6: het schema opruimen.** De zeven velden uit `$defs/deployment` halen en de vorm vastleggen in `PublishOnWebConfig`, waarmee `$defs/publish-on-web-config` en `$defs/domains` een stap dichter bij hun aangekondigde pensioen komen. Pas nadat fase 4 op alle projectbestanden gedraaid heeft. Verifieerbaar: een oud bestand valideert na migratie, een nieuw bestand met de velden in de wortel wordt afgewezen.

## De open beslissing

Publish-on-web is `binding=ServiceBinding.COMPONENT` (`catalog/publish_on_web/__init__.py:235`). Een ingang op deploymentniveau is voor deze dienst dus een nieuw soort record, en dat is de enige echte ontwerpvraag in dit plan: gaat de dienst daarmee ergens in de UI of de API lezen als "aangezet op deploymentniveau", terwijl hij per component wordt aangezet.

Wat ik gecontroleerd heb: de reconciliatie in `jobs/reconciliation.py` kijkt alleen naar database- en minio-diensten, dus daar is een publish-on-web-ingang onschadelijk. `_validate_services_listed_once` (`project_validation.py:432`) controleert alleen dubbelingen. Wat nog gecontroleerd moet worden voordat fase 4 begint: `handlers/project_file_handler.py:2088-2153`, dat de deploymentdienstenlijst herschrijft, en de dienstenoverzichten in `api/v2/router.py:1797-1805` en in de portalpagina's.

Als dat bezwaar echt blijkt, is het alternatief niet "dan maar in de wortel laten" maar een `binding` die twee lagen toestaat. Dat is een groter gesprek en hoort dan een eigen punt te worden, niet stilzwijgend hierin te verdwijnen.

## Waar op te letten

**Dit is geen gedragsverandering en moet dat ook niet worden.** Elke gerenderde hostnaam, elk ingress en elk certificaat hoort na afloop byte-identiek te zijn. De veiligste meting is een gegenereerd manifest van een echt project voor en na, vergeleken met `diff`, en niet een testsuite die groen is.

**Migreer niet voordat er één leespad is.** De volgorde in dit plan (lezers eerst, dan schrijvers, dan migratie) is dezelfde als bij RC-5 en om dezelfde reden: zodra de migratie draait staat de waarde op de nieuwe plek, en elke lezer die nog rechtstreeks in de wortel kijkt krijgt vanaf dat moment stil `None` terug. Dat is precies het soort fout dat pas bij een deploy opvalt.

**Validatiefouten bij reprocess worden stil geslikt.** Dat was de kern van dp-bn7 en het is nog steeds zo. Voer de sandboxcontrole daarom uit tegen de git projects-repo en niet tegen wat een logregel beweert.

**`domain-mode` is legacy en blijft dat.** Het is opgevolgd door `domain-format` en `HostnameFormat.from_domain_mode` bestaat alleen nog om oude bestanden te lezen. Het verhuist mee, want twee plekken voor hetzelfde onderwerp is de fout die dit plan opheft, maar het verdient geen nieuwe wizardvelden of nieuwe validators.

**De tests noemen deze velden honderden keren.** Reken op meer testwerk dan codewerk, en gebruik het als sturing: een fase die veel tests aanraakt zonder gedrag te wijzigen is een fase die te breed is opgezet.


History:
  2026-08-10 13:21:11  created — ship
  2026-08-10 13:21:37  dispatched — Worker session: dclaude-RIG-Cluster-rc60
  2026-08-10 13:24:39  pr_opened — PR #59
  2026-08-10 13:25:09  feedback — Aanscherping: de drie lagen van publish-on-web dienen elk een ander doel (project=goedgekeurde domeinen, deployment=hoe het adres wordt samengesteld, component=gebruikt-de-dienst plus TLS). Verdeling is dus niet het probleem; het probleem is dat 2 van de 3 lagen het dienstpad gebruiken en de deploymentlaag niet. Gevolg: splits PublishOnWebConfig in drie modellen, een per laag, via Service.config_model_for(layer) (catalog/base.py:697) - bestaat al, wordt al gebruikt door attachments, persistent-storage, temp-storage, postgresql-database. Anders wordt het een zak van 10 optionele velden waarin niets tegenhoudt dat tls op de deployment belandt. Planbestand plans/het-webadres-hoort-bij-de-dienst.md is bijgewerkt.
  2026-08-10 13:25:09  feedback_delivered — Delivered to running session(s): dclaude-RIG-Cluster-rc60

Approvals/Feedback:
  2026-08-10 13:25:09  feedback — Aanscherping: de drie lagen van publish-on-web dienen elk een ander doel (project=goedgekeurde domeinen, deployment=hoe het adres wordt samengesteld, component=gebruikt-de-dienst plus TLS). Verdeling is dus niet het probleem; het probleem is dat 2 van de 3 lagen het dienstpad gebruiken en de deploymentlaag niet. Gevolg: splits PublishOnWebConfig in drie modellen, een per laag, via Service.config_model_for(layer) (catalog/base.py:697) - bestaat al, wordt al gebruikt door attachments, persistent-storage, temp-storage, postgresql-database. Anders wordt het een zak van 10 optionele velden waarin niets tegenhoudt dat tls op de deployment belandt. Planbestand plans/het-webadres-hoort-bij-de-dienst.md is bijgewerkt.
