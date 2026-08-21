# De goedkeuringsmodal doet met de hand wat htmx al kan

Op `/admin/approvals` opent de knop "Beheren" een dialoog die met twintig regels JavaScript nabouwt wat deze codebase overal elders met htmx doet: een `fetch` naar een fragment-URL, de HTML in een `innerHTML` schuiven, een laadtoestand tonen, en bij een fout zelf een foutvak vullen.

Dat is geen stijlkwestie. Drie fouten van 15 en 16 augustus komen alle drie uit die handbouw voort, en ze zijn alle drie apart gerepareerd zonder dat de oorzaak weg is.

## Wat er misging, en waarom het steeds hetzelfde is

**De projectnaam kwam ongerenderd in de URL.** De dialoog vroeg `/admin/approvals/%7B%7B%20project.project_name%20%7D%7D/modal-wizard/admin-approval` op: `{{ project.project_name }}` URL-gecodeerd. Een sjabloonexpressie belandde als platte tekst in JavaScript en werd daar aan een URL geplakt. Met `hx-get` op de knop staat die naam in het pad dat Jinja zelf rendert, en kan dit niet.

**Er stond een lege rode foutbalk.** Die bak wordt vooraf leeg neergezet zodat de JavaScript hem kan vullen, en werd verborgen met een klasse die op deze bouwlijn niets deed. Met htmx bestaat dat probleem niet: het fragment dat terugkomt bevat de melding, of er komt geen melding.

**De klasse `is-hidden` werkte nergens.** De regel stond alleen in het stylesheet van de oude schil, dat de LOTC-basis niet laadt. Dat is inmiddels gerepareerd voor de hele bouwlijn, maar het is wel het derde geval van hetzelfde patroon: iets wat op de oude bouwlijn impliciet werkte, verhuisde bij het overzetten niet mee, en werd overbrugd met een stukje JavaScript in plaats van dat het gat gedicht werd.

## Wat er gebouwd moet worden

De knop "Beheren" wordt een gewone htmx-aanroep: `hx-get` naar de fragment-URL met de projectnaam erin, `hx-target` op de inhoud van de dialoog, en de laadtoestand via de bestaande htmx-middelen. `openApprovalModal` verdwijnt, en daarmee ook de lege foutbak: een fout komt terug als fragment, of hij komt niet.

## Waar het gevaar zit

**De modalschil is gedeeld.** `opi/web/router_detail_edit.py` rendert er de bewerkdialogen van een project mee, met dezelfde `_modal-wizard-step`-constructie. Een wijziging die alleen naar de goedkeuringspagina kijkt, kan die schermen breken zonder dat iemand het merkt.

**Twee scripts dragen gedrag dat je niet mag kwijtraken.** `edit_modal.js` doet het sluiten, Escape en de blokkade tijdens een lopende taak; `json-enc.js` laat htmx het stapformulier als JSON versturen, en de route weigert iets anders. Dat staat met zoveel woorden in het commentaar boven die scripts in `bg/admin-approvals.html.j2`. Wie ze weghaalt omdat "htmx het nu doet", sloopt het venster.

**De weg terug bij een fout moet echt getoetst worden.** Nu vult JavaScript een vak met "Fout bij het laden van het formulier". Straks moet een falende aanroep een leesbare melding opleveren in de dialoog, en niet een lege dialoog of een stille sluiting. Dat is het enige gedrag waar de gebruiker iets aan heeft als het misgaat, dus het is ook het enige dat je niet op zijn beloop mag laten.

## Verifieerbaar

Er staat sinds 16 augustus een e2e-test voor deze pagina (`tests/e2e/test_lotc_aanvragenbeheer.py`). Bouw daarop voort en toon aan:

1. de dialoog opent en toont het formulier;
2. de opgevraagde URL bevat de ECHTE projectnaam (de regressietest van de eerste fout);
3. er staat geen leeg foutvak in de dialoog;
4. een falende aanroep levert een zichtbare, leesbare melding op;
5. de bewerkdialogen van een project (`router_detail_edit`) werken onveranderd: openen, opslaan, sluiten met Escape.

Punt 5 is geen bijvangst maar de kern van de opdracht: het bewijs dat de gedeelde schil niet beschadigd is.

## Wat er buiten valt

- De verouderde sjabloonboom onder `opi/templates_lotc/admin/approvals/` (`_modal.html.j2`, `approvals.html.j2`). Die draagt dezelfde constructies maar hangt aan `/lotc/pagina/<naam>` en is niet wat een gebruiker ziet. Opruimen mag, maar dan als aparte stap met een eigen oordeel of hij nog ergens voor dient.
- Andere schermen die `edit_modal.js` gebruiken. Blijkt tijdens het werk dat dezelfde handbouw daar ook staat, meld dat dan als bevinding met de vindplaatsen erbij; verbouw ze niet mee.
- De uitlijning van icoon en titel in de dialoogkop. Dat loopt als eigen taak.
