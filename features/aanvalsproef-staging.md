# Draaiboek: de aanvalsproef tegen staging

Staging (`odcn-staging`, `*.staging.rijksapp.dev`, zie `features/zad-staging.md`) is ingericht
als pentestomgeving, maar draait op dezelfde fysieke ODCN-cluster als productie. Een run
daartegen is daarom geen gate en geen taak van een sessie, maar een afgestemde handeling met een
venster en een aanspreekpunt. Dit draaiboek zegt wie wat doet, en hoe de uitkomst dezelfde
groene lijst wordt als een run tegen de sandbox.

De code weigert staging zelf: de grendel in `security_tests/doel.py` kent precies twee soorten
doel (de eigen machine en een host op `sandbox.rijksapp.dev`) en wijst `*.staging.rijksapp.dev`
af zonder er een verzoek naartoe te sturen. Een stagingrun vraagt dus een bewuste, eenmalige
uitbreiding van die lijst door de opdrachtgever, op de dag zelf. Niemand anders doet dat, en het
komt niet als standaardwaarde in de repo.

## Wie

| rol | wie | wat |
|---|---|---|
| opdrachtgever | de eigenaar van het platform | besluit, meldt aan, maakt het runbestand, start de run |
| ODC-Noord | de beheerder van de cluster | weet van het venster, kan de run laten stoppen |
| bouwer/reviewer | de sessie die aan de proef werkt | levert het harnas, draait de stagingrun niet |

## Vooraf

1. **Melding aan ODC-Noord**, met het venster (datum, begintijd, verwachte duur), de doelhosts
   (`*.staging.rijksapp.dev` en niets anders), en een telefoonnummer waarop de run te stoppen is.
2. **Een venster buiten kantooruren** van maximaal twee uur. Staging deelt de cluster met
   productie, dus elke minuut load is load die productie ook voelt.
3. **De begrenzing op papier**: welke hosts, welke lagen, en wat er NIET meegaat.
4. **Een verse back-up van de stagingdatabase**, zodat een gemutileerd project geen onderzoek
   kost maar een restore.

## Welke lagen meegaan

| laag | mee | waarom |
|---|---|---|
| de gerichte proeven (`security_tests/test_*.py`) | ja, zonder `test_stortvloed.py` | een stortvloed raakt de gedeelde cluster, niet alleen staging |
| schemathesis | ja, op een eigen proefproject | de verzoeken blijven binnen dat project |
| ZAP, pas ongeauthenticeerd en projectlid | ja, actief met een beperkte policy: geen DoS-regels, geen brute force, geen pad-brute force | de actieve scan is de kern van de proef; de afgeschakelde regels zijn de regels die load maken |
| ZAP, pas zad-admin | alleen na expliciete toestemming | een beheerderssessie op staging raakt beheerroutes die echte resources aanmaken |
| `test_sandboxdoel.py` | nee | die proef leidt de dienstadressen van het doel af, en op staging zijn dat de diensten van de gedeelde cluster |

## De run

1. De opdrachtgever zet een proeftoken op de stagingdeployment en haalt het er na de run weer
   af. `SECURITY_PROOF_TOKEN` weigert dat vandaag: OPI start niet op met een token op een ander
   cluster dan `sandboxed-local` (`opi/core/config.py`). Dat is bewust, en het opheffen voor een
   stagingrun is een expliciete, tijdelijke wijziging die bij het venster hoort.
2. De opdrachtgever maakt het runbestand met de hand, met een vervaldatum van die dag:

   ```json
   {
     "url": "https://zad.staging.rijksapp.dev",
     "token": "<het token dat in de pod staat>",
     "pid": 0,
     "sleutels": {"proef-staging": "<de API-sleutel van het proefproject>"},
     "sessies": {},
     "vervalt": "2026-10-23T22:00:00+00:00"
   }
   ```

   `vervalt` leest de grendel niet: het venster is een afspraak met de opdrachtgever, geen slot.
   Het slot dat wel werkt is het token, dat na het venster uit de pod gehaald wordt.

3. De drie sloten van de grendel blijven staan: het doel komt uit dit runbestand, de host moet op
   de toegestane lijst staan, en de instantie moet het token terugkaatsen op `/health`. Zonder dat
   token draait er geen enkele laag.
4. De run levert `report/aanvalsproef.md` op: dezelfde groene lijst, met per laag elke gemeten
   regel en de uitkomst (groen, afwijking met uitleg, bevinding, of niet gemeten met de reden).
   De lagen die niet meegingen staan erin als niet gemeten, met dit draaiboek als reden.
5. Na de run: het token uit de pod, het runbestand weg, de uitbreiding van de hostlijst terug.

## Na de run

De uitkomst gaat als groene lijst naar de opdrachtgever en naar ODC-Noord. Een bevinding die
blijft staan hoort met een reden in `operations-manager/security-proof/bekende-bevindingen.tsv`,
onder de proefnaam van zijn laag, zodat de volgende run er niet opnieuw over struikelt en een
reviewer ziet wat we accepteren.

## Zie ook

* `features/aanvalsproef.md`: de proef zelf, de drie doelen en de groene lijst
* `operations-manager/security-proof/README.md`: de grendel, de lagen, de baseline
* `features/zad-staging.md`: wat staging is en waar het op draait
