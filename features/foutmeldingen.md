# Foutmeldingen

Wat een aanroeper te zien krijgt als er iets misgaat, en hoe een beheerder daar de
volledige fout bij terugvindt.

## Waarom

Tijdens een databasestoring kreeg een gebruiker die `zad.rijksapp.nl/projects/dd-mco/details`
opvroeg dit kaal op een zwarte pagina:

```json
{"detail": "Template error: [Errno 111] Connect call failed ('172.30.19.11', 5432)"}
```

Drie problemen tegelijk. Het lekte een intern IP-adres en de databasepoort. Het wees de
verkeerde kant op, want de template deed niets fout: de database was onbereikbaar. En het
was geen pagina maar een JSON-fragment, zonder opmaak, zonder navigatie, zonder weg terug.

## Het model

Een fout heeft twee lezers met tegengestelde behoeften, en er zit een kenmerk tussen:

```
gebruiker ziet:   "De projectpagina kon niet worden opgebouwd.
                   Probeer het over een minuut opnieuw.
                   Blijft het misgaan, meld dan dit kenmerk: req-b041ebb2"

log bevat:        [req-b041ebb2] Renderen van de projectpagina mislukt:
                  [Errno 111] Connect call failed ('172.30.19.11', 5432)
                  Traceback (most recent call last): ...
```

Het kenmerk komt uit `opi/core/flow_id.py` en staat al op elke logregel van hetzelfde
verzoek. `kubectl logs ... | grep req-b041ebb2` geeft het hele verhaal.

## Wie krijgt wat

De `Accept`-header beslist, niet het pad -- op een uitzondering na: onder `/api` komt nooit
markup uit, ook niet als een browser hem opvraagt.

| aanroeper | 404 | 5xx |
|---|---|---|
| browser, geen `/api` | de 404-pagina | de 5xx-pagina met kenmerk |
| client die geen HTML vraagt | `{"detail": "Not Found"}` | de foutenvelop |
| elk `/api`-pad | `{"detail": ...}` | de foutenvelop |

Elke **4xx behalve 404 is onveranderd**: daar leest een client op wat er staat, en dit is
geen goed moment om dat te verplaatsen.

### De foutenvelop

`application/problem+json` volgens RFC 7807, de vorm die de NL GOV API Design Rules
voorschrijven. Alleen voor 5xx.

```json
{
  "type": "about:blank",
  "title": "Internal Server Error",
  "status": 500,
  "detail": "De projectpagina kon niet worden opgebouwd. Probeer het over een minuut opnieuw. Blijft het misgaan, meld dan kenmerk req-b041ebb2.",
  "instance": "/projects/dd-mco/details",
  "category": "InternalError",
  "reference": "req-b041ebb2"
}
```

* `detail` blijft bestaan en blijft een gewone zin. Dat is het veld dat zad-cli vandaag
  afdrukt, dus de envelop is een uitbreiding en geen breuk. Het kenmerk staat er in mee,
  zodat een client die alleen `detail` toont toch iets meldbaars laat zien.
* `category` komt uit `ErrorCategory` in `opi/api/v2/models.py` -- dezelfde woordenschat als
  de clusterfouten in een statusantwoord. Voor een 5xx is dat `InternalError`: het lag niet
  aan je verzoek en opnieuw proberen kan helpen.
* `reference` is het kenmerk. Op het scherm heet dat "kenmerk"; in de API houdt het de
  Engelse naam die de rest van de velden ook heeft.

Er zit bewust **geen `explanation`** naast `detail`. Bij `StatusError` scheidt dat veld de
ruwe clustertekst van de uitleg voor mensen; hier is er geen ruwe tekst meer, dus zou het
een tweede kopie van `detail` zijn.

`InternalError` is nieuw. Een taak met `error_type: internal_error` houdt voorlopig
`error_category: Unknown`: dat is een waarde die clients al uitlezen, en die verplaatsen is
een API-wijziging op zichzelf.

De envelop staat in het OpenAPI-document: `ProblemDetail` als schema, en elke
`/api/`-operatie noemt hem als `5XX`. Dat gebeurt in `custom_openapi()` in `opi/server.py`
en niet als `responses={500: ...}` bij elk endpoint - de envelop geldt overal hetzelfde, en
honderd decorators zijn honderd plekken die uit de pas kunnen lopen.

## Een foutmelding schrijven

De regel: **`detail` bevat nooit een onbewerkte uitzondering.** Wat je schrijft is wat de
lezer eraan heeft -- moet hij wachten, iets corrigeren of iemand bellen.

```python
except Exception as e:
    logger.exception("Verversen van het project mislukt")
    raise HTTPException(
        status_code=500,
        detail="Het project kon niet worden ververst. Probeer het over een minuut opnieuw.",
    ) from e
```

De handler zet er "Blijft het misgaan, meld dan kenmerk req-xxxxxxxx." achter; die zin
schrijf je dus niet zelf.

**Antwoord je zelf, dan komt de handler er niet aan te pas.** Een fragment, een dict die
verderop een body wordt, een regel in een lijst: daar zet je het kenmerk er zelf achter met
`met_kenmerk(zin, kenmerk_van(request))`. Heb je geen `request` in handen -- een
hulpfunctie die een kaart vult -- dan geeft `kenmerk_nu()` het kenmerk van het verzoek dat
nu loopt:

```python
except Exception:
    logger.exception("Ophalen van het resourcegebruik voor %s mislukt", project_name)
    ctx["usage_error"] = met_kenmerk("Probeer het over een minuut opnieuw.", kenmerk_van(request))
```

Voor een mislukte render is er `log_render_failure`, die het regelnummer en de bronregel
van Jinja2 in de log zet in plaats van in het antwoord:

```python
except Exception as e:
    log_render_failure(logger, "het dashboard", e)
    raise HTTPException(status_code=500, detail="Het dashboard kon niet worden opgebouwd. ...") from e
```

### Wat wel mag

Een smalle, eigen uitzondering die zijn boodschap aan een **4xx** meegeeft. Die boodschap
IS de tekst voor de lezer:

```python
except SkopeoValidationError as e:
    raise HTTPException(status_code=400, detail=str(e)) from e
```

## De grendel

`tests/test_geen_uitzondering_in_foutmelding.py` loopt met de AST over `opi/web/` en
`opi/api/` en maakt rood zodra de tekst van een uitzondering naar buiten reist:

* uit een **brede vangst** (`except Exception`) nooit -- wat daar binnenvalt is onbekend;
* naar een **5xx** ook niet uit een smalle vangst -- dat is de laag waarvan de aanroeper
  niets hoort te weten.

### Welke deuren de grendel kent

Niet "elke deur naar buiten" -- dat is een belofte die een AST-sweep niet waar kan maken.
Hij kent er vijf, en dat is precies wat hij dekt:

| deur | de vorm die erachter stond |
|---|---|
| `HTTPException` | `detail=f"Template error: {e}"` -- de storing zelf |
| antwoordklassen | `JSONResponse({"error": f"Encryption failed: {e}"}, 500)` in de gereedschapsroutes |
| sjabloon-render | `ctx["usage_error"] = str(e)` en daarna `render(..., context=ctx)` -- de Prometheus-fout op het tabblad Project |
| een teruggegeven waarde | `return {"success": False, "error": f"Database restore error: {e}"}`, die de aanroeper in een 500-body zet |
| `list.append(...)` | een regel per onderdeel in de logs-body |

Wat hij **niet** ziet, en dus niet belooft: een waarde die via een attribuut of een
buitenstaand object weglekt, iets dat eerst `opi/web`/`opi/api` verlaat (de managerlaag
valt buiten de gemeten mappen), en een uitzondering die wordt opgeslagen en pas bij een
later verzoek getoond.

De besmetting reist mee de handler **uit**: bij het resourcegebruik stond de toekenning in
het except-blok en de `render` eronder, buiten de `try`. De naam van de uitzondering zelf
blijft binnen de handler -- Python maakt hem aan het eind van het blok los -- dus een
tweede blok met dezelfde naam `e` raakt er niet door besmet.

De controle volgt tussenstappen (`error_msg = str(e)` en daarna `detail=f"...{error_msg}"`),
en een statuscode die niet uit de aanroep is af te lezen telt als fout: een grendel die bij
twijfel doorlaat is geen grendel. Een antwoordklasse zonder `status_code` telt als 200 --
daar mag een smalle, eigen uitzondering zijn boodschap wel meegeven, net als een render of
een teruggegeven dict.

### Een gewogen uitzondering

Een plek die de regel bewust niet volgt draagt `# foutmelding-gewogen: <reden>` in zijn
except-blok. Zonder reden telt het teken niet, en een test houdt het aantal op ten hoogste
twee: een uitzondering die je moet opschrijven blijft zichtbaar, een gat in het model niet.
Vandaag is er één, in `opi/api/federation_router.py`: de health-check zet de fout van een
peer naast de url van diezelfde peer, achter de master-sleutel.

## Bestanden

| bestand | wat erin zit |
|---|---|
| `opi/core/errors.py` | de 404- en 5xx-pagina, het kenmerk, `log_render_failure` |
| `opi/api/v2/models.py` | `ProblemDetail`, `ErrorCategory`, `category_for_status` |
| `opi/server.py` | de twee handlers die kiezen tussen pagina en envelop |
| `tests/test_server_error_page.py` | wat een aanroeper krijgt, en de storing zelf nagespeeld |
| `tests/test_geen_uitzondering_in_foutmelding.py` | de grendel plus een tegenproef per deur |
| `tests/test_geen_uitzondering_via_de_andere_deuren.py` | dezelfde deuren gemeten op de route zelf |

## Wat hierna kan

De foutpagina kan bij bekende categorieën een gerichtere tekst tonen: bij `OutOfMemory`
iets anders dan bij `SyncFailed`. Dat kan pas nu de categorie er is.
