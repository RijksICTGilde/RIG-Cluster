# Config bewerkbaar op de laag waar hij hoort

Een dienst declareert waar zijn config staat (`config_editables`, `config_api_fields`,
`config_component_layout`) en waar je hem bewerkt (`config_form_section`). Die tweede haak
was bij de helft van de diensten niet ingevuld. Gemeten op 4 augustus 2026 over alle
diensten: zeven droegen config op een laag zonder ergens een formuliersectie, en twee
beantwoordden de haak op een *andere* laag dan waar hun config staat.

Het gevolg was niet één kapotte sectie maar een stille toestand: het veld bestaat, het
model valideert het, de API accepteert het -- en geen enkele gebruiker kan erbij.

## De regel

Elke laag waar een dienst config draagt heeft een *antwoord*: een `config_form_section`
op die laag, of een vermelding in `form_exempt_layers` met de reden. Bewust geen formulier
en vergeten een formulier zien er daarmee anders uit in de code.

`tests/test_service_config_layers.py` houdt elke dienst daaraan, in beide richtingen: een
laag met config zonder antwoord faalt, en een vrijstelling voor een laag zonder config
faalt ook (dat zou blijven slagen nadat de config die hij excuseerde verhuisd is).

## Hoe een laag antwoord geeft

**Projectlaag** -- de dienst bouwt zijn eigen `FormSection` (een aparte wizardstap), en die
wordt met de hand in de flows gehangen. Ongewijzigd; zie `instructions/services.md`.

**Component- en deployment-componentlaag** -- die velden zitten *ingebed* in het
componentformulier, niet in een eigen stap, dus er valt per dienst niets te schrijven. De
basisklasse bouwt de sectie uit wat de dienst voor die laag al declareert (zijn
visualizers + zijn layout-nodes). `health-check`, `metrics-scraper`, `publish-on-web`,
`persistent-storage` en `temp-storage` beantwoorden de haak daardoor zonder één regel eigen
code, en de sectie kan per definitie geen ander veldenpakket tonen dan het
componentformulier zelf.

**`form_exempt_layers`** -- een dict van laag naar reden:

```python
class MinioStorageService(Service):
    form_exempt_layers = {
        ConfigLayer.DEPLOYMENT: "clone state (generation/revisions) written by revision_manager, not by a user"
    }
```

## Wat er is rechtgezet

| Dienst | Was | Nu |
|---|---|---|
| `health-check`, `metrics-scraper`, `publish-on-web`, `persistent-storage`, `temp-storage` | config op component, geen sectie | componentsectie, afgeleid van hun eigen declaraties |
| `attachments` | config op component, sectie op project | beide: de projectsectie die hij had, plus de componentlaag |
| `redis` | `acl-key-prefix` in het model en op de API, nergens een veld | projectsectie + eigen configuratie-modal |
| `minio-storage` | `enable-versioning` idem | projectsectie + eigen configuratie-modal; deployment-laag als OPI-beheerd gedeclareerd |
| `cross-domain-access` | config op project en deployment, alleen een projectsectie | deployment-laag expliciet gedeclareerd (zie hieronder) |

### redis: sleutels beperken tot dit project

Elke deployment krijgt een eigen Redis-ACL-gebruiker. Aan (standaard) mag die alleen
sleutels met het voorvoegsel `{deployment}-{project}:`; uit komt hij bij elke sleutel in de
gedeelde instantie. Zet dit alleen uit voor applicaties die hun sleutels niet kunnen
voorvoegen. Te vinden onder "Redis configuratie" in de wizard, in "Services beheren", en
achter de Configureer-knop van de dienst.

**Waarom dit een self-service-vinkje is en geen approval.** Uitzetten is een echte
verruiming, dus de vraag is terecht gesteld of hier een `ApprovalSpec` met
`ApproverScope.PLATFORM_ADMIN` hoort, zoals bij een domein van `publish-on-web`. De
beslissing is: nee, en wel hierom.

- Het is geen nieuwe bevoegdheid. Dezelfde waarde was al te zetten via de config-API van
  de dienst en door het projectbestand te bewerken, onder precies dezelfde autorisatie.
  Het veld maakt een bestaande knop vindbaar; het verplaatst geen grens.
- Een approval is niet gratis: hij vraagt om opgeslagen approval-state in het
  projectbestand, een `list_items`/`record`/`notices_for`-drieluik, een plek in de
  approver-interface en handhaving in `redis_manager`. Dat is een eigen wijziging, geen
  bijproduct van het invullen van een formuliersectie.
- Wat wél nodig was, is zichtbaarheid: `redis_manager` logt het uitzetten nu op WARNING
  in plaats van INFO, zodat een platformbeheerder in de centrale log kan terugvinden welk
  project de beperking heeft losgelaten.

Wie de approval alsnog wil, bouwt hem als losstaande wijziging; het veld hierboven hoeft
er dan alleen achter te worden gehangen.

### minio-storage: versiebeheer op de bucket

Bewaar eerdere versies van objecten, zodat een overschrijving of verwijdering terug te
draaien is. Kost extra opslag, evenredig met hoe vaak objecten wijzigen. Zelfde drie
plekken.

### cross-domain-access: de deployment-laag

De deployment-laag van deze dienst is een *patch* op de projectregels (zie `merge.py`):
gebruikersconfig, geen OPI-state, maar er is nog geen formulier voor. Een patch-editor is
eigen werk. De laag staat daarom in `form_exempt_layers` met die reden, zodat het gat
zichtbaar is in plaats van eruit te zien als een vergissing. Vandaag bewerk je die regels
via de API of het projectbestand.
