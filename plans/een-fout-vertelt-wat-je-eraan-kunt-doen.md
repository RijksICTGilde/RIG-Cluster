# Een fout vertelt wat je eraan kunt doen

Status: plan, 1 september 2026. Aanleiding: tijdens de databasestoring van vanavond kreeg een gebruiker die `zad.rijksapp.nl/projects/dd-mco/details` opvroeg dit te zien, kaal op een zwarte pagina:

```json
{
  "detail": "Template error: [Errno 111] Connect call failed ('172.30.19.11', 5432)"
}
```

Daar zitten drie problemen in, en ze vragen elk een andere oplossing. Het lekt een intern IP-adres en de databasepoort naar iedereen die de pagina opvraagt. Het is voor een gebruiker onbruikbaar en voor een beheerder misleidend, want de template deed niets fout: de database was onbereikbaar. En het is geen pagina maar een JSON-fragment, zonder opmaak, zonder navigatie, zonder weg terug.

**Scope.** `opi/server.py` (de exception handlers), de plekken in `opi/web/` en `opi/api/` die een onbewerkte uitzondering doorgeven, en een nieuw foutmodel voor de API. Dit plan verandert **niets** aan wanneer er een fout optreedt, alleen aan wat de aanroeper te zien krijgt.

## Wat er nu is, gemeten

### Het patroon bestaat al, maar dekt alleen 404

`opi/server.py:548` doet precies het goede voor een niet-gevonden pagina:

```python
if exc.status_code != 404 or request.url.path.startswith("/api"):
    return await http_exception_handler(request, exc)
if "text/html" not in request.headers.get("accept", ""):
    return await http_exception_handler(request, exc)
return HTMLResponse(_NOT_FOUND_PAGE, status_code=404)
```

Een browser krijgt een pagina, een API-client krijgt JSON, en de keuze hangt aan de `Accept`-header. Netjes. Maar de eerste regel filtert op `status_code != 404`, dus een 500 valt erdoorheen naar de standaard-JSON van FastAPI. Dat is wat er op het scherm stond.

De handler voor `Exception` erboven (`server.py:543`) logt alleen en gooit opnieuw. Er is dus nergens een 500-pagina.

### De ruwe uitzondering reist mee naar buiten

Geteld op de huidige main:

| plek | aantal |
|---|---|
| `detail=f"Template error: {...}"` in `opi/web/` | 9 |
| een onbewerkte uitzondering in `detail=` in `opi/web/` en `opi/api/` | 38 |
| `HTTPException`-raises in `opi/api/v2/router.py` | 47, waarvan 19 met een f-string in `detail` |

Elk van die f-strings kan alles bevatten wat de onderliggende laag toevallig in zijn foutmelding zet: hostnamen, IP-adressen, poorten, paden, en bij een databasefout ook wel eens een gebruikersnaam.

### Er is al een woordenschat voor fouten, alleen niet hiervoor

`opi/api/v2/models.py` heeft `ErrorCategory` met negen waarden: `ImagePull`, `CrashLoop`, `InvalidTarget`, `InvalidInput`, `OutOfMemory`, `HealthCheck`, `SyncFailed`, `ComparisonError`, `Unknown`. En `StatusError` combineert een `message` voor automatisering met een `explanation` voor mensen.

Dat is precies de goede denkwijze, maar het zit op de verkeerde plek: die objecten beschrijven **clusterfouten binnen een geslaagd statusantwoord**. Er is geen envelop voor een gefaald HTTP-antwoord. `problem+json` komt nergens in de codebase voor.

### Er is al een kenmerk om aan te haken

`opi/core/flow_id.py` zet via een contextvar een id op elke logregel, zichtbaar als `[req-b041ebb2]` of `[task-0b6a1c07]`. Dat is de sleutel tot een foutmelding die tegelijk veilig en bruikbaar is, en hij ligt er al.

## Het model

Eén regel: **wat de gebruiker ziet is stabiel en zegt wat hij kan doen; wat de beheerder nodig heeft staat in de log, gekoppeld met een kenmerk.**

```
gebruiker ziet:   "Er ging iets mis bij het ophalen van dit project.
                   Probeer het over een minuut opnieuw. Blijft het misgaan,
                   meld dan kenmerk req-b041ebb2."

log bevat:        [req-b041ebb2] Template render failed for projects/dd-mco/details:
                  [Errno 111] Connect call failed ('172.30.19.11', 5432)
```

Dezelfde fout, twee lezers, één kenmerk ertussen. De gebruiker krijgt niets gevoeligs en toch iets waarmee hij geholpen kan worden. De beheerder vindt met één grep de goede regel.

Drie gevolgen die de vorm bepalen:

**De `Accept`-header beslist, niet het pad.** Dat mechanisme staat er al voor 404 en werkt; breid het uit in plaats van er een tweede naast te zetten.

**`detail` bevat nooit een onbewerkte uitzondering.** Niet in `opi/web/`, niet in `opi/api/`. Dat is te toetsen, en daarmee kan het niet terugsluipen.

**De API krijgt een echte foutenvelop.** Geen los `{"detail": "..."}` maar een object met een categorie waarop een client kan sturen, in lijn met de `ErrorCategory` die er al is. `application/problem+json` is de aangewezen vorm: dat is de NL GOV API Design Rules-standaard die we voor onze eigen API's aanhouden, en het scheelt een eigen bedenksel.

## Wat er moet gebeuren

Vijf stappen. De eerste twee zijn samen al het grootste deel van de winst.

### 1. Een 500-pagina, langs hetzelfde pad als de 404

Verruim de conditie in `server.py:548` zodat ook een 5xx bij een browser als pagina aankomt. Dezelfde opzet als `_NOT_FOUND_PAGE`: eigen opmaak, een weg terug, en het kenmerk uit `flow_id` zichtbaar.

Een API-pad of een client die geen HTML vraagt blijft JSON krijgen, precies zoals nu.

### 2. Het kenmerk in beeld, de uitzondering in de log

De handler voor `Exception` logt al. Laat hem daarnaast het `flow_id` doorgeven aan de pagina, en zorg dat de oorspronkelijke uitzondering met datzelfde id in de log staat.

Dit is de stap die de foutmelding bruikbaar maakt: zonder kenmerk is "er ging iets mis" een doodlopende weg voor wie het meldt.

### 3. Een foutenvelop voor de API

Een model in de trant van `application/problem+json`: een korte titel, een stabiele categorie uit `ErrorCategory`, het kenmerk, en optioneel een `explanation` zoals `StatusError` die al kent. Geen onbewerkte uitzondering.

Hergebruik `ErrorCategory` en breid hem uit waar de bestaande waarden niet passen; verzin geen tweede vocabulaire naast de eerste.

### 4. De 38 plekken opruimen

Per plek de keuze: heeft deze fout een eigen boodschap nodig, of kan de generieke handler hem afvangen? Verreweg de meeste zijn het tweede. Waar wel een eigen boodschap hoort, is die geschreven voor de lezer en bevat hij de uitzondering niet.

De negen `Template error`-plekken horen sowieso in de tweede categorie: die naam wees de verkeerde kant op.

### 5. Een grendel

Een test die faalt zodra een `detail=` of een foutenvelop een onbewerkte uitzondering doorgeeft. Zonder die grendel staat er over een half jaar weer een IP-adres op het scherm.

## De toets

- `/projects/<naam>/details` met een onbereikbare database geeft een **pagina**, geen JSON, met een kenmerk erop;
- die pagina bevat **geen** IP-adres, geen poortnummer, geen hostnaam en geen padnaam uit de infrastructuur: dat is letterlijk te toetsen met een reguliere expressie op het antwoord;
- de log bevat op datzelfde moment wél de volledige fout, met hetzelfde kenmerk;
- dezelfde aanroep met `Accept: application/json` geeft de foutenvelop met een categorie, en dus geen HTML;
- een `/api`-pad geeft nooit HTML, ook niet aan een browser;
- een 404 blijft precies doen wat hij nu doet: dat is de regressietoets op het bestaande gedrag;
- `grep -rn 'detail=f"' opi/web/ opi/api/` levert geen enkele plek meer op waar een uitzondering in de tekst wordt geïnterpoleerd;
- de grendel uit stap 5 faalt aantoonbaar wanneer iemand zo'n plek terugzet.

## Waar op te letten

**Dit is beveiligingsrelevant, en niet alleen theoretisch.** Er stond vanavond een intern IP-adres en de databasepoort op het scherm van een gebruiker. Dat het achter een login zit maakt het niet ongedaan: de netwerkindeling van het platform hoort niet in een foutmelding. Behandel stap 4 daarom niet als opruimwerk dat nog wel een keer komt.

**Verander niet wat een bestaande client leest.** De 404-handler zegt het al: een `/api`-pad en een client die geen HTML vraagt blijven JSON krijgen. De foutenvelop uit stap 3 is een uitbreiding; controleer per bestaande consument (zad-cli voorop) of het `detail`-veld dat vandaag gelezen wordt blijft bestaan of netjes wordt uitgefaseerd.

**Een categorie is een belofte.** `ErrorCategory` bestaat juist zodat een client kan besluiten of hij opnieuw moet proberen. `InvalidInput` betekent dat opnieuw proberen zinloos is; de databasestoring van vanavond was precies het tegenovergestelde. Kies de categorie op wat de aanroeper eraan heeft, niet op waar de fout vandaan kwam.

**De tekst is het werk.** "Er ging iets mis" is niet beter dan een stacktrace, alleen minder gevaarlijk. Wat een gebruiker verder helpt is of hij moet wachten, iets moet corrigeren of iemand moet bellen. Schrijf die zinnen bewust; dit is geen stap die je aan een sjabloon overlaat.

**Alles wat ik hier een naam geef is een voorstel.** Er staan in dit plan geen bestaande identifiers voor het nieuwe foutmodel. Kies de namen bij het bouwen.

## Wat hierna nodig is

Als de envelop er staat, is een logische volgende stap dat de foutpagina bij bekende categorieën een gerichtere tekst toont: bij `OutOfMemory` iets anders dan bij `SyncFailed`. Dat kan pas als de categorie er is, en het hoort niet in deze taak.
