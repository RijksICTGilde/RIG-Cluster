# Een bestaande bijlage vervangen via de UI

In het blok Bijlagen kun je een bestand toevoegen en verwijderen, maar niet **vervangen**. Een certificaat dat verloopt moet je nu weggooien en opnieuw uploaden, en dan zijn alle koppelingen naar die bijlage weg: elk component dat hem gebruikte verwijst nergens meer heen.

Wat er moet komen is een bewerking waarbij de **id vast staat** en alleen de inhoud wordt overschreven.

## Wat er al is

De API kan dit. `ProjectManager.upsert_attachment` kent `on_existing`, geeft `replaced` terug, en `ActionVerb.UPDATE` betekent precies dit: het ding moet bestaan, en de inhoud gaat eroverheen (`opi/services/catalog/actions.py:47-48`). Het ontbreekt dus alleen aan de kant van het scherm.

Wat er staat:

- Het blok zelf: `opi/services/catalog/attachments/section-detail.html.j2`. Per bijlage de bestandsnaam, de id, en een knop Verwijderen. Meer niet.
- Het uploadformulier: `opi/templates_lotc/wizard/partials/attachments_upload.html.j2`, met de lijst in `attachments_list.html.j2`.
- De actie: `_store` in `opi/services/catalog/attachments/api.py:219`, die het werkwoord uit de context leest.

## De drie plekken waar het misgaat als je er alleen een knop bij zet

Dit is de reden dat dit een eigen taak is en geen knop.

**De id-controle weigert juist een bestaande id.** `attachments_id_field.html.j2` hangt aan `/forms/wizard/create-project/attachments/validate-id`, en die controleert op uniciteit. Voor een vervanging is een bestaande id precies goed. Die controle moet dus weten of hij een nieuwe of een vervangende bijlage beoordeelt, en niet stilzwijgend allebei toestaan: een typefout in een nieuwe id die per ongeluk een bestaande raakt, overschrijft dan zonder waarschuwing iemand anders zijn certificaat.

**Het uploaden loopt via staging.** De knop stuurt naar `/attachments/stage`, dat in de wizardsessie belandt en pas bij opslaan wordt gecombineerd. Een vervanging moet daar als vervanging doorheen komen, niet als tweede bijlage met dezelfde id. Kijk goed naar wat `combine` doet als de id al in de catalogus staat.

**De redactie van de sessie.** Bijlagen zijn versleuteld en de wizardsessie streept versleutelde waarden weg die het formulier niet kan bereiken (`opi/forms/wizard/secrets.py`). Een bijlage die je aan het vervangen bent, moet die rit overleven. Dit is dezelfde klasse fout als het realm-wachtwoord (RC-102) en de aliassen: controleer het, ga er niet van uit.

## Taken

### 1. De knop en waar hij heen gaat

Per bijlage in `section-detail.html.j2` een knop **Vervangen** naast Verwijderen. Hij opent hetzelfde bewerkscherm als Toevoegen, maar met de id ingevuld en **niet te wijzigen**, en met een tekst die zegt wat er gebeurt: de inhoud wordt overschreven, de koppelingen blijven.

Let op de bekende val met ROOS-knoppen: een componenttag laat een los `hx-`-attribuut niet door, dus de bedrading gaat via `:attrs`, zoals de bestaande knoppen in `attachments_upload.html.j2` het al doen.

Verifieer op het scherm met `scripts/kijk_sandbox.py`, niet in de markup.

### 2. De id-controle moet het onderscheid kennen

`validate-id` moet weten of het om een nieuwe of een vervangende bijlage gaat. Bij vervangen is een bestaande id vereist in plaats van verboden, en een id die **niet** bestaat is dan een fout: dat is een vervanging van iets wat er niet is.

Verifieer met beide kanten op: een nieuwe id bij vervangen wordt geweigerd, een bestaande id bij toevoegen ook.

### 3. Door de staging heen

Het gestageerde bestand moet bij het opslaan als vervanging landen, met `ActionVerb.UPDATE`-semantiek: id bestaat, inhoud eroverheen, catalogusregel behouden.

Verifieer op het projectbestand: na een vervanging is er precies **één** catalogusregel met die id, de inhoud is nieuw, en elke `use` in elk component wijst er nog steeds naar. Dat laatste is waar het echt om gaat.

### 4. De bestandsnaam

Een vervangend bestand kan anders heten dan het origineel. Beslis wat er gebeurt en schrijf op waarom: de nieuwe naam overnemen (dan klopt wat er getoond wordt met wat erin zit) of de oude houden (dan blijft herkenbaar waar de bijlage voor dient). **Voorstel: de nieuwe naam overnemen**, want de id is waar alles aan hangt en de naam is niets meer dan een etiket.

### 5. Testen

- Vervangen laat de koppelingen staan. Dit is de test die de reden van deze taak bewaakt: verwijderen-en-opnieuw-uploaden verliest ze, vervangen niet.
- Een vervanging levert geen tweede catalogusregel op.
- De id is in het formulier niet te wijzigen, en een verzoek dat hem toch meestuurt met een andere waarde wordt geweigerd. Een knop die iets vastzet is geen beveiliging.
- Een vervanging van een niet-bestaande id geeft een nette fout.
- De bijlage overleeft de sessieredactie: vervang er een in een project dat ook een realm-wachtwoord heeft, en controleer dat allebei nog staan.

### 6. Documentatie

`features/` heeft een document over bijlagen; daar hoort de vervangbewerking bij, met de zin dat de koppelingen blijven. Ook `help.md` van de dienst, want dat is wat de API-lezer krijgt.

## Wat er buiten valt

- De id wijzigen. Dat is geen bewerking maar een nieuwe bijlage plus het omzetten van elke koppeling, en dat is een eigen taak.
- Meerdere bestanden tegelijk vervangen.
- Versiehistorie van een bijlage. Vervangen is vervangen; de vorige inhoud is weg.

## Volgorde

1. De id-controle (taak 2) → verifieer: beide kanten op.
2. De staging (taak 3) → verifieer: op het projectbestand, met de koppelingen.
3. De knop (taak 1) → verifieer: op het scherm.
4. Testen en documentatie (taak 5 en 6).
