# Impliciete dienstselectie: een dienst die zichzelf mag aanmelden

Via de API kun je een dienst aan een component of een deployment hangen. Tot nu toe moest
die dienst altijd al op projectniveau gekozen zijn, anders kreeg je een fout. Voor een
dienst die op projectniveau niets te kiezen heeft - een database, opslag - was dat een
tussenstap die alleen maar werk was. Voor een dienst waar het project juist iets moet
vastleggen voordat de rest betekenis heeft - op welke domeinen mag dit project publiceren -
was het precies de goede fout.

Het antwoord verschilt dus per dienst, en het staat sinds RC-84 **bij de dienst**.

## Hoe het werkt

Twee haken op `Service` (`opi/services/catalog/base.py`):

```python
class MijnService(Service):
    #: Mag deze dienst zichzelf op de projectlaag zetten? Standaard False.
    allows_implicit_project_selection = True

    def implicit_project_config(self) -> ServiceConfigData | None:
        """De standaardconfiguratie op de projectlaag; None = een kale selectie."""
        return None
```

`Service.implicit_project_entry()` combineert de twee tot het projectbestand-item, of
`None` als het niet mag:

| Situatie | Antwoord |
|---|---|
| De dienst draagt **geen projectlaag** (`config_layers()`) | `"mijn-dienst"` (kale selectie), wat hij verder ook verklaart |
| De basisklasse, met een projectlaag (de dienst zegt niets) | `None` - eerst op projectniveau aanzetten |
| `allows_implicit_project_selection = True`, geen configuratie | `"mijn-dienst"` (kale selectie) |
| ... met een standaardconfiguratie | `{"name": "mijn-dienst", "config": {...}}` |
| De dienst declareert goedkeuringen (`config_approvals`) én heeft een projectlaag | `None`, wat hij verder ook verklaart |
| Ja gezegd, maar de standaardconfiguratie valideert niet | `TypeError` - luid, bij de eerste aanroep |

Drie regels staan hier in plaats van in elke dienst:

* **Zonder projectlaag is er niets te weigeren** (RC-103). Een weigering stuurt de
  aanroeper naar de projectlaag; heeft de dienst die niet, dan verwijst de melding naar een
  laag die `GET /v2/services/{naam}` niet meldt en die geen enkel endpoint kan zetten. Deze
  regel gaat vóór de twee hieronder, want die beschermen allebei een projectbesluit - en
  dat is er dan niet. Dit is wat `publish-on-web` blokkeerde: drie schrijfwegen weigerden op
  een eis waaraan niet te voldoen was.
* **Een goedkeuring wint altijd.** Een dienst met een projectlaag die zichzelf aanmeldt en
  daarmee een goedkeuring van een beheerder omzeilt, is een gat. `config_approvals`
  blokkeert de impliciete selectie, ongeacht de vlag. Een kale selectie op een dienst
  zónder projectlaag draagt geen goed te keuren inhoud: de domeinen van `publish-on-web`
  zijn een deployment-besluit en houden hun eigen goedkeuring.
* **Ja zeggen verplicht tot een geldig item.** Voldoet de standaardconfiguratie niet aan het
  eigen projectlaag-model van de dienst, dan is dat een programmeerfout in die dienst; die
  faalt hier hard in plaats van een projectbestand te schrijven dat de opslagpoort afkeurt.

## Waar het gevraagd wordt

Beide schrijfwegen van de API vragen dezelfde haak, via
`ServiceAdapter.ensure_project_selection(project_data, *namen)`:

* `POST/PATCH /api/v2/projects/{p}/components` met een `services`-lijst
  (`ProjectManager.add_component` / `update_component`);
* `PUT /api/v2/projects/{p}/services/{svc}/config/{component|deployment}/{naam}`
  (`ServiceAdapter.set_service_config`).

Mag het, dan komt de dienst in de projectlijst en gaat de handeling door - het schrijven
loopt via `save_and_commit_project`, dezelfde gevalideerde poort als elke andere wijziging.
Mag het niet, dan komt er een fout die de dienst bij naam noemt (`error_type:
invalid_services`, HTTP 400), en het projectbestand blijft ongemoeid. Ook bij een lijst met
meerdere diensten wordt er niets geschreven als er één bij zit die het niet mag.

## Wat die fout zegt

De weigering is de goede uitkomst, maar hij was alleen een *reden* en geen *volgende stap*:
"they need project-level configuration that cannot be assumed". Wie de authorization-wall
aan een component hing wist daarna dat er iets ontbrak, en niet welk verzoek het aanvult.
De drie eisen van die dienst (`publish-on-web`, `keycloak`, en keycloaks `restrict-access`)
vielen er daarna alsnog een voor een uit, elk als een eigen mislukte aanroep (zad-cli,
bevinding 21).

De melding draagt nu per geweigerde dienst drie dingen:

* **Het verzoek dat de weigering opheft**, met de echte projectnaam erin:
  `PUT /api/v2/projects/{p}/services/{svc}/config/project`. Het pad komt uit
  `config_endpoint_path` (`catalog/base.py`), dezelfde bron waaruit de v2-router zijn
  routes registreert, zodat de melding niet stilzwijgend kan verouderen.
* **Waar de body beschreven staat**: `GET /api/v2/services/{svc}`.
* **De eisen uit `requires` die dit project nog niet heeft**, en alleen die
  (`unmet_service_requirements`). Een lijst die opsomt wat er al staat leest als een muur in
  plaats van als een volgende stap.

Het endpoint wordt **alleen genoemd als het bestaat**. De generieke configroute wordt
gegenereerd voor een laag met een configmodel, en een dienst kan een projectlaag hebben
zonder zo'n model: `attachments` definieert daar een catalogus onder `data` en heeft er
helemaal geen configblok, dus die route is nooit gegenereerd. Die dienst wordt naar zijn
eigen beschrijving verwezen, waar de acties staan die er wél iets neerzetten. Het
alternatief -- het pad toch noemen -- zou één doodlopende weg inruilen voor een ergere: een
404 op een verzoek dat de melding zelf aanraadde. `tests/test_visible_blockers.py` houdt
iedere weigering tegen de werkelijk geregistreerde routes.

## Het antwoord per dienst

21 dienstpakketten, 15 mogen zichzelf aanmelden, 6 niet. Twijfelgevallen horen bij nee: een
dienst die per ongeluk ontstaat met een verzonnen standaard is erger dan een foutmelding -
maar alleen als er een projectlaag is waar die keuze in past. Elk van de zes weigeraars
heeft die laag, en de test `test_a_refusal_always_names_a_layer_the_caller_can_reach`
bewaakt dat.

| Dienst | Mag | Waarom |
|---|---|---|
| `postgresql-database` | ja | Zonder configuratie is het een shared-scope database - precies wat een project krijgt dat de dienst met de hand aanzet. Project-scope of extra schema's blijft een expliciete daad. |
| `namespace-postgresql-database` | ja | Elk veld van de projectconfiguratie reproduceert `DatabaseManager.DEFAULT_CONFIG`; een lege configuratie geeft hetzelfde cluster als de wizard. |
| `minio-storage` | ja | Een bucket vraagt geen keuze op projectniveau. `enable-versioning` valt terug op de platformstandaard; de rest van het model is kloonstatus die het platform zelf schrijft. |
| `redis` | ja | Het enige projectveld (`acl-key-prefix`) staat standaard op de smalle, veilige waarde. |
| `namespace-redis` | ja | Draagt helemaal geen projectconfiguratie; een expliciete selectie zou een besluit vastleggen dat niemand nam. |
| `persistent-storage` | ja | De mounts staan op het component; de projectlaag bevat geen opslagbesluit. |
| `temp-storage` | ja | Idem. |
| `health-check` | ja | De probe wordt op het component ingesteld; op projectniveau valt niets te kiezen. |
| `metrics-scraper` | ja | Het scrapen wordt op het component ingesteld; op projectniveau valt niets te kiezen. |
| `platform` | ja | Systeemdienst: geen gebruikerskeuze, dus ook geen besluit dat vooraf hoort. |
| `resource-tuning` | ja | Systeemdienst, idem. |
| `deployment-health` | ja | Systeemdienst, idem. |
| `user-env-vars` | ja | Systeemdienst waarvan de waarden een eigenschap van het component zijn. |
| `aliases` | ja | Systeemdienst, idem. |
| `publish-on-web` | ja (RC-103) | Draagt geen projectlaag: de domeinkeuze en de goedkeuringen (`domain`, `subdomain`) zitten op de **deployment**, niet op het project. Er was dus geen projectbesluit om op te wachten en ook geen endpoint om het te nemen - de weigering wees naar een laag die niet bestaat. De goedkeuring per deployment staat onveranderd. |
| `vlam` | ja (RC-142) | Draagt op geen enkel niveau configuratie: een endpoint uit de clusterconfiguratie, een variabele, een netwerkregel. Er valt op projectniveau dus niets te beslissen. Of het cluster de dienst kan leveren is een andere vraag, en die weigert `available_on_cluster` bij het opslaan. |
| `keycloak` | **nee** | Realm en template zijn een keuze; een verzonnen realm is direct zichtbaar in de authenticatie van de gebruiker. |
| `authorization-wall` | **nee** | Een muur voor de applicatie zetten is een beveiligingsbesluit op projectniveau, niet een bijeffect van een componentwijziging. |
| `cross-domain-access` | **nee** | Legt vast welke domeinen elkaar mogen bereiken; zonder die keuze betekent de dienst niets. |
| `sleep-mode` | **nee** | Een beleidsbesluit (wanneer mag dit project in slaap vallen) dat op projectniveau hoort. |
| `invite` | **nee** | Bepaalt wie er uitgenodigd mag worden; geen standaard te verzinnen. |
| `attachments` | **nee** | Definieert op projectniveau een catalogus (`ConfigRole.DEFINE`). Een component verwijst naar een definitie die er eerst moet zijn; impliciet aanmaken zou naar een lege catalogus verwijzen. |

De lijst staat als poort in `tests/test_implicit_project_selection.py`
(`IMPLICIT_SERVICES`): een nieuwe dienst kan er niet stilzwijgend bij komen, want de
basisklasse zegt nee zodra er een projectlaag is, en de test noemt de verzameling bij naam.
Een nieuwe dienst *zonder* projectlaag komt er wel bij - dat is de bedoelde regel, en de
poort dwingt af dat je hem in de tabel hierboven verantwoordt.

## En de UI?

**Deze taak verandert de UI niet.** De wizard vraagt nog steeds om de dienst eerst op
projectniveau aan te vinken. De aanleiding was de API en het antwoord is daar geïmplementeerd.

De haak maakt een vervolg mogelijk zonder verbouwing: de wizard zou bij het aanvinken van
een dienst op een component dezelfde `ensure_project_selection` kunnen aanroepen en de
projectstap voor die dienst overslaan. Dat is een eigen taak, met een eigen ontwerpvraag
(wat toont de projectstap dan nog, en hoe ziet de gebruiker wat er impliciet is ontstaan),
en het is bewust hier niet meegenomen.

## Afhankelijkheden

Geen nieuwe. De haak leunt op wat er al was: `config_approvals(layer)`, `config_layers()`,
`config_model_for(layer)` en het bestaande opslagpad `save_and_commit_project`.
