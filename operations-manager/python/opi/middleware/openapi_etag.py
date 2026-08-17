"""Een ``ETag`` op ``/openapi.json``, zodat een client op verandering kan cachen.

DE MELDING (zad-cli, punt 1)

De zad-cli leest de spec sinds kort live in plaats van uit een meegeleverde kopie, want
daar staat in wat een veld accepteert. Het document geeft echter geen enkel signaal dat het
veranderd is: ``info.version`` stond op ``0.1.0`` en bleef daar door alle wijzigingen van een
week heen, en er kwam geen ``ETag`` of ``Last-Modified`` mee. Een client kan dan alleen op
TIJD cachen, en die cache stond op een uur.

Wat dat kost, is precies gemeten: op de dag dat de standaard van ``sleep-mode.wake-mode``
veranderde, vertelde de CLI zijn gebruikers een uur lang de oude waarheid.

WAAROM EEN ETAG EN NIET ALLEEN EEN VERSIENUMMER

Allebei, want ze doen iets anders. Het versienummer (``x-spec-revision`` in ``info``, gezet
in ``custom_openapi``) zegt WELKE build dit document maakte, en is te lezen zonder een tweede
verzoek. De ETag maakt een CONDITIONELE GET mogelijk: de client stuurt zijn vorige waarde mee
in ``If-None-Match`` en krijgt een 304 zonder body als er niets veranderde. Daarmee kan de
cache op verandering in plaats van op tijd, en is de vertraging nul in plaats van een uur.

De waarde is een hash van het document zelf en niet van de commit. Twee builds van dezelfde
code geven dan dezelfde ETag, en een wijziging die de spec niet raakt kost een client geen
nieuwe download. Dat is ook meteen de reden dat dit hier gebeurt en niet in ``custom_openapi``:
de hash hoort bij wat er over de lijn gaat.

WAAROM MIDDLEWARE EN GEEN EIGEN ROUTE

FastAPI bedient ``/openapi.json`` zelf, en ``/docs`` verwijst naar diezelfde URL. Die route
vervangen kost de documentatiepagina; deze middleware laat hem staan en beantwoordt alleen
het conditionele geval zelf.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.requests import Request

#: Het pad dat FastAPI voor het document gebruikt. Staat hier los zodat de middleware zich
#: niet met andere verzoeken bemoeit.
OPENAPI_PATH = "/openapi.json"


def _etag_for(document: dict[str, object]) -> tuple[str, bytes]:
    """De ETag en de body die erbij hoort.

    ``sort_keys`` omdat de volgorde van een dict geen betekenis heeft voor een lezer: zonder
    dat zou een herstart met een andere invoegvolgorde een nieuwe ETag opleveren en elke
    client onnodig laten downloaden.
    """
    body = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    return f'"{hashlib.sha256(body).hexdigest()[:32]}"', body


class OpenApiETagMiddleware(BaseHTTPMiddleware):
    """Beantwoordt ``GET /openapi.json`` met een ETag, en met 304 als er niets veranderde."""

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if request.url.path != OPENAPI_PATH or request.method not in ("GET", "HEAD"):
            return await call_next(request)

        etag, body = _etag_for(request.app.openapi())

        # Een client mag meerdere waarden meesturen, en "*" betekent "wat je ook hebt".
        binnengekomen = [deel.strip() for deel in request.headers.get("if-none-match", "").split(",") if deel.strip()]
        if etag in binnengekomen or "*" in binnengekomen:
            # 304 draagt geen body, wel de ETag: anders weet de client niet waar hij nu op staat.
            return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-cache"})

        return JSONResponse(
            content=json.loads(body),
            headers={
                "ETag": etag,
                # no-cache is niet "niet cachen" maar "cache, en vraag elke keer of het nog
                # klopt". Dat is precies wat we willen: de conditionele GET is goedkoop en de
                # client loopt nooit achter.
                "Cache-Control": "no-cache",
            },
        )
