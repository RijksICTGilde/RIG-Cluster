# AGE-ontsleuteling in het proces

## Wat het is

`decrypt_age_content` en `decrypt_age_content_sync` (`opi/utils/age.py`) ontsleutelen
sinds deze wijziging in het proces zelf, via `pyrage`, in plaats van per aanroep het
`age`-binary als subprocess te starten.

De aanleiding was issue #145, dat de projectdetailpagina traag noemde. Dat klopt niet
meer: de tweede oorzaak uit dat ticket (`ensure_projects_fresh()` op het request-pad) is
met de ProjectStore verdwenen, en de decryptie die overblijft kost milliseconden. Wat
wel bleef is een fork per veld, bij elke render en voor elke gelijktijdige bezoeker.

## Wat het oplevert

Gemeten met een echte sleutel en een echt versleuteld blok, veertig velden achter
elkaar:

| | per veld |
|---|---|
| `age`-binary als subprocess | 7,2 ms |
| `pyrage` in het proces | 0,18 ms |

De absolute getallen hangen van de machine en de belasting af: de fork en de tempfile
zijn de kosten, niet het rekenwerk.

Twee winsten die los staan van de tijd:

- **Geen proces per veld.** OPI is eerder OOMKilled door subprocessen die uitwaaieren in
  pieken die tussen twee Prometheus-scrapes door onzichtbaar blijven.
- **De private sleutel gaat niet meer naar schijf.** Het binary leest zijn sleutel uit
  een bestand, dus elke ontsleuteling zette de sleutel in `/tmp`, al werd hij direct
  opgeruimd.

## Hoe het werkt

`_decrypt_block` parseert de private sleutel tot een `pyrage.x25519.Identity` en
laat `pyrage.decrypt` het blok openen. `pyrage` is een binding op de Rust-implementatie
van age en leest zowel de armored als de binaire vorm, dus bestaande projectbestanden
openen ongewijzigd.

`decrypt_age_content` blijft `async` terwijl er niets te wachten valt: dat houdt alle
aanroepers onveranderd.

Het foutgedrag, sinds RC-453 een antwoord per soort:

- ontbrekende inhoud of sleutel: `ValueError`;
- mislukte ontsleuteling: `AgeDecryptionError` uit de async-variant, `None` uit de
  sync-variant. `AgeDecryptionError` erft van `ValueError`, zodat een aanroeper met
  `except ValueError` hem niet mist;
- een plaintext die geen utf-8 is: dezelfde `AgeDecryptionError`, niet een kale
  `UnicodeDecodeError` die door de sync-variant heen lekt.

## De versleutelkant is sinds RC-263 ook in-process

`encrypt_age_content` en `encrypt_age_content_sync` gebruiken `pyrage.encrypt(...,
armored=True)`. De aanleiding was niet de snelheid maar de event loop: de synchrone
variant startte het binary midden op de loop, en die loop deelt OPI met FastAPI, de
TaskWorker en alle schedulers.

De uitvoer is niet byte-gelijk aan die van het binary, want age is niet deterministisch.
Wel uitwisselbaar, en dat is wat telt: `tests/test_age_ontsleuteling_in_proces.py` toetst
beide richtingen tegen het echte binary, voor de async- en de sync-variant en voor een
bijlage die via base64 door dezelfde versleuteling gaat.

## Wat niet is omgezet

**Geen cache.** Issue #145 stelde caching voor als alternatief. Ontsleutelde geheimen in
een cache leggen is een risico dat je terugkrijgt voor tijdwinst die de omzetting
hierboven al geeft.

## De metriek

`track_subprocess_memory("age")` meet de geheugenpiek rond een subprocess. Sinds RC-263
start `opi/utils/age.py` er geen meer, dus `opi_connector_subprocess_calls_total
{connector="age"}` loopt niet meer op. Dat is tegelijk het bewijs voor beide
wijzigingen, en het is wat `TestDeProcesteller` toetst.

Er is geen vervangende teller voor het aantal ontsleutelingen gekomen: die teller bestond
om uitwaaiering van processen te zien, en een aanroep die geen proces start draagt dat
risico niet.

## Afhankelijkheden

- `pyrage>=1.4.0` (runtime-dependency in `operations-manager/python/pyproject.toml`).
  De wheel draagt geen license-metadata, dus staat er een `authorized_packages`-regel
  voor in de liccheck-configuratie; de repo zelf is MIT.
- Het `age`-binary blijft nodig in de image, voor SOPS. Sinds RC-453 maakt
  `generate_age_key_pair` in `opi/utils/age.py` ook het sleutelpaar in het proces, dus
  `age-keygen` wordt door OPI zelf niet meer gestart.
