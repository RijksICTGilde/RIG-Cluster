# Een mislukte run maakt geen nieuwe database meer

**Status**: plan, nog niets gebouwd.
**Datum**: 2026-09-17
**Aanleiding**: issue #56, nagemeten op main op 17-09-2026. De meest voorkomende aanleiding is dicht, het ontwerpgat eronder niet.

## Wat er gebeurt

Een deployment met `clone-from` kloont zijn database als eerste stap van de run, en pas aan het eind van diezelfde run wordt vastgelegd dat dat gelukt is. Faalt er iets tussen die twee momenten, dan ziet de volgende refresh een database die bestaat bij een status die zegt dat er nog niets gekloond is, en maakt hij een versie ernaast: `_v1`, `_v2`, tot de cap van vijf. Elke poging kost een generatie en laat een volledige kopie achter.

Op main is dat onveranderd. `set_clone_status(completed=True)` staat nog steeds na het provisioneren én na `_process_application_manifests` (`project_manager.py:5386-5404`), met de PVC-uitleg erboven als reden. De failover is nog steeds `if target_db_exists and (force_clone or generation is None)` (`database_manager.py:772`), met `max_attempts = 5` erachter. Een `in-progress`-vlag bestaat nergens: `clone-from.status` kent in `opi/schemas/project_v2.json` alleen `completed` en `timestamp`, en dat blok staat op `additionalProperties: false`.

De generatie helpt niet mee, want die gaat dezelfde weg. `record_clone` schrijft hem in het geheugen (`database_manager.py:869-875`) en pas de save aan het eind zet hem op schijf. Faalt de run daarvoor, dan leest de volgende `completed=false` én `generation=None`, precies de combinatie die de failover aanzet.

Wat sinds mei wel veranderde, is de kans dat het gebeurt. De statusmutatie liep eerst via een kale schrijf in de warme werkkopie, die een gelijktijdige reconcile met `reset --hard` weggooide waarna de commit niets te committen vond en toch succes meldde. Dat loopt nu door `save_and_commit_project`, één vergrendelde operatie met een three-way merge. Het raam is kleiner, niet weg: elke fout tussen de geslaagde kloon en het eind van de run doet het nog steeds.

## Waarom optie B uit het kaartje niet volstaat zoals hij er staat

Het kaartje beveelt een `in-progress`-vlag aan: bij een halve kloon ziet de volgende run dat er al een poging liep, vertrouwt de bestaande database en draait de flow opnieuw. Dat tweede deel klopt niet met wat de kloon doet.

`_execute_pgdump_clone` weigert te klonen naar een schema dat al bestaat, tenzij `force_clone` aanstaat (`opi/connectors/postgres.py:1840-1844`, en dezelfde toets bij het voorbereiden van de extensies op `:1352-1364`). In het scenario van dit kaartje ís de kloon geslaagd, dus dat schema staat er. Alleen de failover onderdrukken levert dus geen geslaagde herhaling op maar een run die elke keer op die uitzondering stukloopt, met een handmatige `DROP SCHEMA` als enige uitweg. Dat ruilt een groeiende stapel databases in voor een deployment die stilstaat.

Dat verklaart ook waarom de failover ooit zo gebouwd is: hij was de ontsnapping uit precies die weigering. Een oplossing die de failover wegneemt moet die ontsnapping dus vervangen, niet alleen afsluiten.

## Wat we bouwen

**1. De vlag.** `clone-from.status` krijgt er een boolean bij die zegt dat er een kloonpoging loopt. Naam is een voorstel, `in-progress` sluit aan bij het kaartje; kies wat bij `completed` en `timestamp` past. Hij komt in `opi/schemas/project_v2.json` naast die twee, want dat blok staat op `additionalProperties: false` en zonder die toevoeging wordt het nieuwe veld bij validatie geweigerd. Een datamigratie is niet nodig: afwezig betekent niet bezig, dus bestaande projectbestanden gedragen zich als vandaag.

**2. Wanneer hij aan gaat.** Vlak voor het provisioneren in `process_project`, voor elke deployment van deze cluster met een `clone-from` in mode `once` waarvan `completed` nog false is. Dat is precies de situatie uit dit kaartje. Mode `always` blijft buiten schot, die zet `force_clone` aan en hoort elke run een verse generatie te krijgen. De vlag moet op dat moment ook echt op schijf staan, dus via `save_and_commit_project`, anders lost hij op in dezelfde fout die we proberen te overleven.

**3. De failover luistert ernaar.** De voorwaarde wordt: bestaat de doeldatabase, dan alleen een nieuwe generatie bij `force_clone`, of bij `generation is None` terwijl er geen poging liep. Loopt er een poging, dan is de bestaande database van ons en blijft hij van ons. De cap van vijf blijft staan als achtervang voor het geval het schrijven van de vlag zelf faalt.

**4. Een tweede poging maakt de kloon af in plaats van hem over te doen.** Loopt er een poging en bestaat het doelschema al, dan is de kloon klaar: sla hem over, en meld hem verder precies zoals een geslaagde kloon zich meldt (`record_clone` en `report_clone_performed`), zodat de rest van de run en de save aan het eind niets bijzonders hoeven te weten. Dit is het deel dat de weigering uit de vorige paragraaf opvangt.

**5. Een half schema blijft niet liggen.** Punt 4 mag alleen als "schema aanwezig" ook echt "kloon afgerond" betekent. Vandaag ruimt de foutafhandeling in `database_manager` alleen de database op die ze zelf aanmaakte; klapt de kloon in een database die er al stond, dan blijft een half schema achter. Breid die opruiming uit naar het schema dat deze run aanmaakte, zodat een overlevend schema altijd een afgeronde kloon is.

Let bij het bouwen op de basisadministratie van de save. De save aan het eind pint `self.__contents_as_read` bewust terug op de basis waarop `project_data` gebouwd is, met de uitleg waarom dat moet (`project_manager.py:5415-5441`). De nieuwe save halverwege verzet die basis. Controleer dat die pin blijft werken en leg dat vast in een test, want dit is de manier waarop deze wijziging stilletjes iets anders kapot kan maken.

## Wat we niet doen

Optie C uit het kaartje, de failover laten beslissen op de inhoud van de doeldatabase. Dat repareert ook projecten met een oud projectbestand, maar koopt dat met een probe per kloonbesluit en met een verkeerd antwoord zodra een bron legitiem leeg is. Punt 4 hierboven doet een veel kleinere variant: het kijkt alleen of het schema bestaat, en alleen wanneer de vlag zegt dat er een poging liep.

Optie A, een aparte statusvlag per dienst. Die vraagt een schemawijziging per dienst en een migratie van bestaande projectbestanden, terwijl het probleem uit dit kaartje met één vlag te dekken is. De PVC-kloon blijft gewoon `completed` lezen en verandert niet.

De MinIO- en PVC-kloon op een tweede poging. Die doen vandaag wat ze doen, en dit kaartje gaat over de databases die blijven staan.

De bestaande zombies opruimen. Dat is een losse actie met een controle per deployment (welke database het actieve `<deployment>-database` secret aanwijst) en hoort niet in dezelfde PR als een gedragswijziging.

## Klaar als

- Een run die na een geslaagde databasekloon afbreekt, laat de vlag aan in het projectbestand, en de volgende run maakt geen `_v1` maar maakt de deployment af. Dit is de kern; toets hem op de manier die het dichtst bij de echte volgorde zit.
- Diezelfde tweede run kloont niet opnieuw over een bestaand schema heen en loopt dus niet op de weigering uit `_execute_pgdump_clone` stuk.
- `force-clone` en mode `always` krijgen nog steeds een nieuwe generatie. De vlag onderdrukt alleen het geval `generation is None`.
- Een kloon die halverwege faalt in een database die al bestond, laat geen schema achter.
- Een projectbestand zonder de nieuwe vlag valideert en gedraagt zich als vandaag, en een bestand met de vlag valideert ook. Toets na `migrate_to_latest()`, niet ervoor.
- De save aan het eind van de run vergelijkt nog steeds tegen de basis waarop de run begon, ondanks de extra save halverwege.
- `uv run ruff check . --fix`, `uv run ruff format .` en `uv run pyright` schoon, unitsuite groen.
- `features/` beschrijft het nieuwe gedrag: wanneer de vlag aan gaat, wanneer hij weer uit gaat, en wat een tweede poging doet.
