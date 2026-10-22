# Uploads: een plafond, een spill naar schijf en een slot

Wat het is: de drie uploadroutes van OPI lezen hun stream met een plafond, houden hooguit
een kleine marge in het geheugen, en er lopen er maar een paar tegelijk. Zonder die
grenzen kan een grote of trage upload OPI platleggen voor alle projecten: hij draait als
één proces (`exec python -m opi.server`, geen extra workers), dus alle gelijktijdige
requests delen één event loop en één geheugenbudget.

De functionele plafonds veranderen hier niet. Een bijlage mag 64 KB
(`MAX_ATTACHMENT_BYTES`), een image-tarball `IMAGE_UPLOAD_MAX_SIZE_MB`. Dit gaat over hoe
die gehaald worden, en over de grenzen die eromheen ontbraken.

## De drie routes

| Route | Plafond | Leest met |
|---|---|---|
| Bijlage via de wizard (`router_wizard_attachments.py`) | `MAX_ATTACHMENT_BYTES` | `read_capped` |
| Bijlage via de v2-API (het FILE-veld in `api/v2/router.py`) | `max_bytes` van het actieveld | `read_capped` |
| Image-push (`api/image_router.py`) | `IMAGE_UPLOAD_MAX_SIZE_MB` | `spill_capped(spill_after=0)` |

Komt er een vierde uploadroute, dan hoort die dezelfde lezer en hetzelfde slot te
gebruiken. `git grep UploadFile opi/` laat zien of dat nog de hele lijst is.

## De lezers

Alle drie staan in `opi/utils/uploads.py` en tellen op dezelfde manier:

- `iter_capped(upload, max_bytes)`: de blokken van 64 KB, met een `UploadTooLargeError` op
  het blok dat het plafond passeert. Wat erachter zit wordt niet meer gelezen, dus het
  aantal leesaanroepen is begrensd en niet de grootte van de upload.
- `read_capped(upload, max_bytes)`: de bytes in het geheugen, of `None` boven het plafond.
  Voor een bijlage, die met 64 KB niets te spillen heeft.
- `spill_capped(upload, max_bytes, spill_after=...)`: in het geheugen tot `spill_after`
  bytes (default `UPLOAD_SPILL_AFTER_BYTES`, 1 MB) en daarboven naar een tempbestand in
  `TEMP_DIR`, mode 0o600, net als de staging-store: wat hier langskomt kan een private key
  zijn. Geeft een `CappedUpload` met `content` of `path`, en de beller ruimt op met
  `cleanup()`. De image-push zet `spill_after=0`, dus een tarball van gigabytes raakt de
  heap nooit.

## Het slot

`upload_slot()` is een proces-globale `asyncio.Semaphore(MAX_CONCURRENT_UPLOADS)`, in de
geest van `BACKUP_MAX_CONCURRENT`. Alle drie de routes nemen er een plek in voordat ze de
stream gaan lezen, en geven hem in een `finally` vrij, ook bij een fout of een afgebroken
upload (het is een `async with`).

Is het slot vol, dan wacht de upload in een wachtrij. Een upload is meestal in een oogwenk
gelezen, dus meteen weigeren zou een gebruiker raken voor een drukte die een halve seconde
duurt. Die wachtrij is wel zelf begrensd op `UPLOAD_QUEUE_SIZE`: elk wachtend verzoek houdt
zijn verbinding en zijn stream vast, en een onbegrensde rij is weer precies de opstapeling
die dit plafond moet voorkomen.

Staat de rij vol, of staat een verzoek er langer dan `UPLOAD_QUEUE_WAIT_SECONDS` in, dan
volgt een 503 met een `Retry-After`.

Voor een bijlage wordt het slot rond het lezen van de stream gehouden, niet rond de hele
afhandeling. Voor de image-push neemt `BodyLimitMiddleware` het, vóór de parser, en houdt
het tot het antwoord verstuurd is: pas dan is de tarball van de tempschijf. Dat betekent
dat een trage skopeo-push zijn plek bezet houdt, en dat is de bedoeling, want zijn
tempbestand van gigabytes staat er in die tijd ook nog. De route neemt het slot daarna
niet nog eens: `upload_slot(request.scope)` ziet de markering `SLOT_HELD_IN_SCOPE` en doet
niets, zodat één push één plek kost.

## De bovengrens op de body

`BodyLimitMiddleware` (`opi/middleware/body_limit.py`) staat buiten sessie, CSRF en
autorisatie en weigert een te grote body voordat de multipart-parser hem ontvangt. De grens
is per route (`body_limit_for`):

- een pad met `/attachments`: `MAX_ATTACHMENT_BYTES` maal `ATTACHMENT_BODY_LIMIT_FACTOR`
  (16, dus 1 MB), ruimte voor de multipart-omlijsting om een bestand van 64 KB heen;
- een pad dat eindigt op `/images/push`: `IMAGE_UPLOAD_MAX_SIZE_MB` in bytes;
- elk ander pad: geen grens van deze middleware.

Niet één app-breed getal: een image-tarball hoort veel hoger te mogen dan een bijlage. Een
gedeclareerde `Content-Length` boven de grens wordt geweigerd voordat er iets gelezen is;
een chunked body wordt tijdens het lezen geteld en afgebroken op het blok dat de grens
passeert.

De sandbox-ingress zet `proxy-body-size: "0"` en de OpenShift-router kent geen
body-size-annotatie, dus de ingress vangt dit niet af. Deze middleware is de vangrail die
er niet van afhangt.

### De image-push: sleutel en slot vóór de parser

FastAPI parseert de multipart-body voordat de handler draait, dus een 401 uit
`validate_api_token` en een 503 uit het slot vallen pas nadat de hele body ontvangen is.
Voor een bijlage scheelt dat weinig, want de body is er op 1 MB begrensd. Voor de
image-push wel: tien gelijktijdige pushes van 5 GB zetten 50 GB op de tempschijf, en dat
vraagt zonder verdere maatregel geen geldige sleutel. Een volle schijf van OPI is een
storing voor alle projecten.

`BodyLimitMiddleware` is de enige plek die vóór de parser en vóór de autorisatie draait,
en doet daarom voor een pad dat eindigt op `/images/push` twee dingen meer:

1. **De projectsleutel.** De `X-API-Key` moet de sleutel zijn van het project in het pad.
   Zonder header of met de sleutel van een ander project volgt een 401 voordat er een byte
   body gelezen is. De vergelijking is `authenticate_project_api_key`
   (`opi/api/endpoint_util.py`), dezelfde functie die `validate_api_token` gebruikt, dus er
   is geen tweede vergelijking die uiteen kan lopen. De decorator op de route blijft staan:
   die zet de actor en normaliseert de projectnaam.
2. **Het uploadslot.** Een plek in `upload_slot()`, ook vóór de parser, en vrijgegeven als
   het antwoord verstuurd is. Is het slot vol en de wachtrij ook, dan volgt de 503 met
   `Retry-After` zonder dat er een body binnenkomt.

## Instelbaar

Alle getallen staan bij elkaar in `opi/core/config.py`, naast
`IMAGE_UPLOAD_MAX_SIZE_MB`:

| Setting | Default | Wat het doet |
|---|---|---|
| `MAX_CONCURRENT_UPLOADS` | 4 | gelijktijdige uploads over alle routes samen |
| `UPLOAD_QUEUE_SIZE` | 10 | uploads die mogen wachten op een plek; daarboven direct 503 |
| `UPLOAD_QUEUE_WAIT_SECONDS` | 30.0 | hoe lang een upload in de wachtrij mag staan |
| `UPLOAD_SLOT_RETRY_AFTER_SECONDS` | 5 | wat die 503 als `Retry-After` meegeeft |
| `UPLOAD_SPILL_AFTER_BYTES` | 1 MB | hierboven gaat een upload naar een tempbestand |
| `ATTACHMENT_BODY_LIMIT_FACTOR` | 16 | body-grens = `MAX_ATTACHMENT_BYTES` maal dit |

Het bijlageplafond zelf blijft `MAX_ATTACHMENT_BYTES` in
`opi/services/catalog/attachments/catalog_model.py`; de middleware leest dat, er is geen
tweede kopie van dat getal.

## Wat dit niet dekt

Een bijlageroute neemt zijn slot nog wel ná de parser, dus daar is het aantal bodies dat
tegelijk ontvangen wordt niet begrensd, alleen het aantal dat tegelijk gelezen wordt. Het
verschil is daar klein: `BodyLimitMiddleware` houdt zo'n body op 1 MB, en `uvicorn.run`
(`opi/server.py`) zet geen `limit_concurrency`, dus wat een bijlageroute kan opstapelen is
het aantal gelijktijdige verbindingen maal 1 MB. Voor de image-push, waar het om gigabytes
ging, valt sleutel en slot nu wel vóór de parser (zie hierboven).

Het slot is gedeeld over de drie routes, dus `MAX_CONCURRENT_UPLOADS` trage pushes bezetten
elke plek, en een bijlage-upload van een ander project krijgt dan na
`UPLOAD_QUEUE_WAIT_SECONDS` een 503. De plek is bezet vanaf de middleware, dus niet alleen
een trage skopeo houdt hem vast: een client die zijn body langzaam opstuurt doet dat ook, en
daar zit geen tijdgrens op. Gemeten met `MAX_CONCURRENT_UPLOADS=1`, zowel met een push die
zijn antwoord ophoudt als met een client die nog aan het sturen is: de bijlageroute geeft
503 met `Retry-After`, en daarna slaagt ze weer. Het vraagt wel een geldige
projectsleutel, dus anoniem is het slot niet te vullen. Wie die koppeling weg wil, moet
het vasthouden van de tarball een eigen semafoor geven naast het gedeelde leesslot. Dat
is een ontwerpwijziging, geen getal.

Het plafond op de tempschijf zelf. Het slot begrenst het aantal tarballs, niet hun
omvang tegen de ruimte die er is. Een push kost twee keer de tarball op de tempschijf:
`spill_capped` schrijft zijn eigen bestand, en de `UploadFile` van Starlette heeft de body
daarboven al naar een eigen tempbestand gespoold (`spool_max_size` is 1 MB). Dat tweede
bestand is meteen unlinked en staat dus niet in de map, maar kost wel blokken. Gemeten op
de echte pushroute met een tarball van 8 MB: 16 MB in gebruik op het moment dat skopeo
wordt aangeroepen, factor 2,00. Met `IMAGE_UPLOAD_MAX_SIZE_MB` op 5120 en
`MAX_CONCURRENT_UPLOADS` op 4 is de bovengrens dus 40 GB, en `/tmp` is een emptyDir met
`sizeLimit: 8Gi` (`bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production/patches/deployment.yaml:135`,
staging idem; `TMPDIR` en `TEMP_DIR` wijzen er beide naar). Eén volle push past daar al
niet in. Dit is niet nieuw, het was zonder slot onbegrensd, maar wie de tempschijf echt
wil dichtzetten doet dat met deze drie getallen tegen elkaar, niet met het slot alleen.

Een gedeeld slot over meerdere replica's of workers. Zolang OPI één proces per cluster is,
is het semafoor een echt globaal plafond. Draait OPI ooit op meer dan één, dan is het dat
niet meer en is een gedeelde teller nodig (database of Redis).

## Afhankelijkheden

Geen nieuwe. `asyncio`, `tempfile` en de `UploadFile` van FastAPI.
