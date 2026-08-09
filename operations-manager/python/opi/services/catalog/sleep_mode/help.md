# Slaapstand

Zet deployments die een tijd niet gebruikt zijn in slaapstand, en wek ze weer op wanneer iemand ze nodig heeft. Een slapende deployment gebruikt geen geheugen en geen CPU meer.

## Wanneer gebruik je dit?

- Je hebt preview- of pull-request-omgevingen die vaak stilliggen
- Je hebt test- of demo-omgevingen die niet dag en nacht hoeven te draaien
- Je wilt niet handmatig omgevingen aan- en uitzetten

Doe dit **niet** bij een productieomgeving of bij iets wat altijd meteen moet antwoorden: de applicatie start koud op, dat duurt even.

## Wat wordt er ingesteld?

Met **match** bepaal je welke deployments meedoen, en met een deadline na hoeveel tijd zonder gebruik ze gaan slapen. Een slapende deployment wordt teruggeschaald naar nul pods. Wekken kan met de knop in het portaal, via de API, en bij een web-gepubliceerd component ook door de URL te bezoeken: de bezoeker krijgt dan een "applicatie wordt gestart"-pagina tot de applicatie er weer is.

**Let op:** het is slaapstand, geen sluimerstand. Er wordt niets bewaard: sessies, caches en geheugen zijn na het wekken weg. Gegevens in een database of in opslag blijven uiteraard gewoon staan.
