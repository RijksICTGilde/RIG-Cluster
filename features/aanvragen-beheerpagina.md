# Aanvragen: de beheerpagina voor goedkeuringen

`/admin/approvals` toont elke goedkeuring die een dienst declareert, per project en per
dienst. In het hoofdmenu heet hij **Aanvragen**; hij heette Domeinbeheer, en dat klopte
zolang domeinen het enige waren dat goedkeuring vroeg.

## Wat je ziet

Per project één paneel met de projectnaam en rechts de knop **Beheren** (die opent het
beoordelingsvenster). Binnen dat paneel staat een groep per **dienst**, met de naam en het
icoon uit de `ServiceDefinition` in de registry — "Publiceren op het web" met de wereldbol,
"E-mail versturen" met de envelop. Per groep één regel per aanvraag:

| Kolom | Wat er staat |
|---|---|
| Soort | het opschrift van de `ApprovalSpec` ("Domein", "Subdomein") |
| Aanvraag | `item.subject`: wat er wordt gevraagd |
| Status | Aangevraagd (warning), Goedgekeurd (success), Afgewezen (error) |
| Laatste wijziging | de datum van de laatste history-regel, en door wie |

De kolom **Soort** bestaat alleen waar hij iets toevoegt: als het opschrift van de spec
verschilt van de naam van de dienst. Bij publiceren op het web scheidt hij domein van
subdomein; bij een dienst met één soort aanvraag zou hij de groepskop herhalen, en dan valt
de kolom weg. Dat is een generieke regel (`group_by_service` in
`opi/web/router_approvals.py`) en geen lijstje diensten in het sjabloon.

## `subject`: wat er wordt gevraagd

Elk `ApprovalItem` draagt een `subject`, geschreven door de dienst die het weet:

| Aanvraag | `subject` |
|---|---|
| publish-on-web, domein | `example.nl` |
| publish-on-web, subdomein | `foo.example.nl` (samengesteld) |
| send-email | `Gebruik van de dienst` |
| opslaggrootte, per volume van een component | `5Gi voor volume data van component loep (nu 2Gi)` |
| opslaggrootte, per deployment | `20Gi voor volume data van component loep in deployment productie (nu 5Gi)` |

Zonder dat veld moest de pagina de zin zelf samenstellen uit de velden die zij toevallig
kende (`domain`, `name`), en dat is precies waarom een dienstaanvraag er als een lege
domeinkolom op stond. `collect_approval_items` valt terug op `domain`, anders `name`, zodat
een modalsessie die nog van voor deze wijziging loopt niet breekt.

De twee opslagsoorten zijn er sinds RC-499, en ze horen bij elkaar: het volume van het
component geldt voor elke deployment die erft, en een deployment dat ervan afwijkt vraagt een
eigen besluit. Wat er geldt zolang er geen oordeel is, staat in
`features/opslag-op-aanvraag.md`.

`subject` is **alleen voor de weergave**: het reist niet mee terug door het formulier van
het beoordelingsvenster. De routering van een oordeel gebeurt op `service` + `type`.

## Filteren

De knop **Status** filtert server-side op `?status=`, zodat het zonder JavaScript werkt en
een gefilterde lijst een deelbare URL is. Onder de knop staat "x van y aanvragen".

Beide getallen gaan over **items**, niet over groepen: er wordt eerst gefilterd op items
(`filter_by_status`) en daarna pas gegroepeerd, zodat er geen groepskop boven een lege
tabel komt te staan en de teller met de lijst blijft kloppen.

## Wat een oordeel opnieuw uitrolt

De route bepaalt de scope sinds RC-483 niet meer zelf. Het besluit gaat via
`record_approval_decision` (`opi/manager/project/update.py`), en de uitrol is daarna de taak
`rollout_project` (`queue_rollout`). De scope die de taak meekrijgt is
`result.rollout_deployments`, en die komt uit `deployments_touched(before, after)`
(`opi/services/project_utils.py`): dezelfde ene regel als voor elke andere ingang, zie
`features/project-wijzigen-een-pad.md` ("De uitrolscope").

Waar dat op uitkomt hangt af van de laag waarop de dienst zijn goedkeuringsstaat zet, want
dat bepaalt of het verschil binnen of buiten `deployments` valt. Er zijn twee gevallen:

- **Onder de projectconfig van de dienst: projectbreed.** Zo staat een domein- of
  subdomeinbesluit onder `services/[publish-on-web]/config/domains`, en het afzenderadres
  van het project onder de projectconfig van `send-email`. Dat ligt buiten `deployments` en
  `components`, dus geeft `deployments_touched` hier `None`, en dat betekent projectbreed.
- **Op de deployment-laag: alleen die ene deployment.** Een `ApprovalSpec` uit
  `deployment_value_approval` (`opi/services/catalog/approval.py`) zet zijn staat op
  `deployments[*].services[<dienst>].config/<veld>-approval`, dus binnen `deployments`.
  `send-email` declareert er twee, het afzenderadres en het dagbudget per deployment, en die
  staan op dezelfde pagina als de rest. Een oordeel over `dep-a` levert dan de scope
  `['dep-a']` op.

Wat niet meer bestaat is het versmallen van een domeinbesluit naar de deployments op dat ene
adres; `find_deployments_for_domain_item` heeft geen aanroeper in de applicatie. Projectbreed
kost daar alleen tijd, en heeft het voordeel dat de projectmanifesten zeker meegaan: die
worden alleen gegenereerd in een ronde die ten minste een deployment doet, en een scope van
nul deployments liet zo'n oordeel wel in het projectbestand landen en nooit in het cluster.

Is elk item overgeslagen, dan is er niets gewijzigd, en start er ook geen ronde
(`ProjectUpdateResult.needs_rollout`).

## Een nieuwe dienst met goedkeuring erop

Niets. Een dienst die een `ApprovalSpec` met een `list_items` declareert verschijnt vanzelf
met een eigen groep; zie `docs/development/services.md` ("Approvals"). Geef het item wel een
`subject`, anders staat er wat de terugval ervan maakt. Over de uitrol hoeft de dienst niets
te regelen: de scope volgt uit waar de spec zijn staat zet, projectbreed vanuit de
projectconfig en alleen die deployment vanaf de deployment-laag, zie "Wat een oordeel
opnieuw uitrolt" hierboven.

## De proefopstelling

`/lotc/bg/admin-approvals` toont dezelfde pagina met verzonnen gegevens, uit
`opi/web/lotc_fixtures.py`. Dat is de **tweede schrijver** van deze context, en die twee
horen niet uiteen te lopen: de fixture bouwt haar groepen met dezelfde
`group_by_service()` als de route, en haar items dragen dezelfde sleutels als
`collect_approval_items()` ze aflevert (`service`, `label`, `subject`). Een sleutel die de
route wel schrijft en de fixture niet levert daar geen fout op maar een LEGE lus, dus
`tests/test_aanvragen_per_dienst.py` (`TestDeProefopstelling`) en de browsertoets
`test_de_proefopstelling_van_aanvragen_toont_ook_echt_aanvragen` eisen allebei dat er
rijen op staan.

## Waar het staat

| Bestand | Wat |
|---|---|
| `opi/templates_lotc/bg/admin-approvals.html.j2` | de pagina en het beoordelingsvenster |
| `opi/web/router_approvals.py` | de route, `filter_by_status`, `group_by_service` |
| `opi/web/lotc_fixtures.py` | de gegevens van de proefopstelling op `/lotc/bg/admin-approvals` |
| `opi/services/approvals.py` | de catalogusloop: items verzamelen, oordelen toepassen |
| `opi/services/catalog/approval.py` | `ApprovalSpec`, `ApprovalItem`, `service_use_approval()` |
| `tests/test_aanvragen_per_dienst.py` | de gerichte poort op de weergave en de telling |
| `tests/e2e/test_lotc_aanvragenbeheer.py` | wat alleen in een browser te zien is |
| `tests/e2e/test_aanvragen_schermafdruk.py` | de schermafdruk waarop dit beoordeeld wordt |
