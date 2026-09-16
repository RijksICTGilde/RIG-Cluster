# Lagen van de operations-manager image

Wat er per deploy opnieuw door de leiding gaat, en waarom `operations-manager/Dockerfile`
eruitziet zoals hij eruitziet.

## Waarom dit telt

Een sandboxdeploy serialiseert het image twee keer volledig: `buildx --load` exporteert
het naar de docker-daemon, en `kind load` doet daar `docker save` overheen om het in
containerd op de node te importeren. Het gepubliceerde image is ongeveer 971 MB, dus dat
is circa 2 GB tar-verkeer per deploy. Die twee serialisaties staan hier los van; wat dit
bestand regelt is hoeveel er per codewijziging OPNIEUW gebouwd en verstuurd wordt.

## De vorm

Vier stages, van zelden naar vaak wijzigend:

| Stage | Wat erin zit | Wanneer hij opnieuw bouwt |
|---|---|---|
| `uv` | het gepinde uv-image, alleen als bron voor de uv-binary | `UV_VERSION` verandert |
| `base-system` | apt-pakketten, kubectl, sops, mc, kopia, skopeo, chisel, uv | een van de gepinde `ARG *_VERSION` verandert |
| `dependencies` | `uv sync` van de productie-dependencies | `pyproject.toml` of `uv.lock` verandert |
| `application` | alembic.ini, entrypoint, `opi`, `manifests`, `extensions`, `static` | de broncode verandert |

## Vier regels die makkelijk stilletjes sneuvelen

**Eigenaarschap hoort op de COPY.** `COPY --chown=appuser:appuser`, nooit een
`RUN chown -R /app` erachteraan. Een recursieve chown herschrijft elk bestand, en een
herschreven bestand is in een overlay-laag een nieuw bestand: die ene RUN was een
tweede, volledige kopie van alles wat erboven gekopieerd was. `/app` zelf krijgt zijn
eigenaar in de RUN die de gebruiker aanmaakt, want `WORKDIR` maakte die map als root aan
en een `COPY --chown` raakt alleen wat hij schrijft.

**`static` gaat in meer dan een laag.** `promo.mp4` is 28 MB en verandert nooit; `css`
en `js` veranderen elke week. Samen in een laag betekent dat een gewijzigde stylesheet
de video meesleept. Zet je iets nieuws in `static/`, dan hoort het in een van die COPY's;
`tests/test_image_layer_guard.py` meet dat en valt om als een bestand nergens genoemd
wordt.

**`node_modules` hoort er niet in.** esbuild bouwt er `codemirror-bundle.js` mee, de
runtime heeft alleen die bundel nodig. In `.dockerignore` staat daarom `**/node_modules/`
en niet `node_modules/`: een patroon zonder `**/` wordt alleen tegen de WORTEL van de
buildcontext gelegd, en de boom die bestaat staat op
`operations-manager/python/static/js/node_modules`. Zelfde valkuil als bij `**/tests/` en
`**/__pycache__/`.

**uv is gepind.** `ghcr.io/astral-sh/uv:${UV_VERSION}`, niet `:latest`.
Met `latest` invalideert een uv-release die laag en alles erna, inclusief de `uv sync`
van 189 MB: een build die normaal stage 1 en 2 oversloeg bouwt dan ineens alles.

## Gemeten

Twee builds op de dev-server, met een regel erbij in `opi/server.py` ertussen:

| | voor | na |
|---|---|---|
| totaal image | 877 MB | 801 MB |
| `chown -R appuser:appuser /app` | 46,3 MB | laag bestaat niet meer |
| kubectl-laag | 80,1 MB | 58,5 MB |
| opnieuw gebouwd na een wijziging in `opi/` | 92,2 MB | 8,6 MB |

Bij die laatste build was elke stap CACHED op `COPY ... ./opi` na, stage 1 en 2 dus
volledig uit cache. De 877 MB is gemeten met een `node_modules` van 7,7 MB in de
context; op een werkplek waar esbuild echt gedraaid heeft is die boom 28 MB en scheelt
het navenant meer.

## Nameten

```bash
docker buildx build --builder rig-sandbox-builder --target application --load \
  -t operations-manager -f operations-manager/Dockerfile .

# welke lagen een codewijziging opnieuw kost
docker history operations-manager --format '{{.Size}}\t{{.CreatedBy}}' | head -10

# node_modules hoort er niet te zijn
docker run --rm --entrypoint sh operations-manager -c 'ls /app/static/js/node_modules'

# en de container draait als 1001
docker run --rm --entrypoint sh operations-manager -c 'id -u'
```

Bouw niet zonder `--builder rig-sandbox-builder`: die heeft een geheugengrens en houdt de
buildcache vast. Zie `scripts/build-preflight.sh` en `tests/test_build_guard.py`.
