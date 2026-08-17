# Eigen vormgeving vervangen door componenten

Status: plan, 11 augustus 2026. Aanleiding: het logvenster is een met de hand gebouwd zijpaneel. Zestien eigen CSS-regels voor de knop, de achtergrond, het paneel en de kop, terwijl het thema daar componenten voor levert. Het is geen gebroken scherm, en dat is juist het punt: het valt niet op, en daarom blijft het staan.

Dit is de volgende stap na RC-67. Daar ging ROOS eruit; hier gaat het om wat er in de plaats van componenten met de hand is nagebouwd.

## Wat er nu is, gemeten

| | |
|---|---|
| eigen regels voor het logvenster | **16** (`.log-viewer-btn`, `-backdrop`, `-panel`, `-header`, ...) |
| eigen regels in `static/css/project-details.css` | **220** |
| stijlbladen in `static/css/` | **15** |
| oude themaklassen in het logsjabloon | **0** |

Die laatste rij zegt dat de omzetting van RC-67 hier geslaagd is: er zit geen RVO meer in. Wat overblijft is eigen vormgeving, en die volgt geen themawissel en geen licht/donker-keuze.

**Het thema heeft er componenten voor.** In `lotc_nldd` staan `sheet`, `modal-dialog` en `inline-dialog`. Een `sheet` is wat een zijpaneel is.

## Wat er moet gebeuren

1. **Het logvenster op componenten.** De knop wordt `<c-button>`, het paneel een `sheet` of dialoog. De zestien regels vervallen.

2. **Meet eerst of het component het gedrag verdraagt.** Dit paneel vult zichzelf en houdt een websocket open: logs komen binnen terwijl het openstaat. Een dialoog die zijn inhoud bij het openen ophaalt is iets anders dan een die live bijwerkt. Kan het component dat niet, dan is dat de uitkomst en niet een reden om er omheen te bouwen; schrijf dat op en kaart het aan bij het thema.

3. **Loop daarna de andere 204 regels in `project-details.css` langs**, en de overige veertien stijlbladen. Per blok één vraag: bestaat hier een component voor? Zo ja, gebruiken. Zo nee, laten staan en opschrijven waarom, zodat de volgende niet opnieuw zoekt.

## Wat NIET weg moet

**Klassen waar JavaScript aan hangt.** `config-item`, `config-code`, `copy-btn`, `deployment-section`, `is-hidden`, en hier ook `log-viewer-panel` als het script hem opzoekt. Die zien eruit als opmaak en zijn het niet; ze zijn vandaag al eens bijna gesneuveld.

**Opmaak die het thema echt niet levert.** Het doel is niet nul eigen CSS, het doel is dat er geen component wordt nagebouwd. Een paar regels voor iets dat het design system niet kent is prima, mits er staat waarom.

## Waar op te letten

**Werkt het nog.** Een paneel dat er beter uitziet maar geen logs meer toont is een verslechtering. Toets per omgezet blok dat het gedrag intact is: opent hij, komen de regels binnen, sluit hij, en blijft de websocket werken.

**Kijk ernaar met beeld.** Groene tests zeggen niets over hoe een scherm eruitziet; dat is vandaag vijf keer gebleken, waaronder een voettekst die in de inhoudskolom hing en een menu dat wel in de DOM stond maar niet openging.

**Niet en passant verbeteren.** Dit is een omzetting. Ziet iemand onderweg iets anders dat beter kan, dan is dat een aantekening en niet een tweede wijziging in dezelfde diff.
