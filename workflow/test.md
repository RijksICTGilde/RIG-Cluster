# Testeisen

De lagen en wanneer elke laag draait staan in `docs/development/toetsen.md`.

- **De testfase draait de brede laag, eenmaal per taak**: `task test-wide`, naast je eigen selectie en `task test`. Een sessie start daarvoor een eigen test-Postgres op 127.0.0.1 (RC-478), er is geen handwerk nodig. Zet het aantal geslaagde en mislukte toetsen in je verslag.
- De brede laag vangt wat een selectie mist: toetsen die de hele codebase aflopen, zoals de schrijversinventaris (`tests/test_schrijvers_inventaris.py`), de conventie- en lagencontroles en de golden tests. Verplaats je code, dan faalt juist zo'n toets.
- Een rode toets in de brede laag is rood, ook als hij buiten je venster lijkt te liggen. Repareer hem of zet op de PR waarom hij niet van deze taak is, met het bewijs dat hij op de basiscommit ook rood is.
- De live groepen en de sandboxsuite blijven waar `toetsen.md` en `review.md` ze plaatsen.
