# Backups: een eigen tabblad

De backups van een project staan op een **eigen tabblad** van de projectpagina, met **een
deployment per pagina** en zijn naam in het pad:

```
/projects/<project>/backups/<deployment>
```

Het blok stond op het tabblad Deployments, als een van de blokken die de diensten per
deployment leveren. Daar was het een bijvangst van de deploymentdetails, terwijl het een
**bestemming** is: je gaat naar backups toe om een schema te zetten of iets terug te
zetten. Het is bovendien groot - een schema, twee knoppen en de hele snapshotlijst - en
duwde de rest van de deploymentpagina naar beneden.

Het is **verhuisd, niet gekopieerd**. Op het tabblad Deployments staat het niet meer; twee
weergaven van dezelfde gegevens lopen uit de pas.

## Wat er op het tabblad staat

| Onderdeel | Waar het vandaan komt |
|---|---|
| De deploymentkiezer | `bg/_deployment-selector.html.j2`, dezelfde als op Deployments en Metrics |
| Het backupblok | `shared/section-backups.html.j2`, van de diensten die iets kunnen backuppen |
| De snapshotlijst | `GET /projects/details/<project>/backups`, lui opgehaald en buiten de band geplaatst |

De dialogen "Schema instellen/wijzigen" en "Backup aanmaken" zitten in het blok en zijn
dus meeverhuisd; "Herstellen" verschijnt pas als de snapshotlijst binnen is en er iets te
herstellen valt.

Ziet het tabblad geen blok, dan zegt het waarom in plaats van leeg te blijven:

| Situatie | Wat er staat |
|---|---|
| Het project heeft geen deployments | "Nog geen deployments" - maak er eerst een |
| Geen enkele dienst kan iets backuppen, of de deployment draait op een ander cluster | "Geen backups voor deze deployment" |
| De backupdienst is niet bereikbaar | "Backups niet beschikbaar" (uit het blok zelf) |

## Een deployment per pagina

Hetzelfde als Deployments en Metrics, en om dezelfde reden - zie
`features/deploymentpagina.md`, hoofdstuk "Een deployment per pagina":

- `/projects/<p>/backups` kiest de eerste op naam en **verwijst door**, zodat het adres
  daarna zegt wat je ziet;
- een verwijderde naam in een gedeelde link valt terug op een bestaande;
- de keuze **reist mee** naar Deployments en Metrics, omdat de tabbalk hem in zijn adressen
  meeneemt (`project_tab_url(..., deployment=...)`).

Beide adresvormen (met en zonder deployment) staan **letterlijk** geregistreerd in
`opi/web/router.py`, niet als `/projects/{project}/{tab}`: dat laatste zou ook
`/projects/details/<naam>` opvangen.

Er is **geen** oude vorm met het tabblad voorop (`/projects/backups/<naam>`). De andere
tabbladen hebben daar een doorverwijzing voor omdat hun oude adres gedeeld kan zijn; dit
tabblad heeft nooit onder die vorm bestaan.

## Het lui laden blijft

Het blok haalt de snapshots op met `hx-trigger="intersect once"`. De reden om het te
verbergen is vervallen - er staat er nog maar een op de pagina - maar de reden om het lui
te laden niet: een snapshotlijst opent een Kopia-repository over S3, gemeten op ~2,5
seconde in productie, en dat hoort niet op het renderpad van de pagina te staan.

Het verzoek is er **een per pagina**. Het haalt de snapshots van het hele project op en
plaatst ze met `hx-swap-oob`; de placeholders van de deployments die niet op deze pagina
staan zijn er simpelweg niet. De regel "alleen het blok van de eerste deployment laadt"
is daarmee vervallen: met een deployment per pagina zou elke andere pagina voor eeuwig
"Backups worden opgehaald..." tonen.

## Waarom Backups met naam genoemd wordt

De blokken die een dienst per deployment levert komen uit een **generiek mechanisme**
(`UIEvent.DEPLOYMENT_SECTIONS`): het sjabloon noemt geen enkele dienstnaam. Een eigen
tabblad voor Backups is daarop een uitzondering, en dat is een keuze geweest.

Gemeten: **twee** diensten leveren een deploymentblok - de backupbare diensten en de
metrics-scraper. Dat tweede blok toont dezelfde grafieken die het tabblad Metrics al voor
elk project toont, en is dus geen kandidaat voor een eigen tabblad. Met één kandidaat is
een haak waarmee een dienst kan zeggen "mijn blok verdient een tabblad" - en een tabbalk
die zijn tabbladen deels uit de registry haalt - machinerie voor een geval dat niet
bestaat. Backups staat daarom met naam in de tabbladenlijst, in het sjabloon en in de
route, precies zoals Metrics dat doet. Verdient een tweede blok ooit een tabblad, dan is
dat het moment om te generaliseren.

Het blok blijft wel van de diensten: `collect_backups_sections()` in
`opi/services/catalog/shared/backups.py` kiest het op dezelfde manier als de registry dat
doet (welke diensten gebruikt dit project), en de route van het fragment staat nog steeds
bij de dienst.

## Toetsen

| Bestand | Wat het bewaakt |
|---|---|
| `tests/test_lotc_backups_tabblad.py` | het tabblad bestaat met beide adresvormen, het blok is van Deployments verdwenen, en de route vult het alleen hier |
| `tests/test_service_deployment_sections.py` | wie het blok bezit: elke backupbare dienst, een keer, en niet meer via de deploymentsectiehaak |
| `tests/test_detail_page_backup_laziness.py` | een luie lader, een verzoek, en een namespace een keer |
| `tests/e2e/test_lotc_backups_tab.py` | in de browser: het juiste blok, de meeverhuisde dialogen, en het ene verzoek per pagina |
