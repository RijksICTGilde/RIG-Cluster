# Zeven rode e2e-tests op de releasebranch

Op `release-augustus-2026` falen zeven browsertests. Ze zijn door **twee** agents onafhankelijk van elkaar gemeld, en allebei hebben ze geverifieerd dat de tests ook op de basistoestand falen, dus ze horen niet bij het werk waarmee ze bezig waren.

- `tests/e2e/test_lotc_domeinbeheer.py` — vier, rond de goedkeuringsdialoog
- `tests/e2e/test_shared_modal_blockade.py` — drie

Dat is meer dan cosmetisch. Het raakt de **gedeelde** bewerkdialoog: hetzelfde fragment bedient de goedkeuringspagina en de bewerkdialogen van een project (`opi/web/router_detail_edit.py`). Een groep rode tests op precies dat onderdeel is niet iets om mee naar main te gaan.

## Waarom dit een eigen taak is

De dialoog is deze week door drie handen gegaan: de modal is van handbouw naar htmx omgezet (RC-115), de dubbele kop en de lege foutbalk zijn eruit, en het icoon in de kop is verplaatst. Elk van die wijzigingen is apart groen bevonden, en toch staan er nu zeven rood. De vraag is dus niet alleen "wat is er stuk" maar ook **hoe deze zeven aan de aandacht konden ontsnappen** terwijl elke afzonderlijke tak groen was.

Er is één bekende kandidaat voor dat laatste, en die staat al opgeschreven: de wisselvalligheid rond gedeelde browsersessies. In `tests/e2e/test_wizard_cross_domain_policy.py` documenteert een `xfail`-reden dat twee tests een browsersessie delen, en een agent meldde eerder dat diezelfde test in de ene volledige run rood en in de andere groen was. Als de zeven daaronder vallen, is de reparatie isolatie en niet de dialoog.

## Wat er moet gebeuren

1. **Vaststellen wat er werkelijk faalt**, per test, met de melding erbij. Niet "ze doen het niet" maar wat er gemeten wordt en wat eruit komt.
2. **Onderscheiden tussen drie mogelijkheden**, en dat oordeel expliciet opschrijven per test:
   - de test meet iets dat met opzet veranderd is (dan gaat de test mee, met de reden);
   - de test is wisselvallig door gedeelde toestand tussen tests (dan is isolatie de reparatie, niet de assertie versoepelen);
   - er is werkelijk iets stuk in de dialoog (dan is dat de reparatie, en dan wil ik weten sinds welke commit).
3. **Repareren.** Een assertie afzwakken omdat hij rood staat, is precies hoe een suite waardeloos wordt; als dat toch het juiste antwoord is, moet er staan waarom.
4. **Aantonen dat het blijft staan**: draai de volledige e2e-suite twee keer achter elkaar en meld beide uitkomsten. Wisselvalligheid toont zich niet in één run.

## Wat er buiten valt

- Nieuwe functionaliteit in de dialoog.
- De verouderde sjabloonboom onder `opi/templates_lotc/admin/approvals/`, die niet is wat een gebruiker ziet.
- De vier rode unittests in `tests/test_taken_voortgang_link.py`: die horen bij het onafgemaakte werk van een andere sessie en staan als ongevolgd bestand in de werkboom.

## Verifieerbaar

`pytest -m e2e -q` twee keer achter elkaar groen, met per gerepareerde test in de commit waarom hij rood stond en wat eraan gedaan is.
