# De iconen die nog ontbreken

Status: plan, 13 augustus 2026. De gebruiker ziet nog steeds iconen ontbreken, terwijl de bewaker uit RC-90 groen staat. Er zit dus een gat tussen wat die test dekt en wat er op het scherm gebeurt. Zoek dat gat.

## Wat vaststaat, zodat niemand het opnieuw uitzoekt

**Er is GEEN mix tussen NLDD en RVO.** Dat vermoeden leeft, en het klopt niet: `DESIGN_SYSTEMS = ["lotc-layout", "nldd", "lotc-forms"]` in `opi/core/templates_lotc.py:49`, en `lotc_rvo` staat daar niet in. Het pakket is nog geïnstalleerd maar wordt niet geladen, dus zijn iconen komen er nooit uit. Er staan al drie plekken in de code die dat vermelden. Elke iconnaam moet dus een NLDD-naam opleveren.

**Er zit een aliaslaag tussen die makkelijk over het hoofd wordt gezien.** LOTC heeft een eigen tabel in `icons.json` die tijdens het renderen wordt toegepast, bovenop onze `ROOS_TO_NLDD_ICONS`. Daar ging het bijlagen-icoon in verloren: `folder-stack` bestáát in de bundel, maar LOTC herschrijft hem naar `folder-on-folder` en die bestaat niet. Wie tegen de rauwe iconenlijst toetst ziet dat niet. `tests/test_lotc_icon_mapping.py` meet daarom de naam NA die laag, en `opi/web/nldd_iconen.py` draagt de echte namen.

**Een onbekende naam faalt stil.** Er komt geen foutmelding, alleen een lege plek. Daarom is dit een test- en geen kijkprobleem.

## Wat er moet gebeuren

De bewaker is groen en de gebruiker ziet toch lege plekken. Eén van deze drie is waar, en het uitzoeken daarvan IS de taak:

1. **De bewaker kijkt niet overal.** Hij scant een aantal mappen; iconen die elders staan (de sjablonen van diensten onder `opi/services/catalog/*/`, iconen die uit gegevens komen in plaats van uit een literal, iconen in JavaScript) vallen er dan buiten. Meet welke plekken hij WEL en NIET ziet, en breid uit.
2. **De naam bestaat maar rendert toch niet.** Dan zit er nog een laag tussen, of het icoon zit in een component dat zijn eigen naam kiest. Dan is de meting van de naam niet genoeg en moet er in de browser gekeken worden of er werkelijk iets getekend wordt.
3. **De gebruiker ziet een oudere versie.** De iconenlijst kwam met een pakketwijziging, en die vraagt een herbouw van het image en geen sjabloonsynchronisatie. Sluit dit als eerste uit, want het is het goedkoopst.

**Kijk in de browser.** `scripts/kijk_sandbox.py <pad>` logt in en zet een pagina op beeld. Loop de pagina's langs die de gebruiker noemde (het dashboard, de projectdetails, de metingen, de wizard, de dialogen) en zoek de lege plekken op het BEELD, niet in de code. Van daaruit terug naar de naam.

## De toets

- er staat opgeschreven welke van de drie oorzaken het was, met de meting erbij;
- de bewaker dekt aantoonbaar elke plek waar een iconnaam vandaan kan komen, of er staat waarom een plek niet te dekken is;
- op de genoemde pagina's staat geen lege plek meer waar een icoon hoort;
- de lijst van uitzonderingen (`KNOWN_GAPS`) is nog steeds leeg, of elke naam erin draagt een reden.

## Waar op te letten

**Niet gokken naar een vervangende naam.** `opi/web/nldd_iconen.py` draagt de echte lijst, en het storybook heeft de bron (`components-content-icon`). Een naam kiezen die er goed uitziet levert dezelfde lege plek op, maar dan onvindbaar.

**Verwijder niets uit ROOS_TO_NLDD_ICONS zonder te toetsen.** Die afbeeldingen zijn de reden dat onze eigen namen werken.
