# publish-on-web is niet meer aan te zetten (blokkerend, regressie)

Status: plan, 13 augustus 2026. Vraag 12 uit `plans/vragen-uit-zad-cli.md`. **Dit blokkeert de zad-cli en het is een regressie van onze kant**: op `edbda374` liep hun draaiboek nog helemaal door, op `5c026ecc` lukt het via geen enkele weg meer, ook niet op een vers project.

## Wat er gebeurt

Drie routes, dezelfde fout:

```
PUT   /v2/projects/{p}/services/publish-on-web/config/component/frontend
PATCH /v2/projects/{p}/components/frontend  {"services":["publish-on-web"]}
POST  /v2/projects/{p}/components           (met de dienst er meteen bij)

-> "Services that must be enabled at project level first: ['publish-on-web'].
    They need project-level configuration that cannot be assumed."
```

En die projectlaag bestaat niet:

```
GET /v2/services/publish-on-web -> layers: ["component","deployment","deployment-component"]
```

## De oorzaak, gemeten

Dit komt uit RC-84 (impliciete dienstselectie). De poort weigert een dienst die zichzelf niet mag aanmelden, en publish-on-web staat niet in die veertien. Dat was een bewuste keuze en op zichzelf verdedigbaar: het project legt vast op welke domeinen gepubliceerd mag worden, en dat is geen standaard om te verzinnen.

Maar de melding verwijst naar een laag die de aanroeper **niet kan bereiken**:

* `PublishOnWebProjectConfig` bestaat wel (`opi/services/catalog/publish_on_web/__init__.py:258`);
* `config_layers()` (`opi/services/catalog/base.py:887`) leidt de lagen af uit wat een dienst declareert: editables, API-velden, layoutknopen, DEFINE-payload. Publish-on-web declareert op de PROJECT-laag **nul editables** (dat is gemeten en het staat ook zo in de laagtabel van RC-78);
* dus meldt de catalogus geen projectlaag, en is er geen endpoint om hem te zetten.

**Een dienst zonder bereikbare projectlaag kan niet verplicht worden daar te worden aangezet.** Dat is de fout, en hij zit in de poort en niet in publish-on-web.

## Wat er moet gebeuren

Beslis welke van deze twee, met de reden:

1. **De poort wordt slimmer.** Weigeren mag alleen als de dienst een projectlaag heeft die de aanroeper kán zetten. Heeft hij die niet, dan is een bare selectie (de naam in de lijst, geen configblok) het juiste gedrag, ongeacht `allows_implicit_project_selection`. Dit is de systematische oplossing: hij dekt ook de volgende dienst met dezelfde vorm.
2. **Publish-on-web krijgt zijn projectlaag terug.** Als er echt een keuze op projectniveau hoort (welke domeinen), maak die dan bereikbaar via de API en laat de melding daarnaar verwijzen. Dat is meer werk en het is de vraag of die keuze bij het aanzetten hoort of pas bij het publiceren.

Optie 1 lijkt de juiste: de melding belooft nu iets dat niet kan, en dat is erger dan het weigeren zelf.

**Toets ook de andere kant op.** Zijn er meer diensten met een projectmodel maar zonder projecteditables? Die lopen vandaag tegen dezelfde muur zodra iemand ze via de API aan een component hangt.

## De toets

- de drie routes uit de melding werken weer op een vers project;
- `GET /v2/services/publish-on-web` en de melding spreken elkaar niet tegen: wat de melding vraagt, is ook te doen;
- een dienst die wél een bereikbare projectlaag heeft en niet impliciet mag, wordt nog steeds geweigerd, met een melding die klopt;
- het antwoord op vraag 12 staat in `plans/vragen-uit-zad-cli.md`;
- er is een test die faalt op de huidige code.

## Waar op te letten

**Dit is een regressie op een releasebuild.** De CLI had het werkend en heeft het verloren; hun draaiboek staat stil. Behandel het als zodanig.

**Draai RC-84 niet terug.** De impliciete selectie doet wat hij moet doen; wat ontbreekt is dat de weigering rekening houdt met een dienst die de gevraagde laag niet heeft.
