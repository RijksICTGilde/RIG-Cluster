# Een test ruimt zijn eigen rommel op

Status: plan, 7 augustus 2026. Niet gebouwd. Aanleiding: een e2e-test die slaagt in zijn eentje en faalt in gezelschap. Dat is punt 12 van de TODO, maar de oorzaak zit dieper dan die twee bestanden.

## Het symptoom, gemeten

```
tests/e2e/test_backup.py        alleen   25 geslaagd
tests/e2e/test_edit_wizard.py   alleen    1 gefaald, 14 geslaagd
dat ene testje op zichzelf                1 geslaagd
```

`TestEditServices::test_select_service_advance_through_config_to_review` valt om zodra er andere tests uit hetzelfde bestand vóór hem gedraaid hebben, en slaagt los. Twee keer gereproduceerd, dus het is geen toeval.

## De oorzaak, gemeten

**Alles is sessie-breed.** Elke fixture in `tests/e2e/conftest.py` heeft `scope="session"`: de applicatie, de browsercontext, de aangemelde gebruiker. Wat een test verandert, blijft staan voor de volgende.

**Vijf bestanden delen hetzelfde projectbestand.** `test-project-detail` komt op zes plekken voor, verdeeld over vijf testbestanden. Er zijn drie fixture-projecten in `tests/e2e/fixtures/projects/`, en niets zet ze tussen tests terug.

**De opruimhulp bestaat al en wordt door niemand gebruikt.** `tests/e2e/helpers/cleanup.py` heeft een `ProjectCleanup` met `register()` en `cleanup_via_ui()`. Nul testbestanden importeren hem. Dat is geen ontbrekend gereedschap maar ongebruikt gereedschap, net als de dode `deployment_order.py` die eerder deze week boven kwam.

**Parallel draaien kan vandaag niet.** `pytest-xdist` zit niet in de afhankelijkheden. De wens om parallel te draaien is dus vooruitkijkend, en dat is precies het goede moment om dit op te lossen: isolatie die je nodig hebt voor parallellisme is dezelfde isolatie die deze fout wegneemt.

## Wat het moet worden

Twee eisen, en ze versterken elkaar:

1. **Een test ruimt zijn eigen onderdelen op.** Wat hij aanmaakt of verandert, zet hij terug. Niet als beleefdheid maar zodat de volgende test van een bekende toestand uitgaat.
2. **Tests zitten elkaar niet in de weg als ze tegelijk draaien.** Dus geen gedeelde naam, geen gedeeld bestand, geen gedeelde sessie waar dat vermijdbaar is.

## Voorstel

1. **Elk testbestand krijgt zijn eigen project.** Een naam die van de test zelf is afgeleid, niet een gedeelde `test-project-detail`. Daarmee vervalt de hele klasse "de vorige test heeft mijn project veranderd", en het is meteen de isolatie die parallel draaien vraagt.

2. **Terugzetten is structureel, niet per test geregeld.** Een fixture die het projectbestand vóór de test neerzet en erna opruimt. Wie het vergeet, kan het niet vergeten, want het zit in de weg ernaartoe.

3. **Beslis over `ProjectCleanup`: gebruiken of weghalen.** Hij bestaat, hij doet ongeveer wat punt 2 vraagt, en niemand roept hem aan. Bouw erop voort of haal hem weg; laat hem niet staan als iets dat wel lijkt te werken maar nergens draait.

4. **Zet de e2e-laag ergens in de vaste loop.** In de gewone suite staat `249 deselected` en daar zitten deze tests in; ze draaien alleen als iemand er expliciet om vraagt. Dat is hoe dit lang ongemerkt kon blijven. Dat hoeft niet bij elke commit, maar wel ergens met regelmaat, anders is de laag er wel en beschermt hij niets.

5. **Pas daarna parallel draaien.** `pytest-xdist` erbij als de isolatie klopt, niet ervoor. Anders ruil je een reproduceerbare fout in voor een onvoorspelbare.

## Volgorde

1. Uitzoeken welke test welk spoor achterlaat. Begin bij het gemeten geval: draai `test_edit_wizard.py` en kijk wat er vóór de vallende test aan `test-project-detail` verandert.
2. Eigen project per bestand, met de fixture die neerzet en opruimt. Verifieerbaar: elk e2e-bestand slaagt zowel los als in willekeurige volgorde.
3. De beslissing over `ProjectCleanup`.
4. De e2e-laag in de vaste loop.
5. Parallel draaien, met de suite groen in beide vormen.

## Waar op te letten

**Repareer niet die ene test.** De verleiding is om de vallende test een `setUp` te geven en verder te gaan. Dan is de fout weg en de oorzaak niet, en de volgende die twee tests aan elkaar knoopt loopt er opnieuw tegenaan.

**Volgorde-onafhankelijkheid is te testen.** Draai de suite met omgekeerde of willekeurige volgorde en eis dat hij groen blijft. Dat is de enige manier om te weten dat het echt weg is in plaats van verplaatst; `pytest-randomly` staat al in de opzet, want overal in deze repo wordt hem juist uitgezet met `-p no:randomly`.

**Sessie-brede fixtures zijn niet allemaal fout.** De applicatie opstarten per test zou de suite onbruikbaar traag maken. Het gaat om de *data*, niet om de server: die mag gedeeld blijven, het projectbestand niet.

**Deze laag verdient het.** Vandaag ving de e2e-laag drie fouten die de gewone suite niet zag: een vergrendelde dienst die bij het opslaan verdween, een configregel die bij elk vinkje verdween, en een toevoegknop die niets deed. Een vangnet dat werkt maar dat je niet vertrouwt, is bijna net zo slecht als geen vangnet.
