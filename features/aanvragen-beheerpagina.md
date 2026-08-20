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
de kolom weg. Dat is een generieke regel (`groepeer_per_dienst` in
`opi/web/router_approvals.py`) en geen lijstje diensten in het sjabloon.

## `subject`: wat er wordt gevraagd

Elk `ApprovalItem` draagt een `subject`, geschreven door de dienst die het weet:

| Aanvraag | `subject` |
|---|---|
| publish-on-web, domein | `example.nl` |
| publish-on-web, subdomein | `foo.example.nl` (samengesteld) |
| send-email | `Gebruik van de dienst` |

Zonder dat veld moest de pagina de zin zelf samenstellen uit de velden die zij toevallig
kende (`domain`, `name`), en dat is precies waarom een dienstaanvraag er als een lege
domeinkolom op stond. `collect_approval_items` valt terug op `domain`, anders `name`, zodat
een modalsessie die nog van voor deze wijziging loopt niet breekt.

`subject` is **alleen voor de weergave**: het reist niet mee terug door het formulier van
het beoordelingsvenster. De routering van een oordeel gebeurt op `service` + `type`.

## Filteren

De knop **Status** filtert server-side op `?status=`, zodat het zonder JavaScript werkt en
een gefilterde lijst een deelbare URL is. Onder de knop staat "x van y aanvragen".

Beide getallen gaan over **items**, niet over groepen: er wordt eerst gefilterd op items
(`filter_op_status`) en daarna pas gegroepeerd, zodat er geen groepskop boven een lege
tabel komt te staan en de teller met de lijst blijft kloppen.

## Een nieuwe dienst met goedkeuring erop

Niets. Een dienst die een `ApprovalSpec` met een `list_items` declareert verschijnt vanzelf
met een eigen groep; zie `instructions/services.md` ("Approvals"). Geef het item wel een
`subject`, anders staat er wat de terugval ervan maakt.

## Waar het staat

| Bestand | Wat |
|---|---|
| `opi/templates_lotc/bg/admin-approvals.html.j2` | de pagina en het beoordelingsvenster |
| `opi/web/router_approvals.py` | de route, `filter_op_status`, `groepeer_per_dienst` |
| `opi/services/approvals.py` | de catalogusloop: items verzamelen, oordelen toepassen |
| `opi/services/catalog/approval.py` | `ApprovalSpec`, `ApprovalItem`, `service_use_approval()` |
| `tests/test_aanvragen_per_dienst.py` | de gerichte poort op de weergave en de telling |
| `tests/e2e/test_lotc_aanvragenbeheer.py` | wat alleen in een browser te zien is |
| `tests/e2e/test_aanvragen_schermafdruk.py` | de schermafdruk waarop dit beoordeeld wordt |
