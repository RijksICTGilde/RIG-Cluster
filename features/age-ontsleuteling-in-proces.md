# AGE-ontsleuteling in het proces

## Wat het is

`decrypt_age_content` en `decrypt_age_content_sync` (`opi/utils/age.py`) ontsleutelen
sinds deze wijziging in het proces zelf, via `pyrage`, in plaats van per aanroep het
`age`-binary als subprocess te starten.

De aanleiding was issue #145, dat de projectdetailpagina traag noemde. Dat klopt niet
meer: de tweede oorzaak uit dat ticket (`ensure_projects_fresh()` op het request-pad) is
met de ProjectStore verdwenen, en de decryptie die overblijft kost milliseconden. Wat
wel bleef is een **fork per veld**: elke ontsleuteling was een proces erbij, met een
eigen adresruimte en een tempfile met de private sleutel erin. OPI is eerder OOMKilled
door precies die klasse, en een detailpagina deed die forks bij elke render, voor elke
gelijktijdige bezoeker.

## Wat het oplevert

Gemeten met een echte sleutel en een echt versleuteld blok, veertig velden achter
elkaar, zoals een detailpagina van een project met twee realms en tien componenten:

| | per veld |
|---|---|
| `age`-binary als subprocess | 7,2 ms |
| `pyrage` in het proces | 0,18 ms |

De absolute getallen hangen van de machine af, de verhouding nauwelijks: de fork en de
tempfile zijn de kosten, niet het rekenwerk.

Twee winsten die los staan van de tijd:

- **Geen proces per veld.** De uitwaaiering die tussen twee Prometheus-scrapes door
  onzichtbaar blijft, is weg op het leespad.
- **De private sleutel gaat niet meer naar schijf.** Het binary leest zijn sleutel uit
  een bestand, dus elke ontsleuteling zette de sleutel in `/tmp`, al werd hij direct
  opgeruimd.

## Hoe het werkt

`_decrypt_in_process` parseert de private sleutel tot een `pyrage.x25519.Identity` en
laat `pyrage.decrypt` het blok openen. `pyrage` is een binding op de Rust-implementatie
van age en leest zowel de armored als de binaire vorm, dus bestaande projectbestanden
openen ongewijzigd.

`decrypt_age_content` blijft `async` terwijl er niets meer te wachten valt. Dat is
bewust: de handtekening is wat alle aanroepers onveranderd houdt.

Het foutgedrag is hetzelfde gebleven:

- ontbrekende inhoud of sleutel: `ValueError`;
- mislukte ontsleuteling: `Exception("Age decryption failed: ...")` uit de async-variant,
  `None` uit de sync-variant.

## Wat niet is omgezet

**De versleutelkant.** `encrypt_age_content` en `encrypt_age_content_sync` starten nog
steeds het binary. De winst is daar kleiner (versleutelen gebeurt bij het opslaan, niet
bij elke render) en `pyrage` 1.4.0 kent geen armor-uitvoer, terwijl de opgeslagen vorm
armored is. Die vorm met de hand nabouwen is meer risico dan de winst rechtvaardigt.
Dat beide richtingen wel uitwisselbaar zijn is gemeten en vastgelegd in
`tests/test_age_ontsleuteling_in_proces.py`, zodat die stap later op een meting rust.

**Geen cache.** Issue #145 stelde caching voor als alternatief. Ontsleutelde geheimen in
een cache leggen is een risico dat je terugkrijgt voor tijdwinst die de omzetting
hierboven al geeft.

## De metriek

`track_subprocess_memory("age")` meet de geheugenpiek rond een subprocess. Op het
leespad is er geen subprocess meer, dus die haak is daar weggevallen; hij staat nog waar
hij nog iets meet, namelijk rond het versleutelen.

Er is **geen** vervangende teller voor het aantal ontsleutelingen bij gekomen. De teller
`opi_connector_subprocess_calls_total{connector="age"}` bestond om uitwaaiering van
processen te zien, en een aanroep die geen proces start draagt dat risico niet. De
teller doet daarmee nog precies wat hij deed, en is tegelijk het bewijs voor deze
wijziging: hij hoort niet meer op te lopen terwijl je detailpagina's opent.

## Afhankelijkheden

- `pyrage>=1.4.0` (runtime-dependency in `operations-manager/python/pyproject.toml`).
  De wheel draagt geen license-metadata, dus staat er een `authorized_packages`-regel
  voor in de liccheck-configuratie; de repo zelf is MIT.
- Het `age`-binary blijft nodig in de image, voor versleutelen en voor SOPS.
