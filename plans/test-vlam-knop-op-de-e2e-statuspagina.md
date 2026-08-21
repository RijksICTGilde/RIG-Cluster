# Test-VLAM-knop op de e2e-statuspagina

**Status**: uitgevoerd in RC-147 (PR #143)
**Datum**: 2026-08-21
**Context**: vervolg op RC-142/RC-144 (beide gemerged). De e2e-allservices-pod heeft al een actieve vlam-probe (`GET /v1/models`, groen in de sandbox tegen de stub) en een testmail-knop als bestaand patroon voor "doe echt iets en toon het resultaat". Gebruikerswens: vanaf de statuspagina een echte chat-completion kunnen doen met een eigen token, evt. model en vraag, zodat de keten inclusief authenticatie en modelantwoord te bewijzen is. Alles in `images/e2e-allservices/` plus de stub-helper; OPI-code wijzigt niet.

## Onderdeel 1: het formulier en de handler (Go)

- Een **"Test VLAM"-blok** op de statuspagina, alleen gerenderd wanneer het vlam-target gebonden is (zelfde plek en stijl als het Testmail-blok in `server.go`).
- Velden: **token** (`<input type="password">`), **model** (tekstveld, vooringevuld met `first_model` uit het laatste probe-resultaat als dat er is), **vraag** (tekstveld, een zinnige default mag).
- `POST /vlam-chat`: server-side `POST {VLAM_API_URL}/v1/chat/completions` met `Authorization: Bearer <token>`, body `{model, messages:[{role:"user", content:<vraag>}]}`, géén streaming. Timeout ruim (minuten, taalmodellen denken na); toon daarna het antwoord (de content van de eerste choice) of de fout, zoals de testmail-uitkomst getoond wordt.
- **Het token wordt nergens opgeslagen, nergens gelogd en nooit teruggetoond** (ook niet als value in het formulier na een POST). De bestemming staat vast: alleen de geïnjecteerde `VLAM_API_URL` en alleen dat ene pad; dit is geen open proxy. In logs alleen status en latency, nooit de vraag of het antwoord.
- Foutweergave volgt de probe-conventie: benoem welke hop verdacht is (geen antwoord = netwerkpad; 401/403 = token; 404/model-fout = modelnaam; 5xx = proxy of VLAM).

## Onderdeel 2: de stub leert chat-completions

- `tests/e2e/helpers/vlam_stub.py`: de haproxy-config beantwoordt ook `POST /v1/chat/completions`, met een vast, herkenbaar OpenAI-vormig antwoord waarvan de content duidelijk maakt dat het de stub is (bijv. "dit is de vlam-stub"). De stub controleert het token niet; dat is bewust en staat als commentaar bij de config.
- Let op: haproxy `http-request return` kan alleen op de request oordelen; als method+path-matching in de bestaande opzet niet volstaat, mag de stub-config anders worden opgelost zolang hij statisch en zonder extra image blijft.

## Onderdeel 3: sandbox-E2E-assertie

- `tests/e2e/test_sandbox_vlam.py`: één extra assertie die het formulier echt gebruikt (Playwright: velden vullen, knop, antwoord op de pagina) tegen de stub, met een nep-token; de test zegt expliciet dat de stub het token niet controleert en dat de productiewaarde van deze knop juist in de tokencontrole door het echte VLAM zit.

## Aandachtspunten

- De statuspagina is publiek bereikbaar. De knop geeft niemand iets dat hij zonder token niet al kon; dit is dezelfde afweging als de testmail-knop. Vastleggen in `images/e2e-allservices/README.md` (één alinea).
- De probe-spec verandert niet (geen nieuwe variabelen); `tests/test_probe_spec_drift.py` hoort ongewijzigd groen te blijven.
- Geen streaming en geen gespreksgeschiedenis; dit is een bewijsknop, geen chatclient.

## Verify

1. `go build` + `go test` van het image, inclusief handler-tests voor: geslaagd antwoord, 401 met duidelijke tokenmelding, onbereikbaar endpoint met duidelijke netwerkpad-melding, en de garantie dat het token niet in de respons-HTML of logregels belandt.
2. Sandbox-E2E: de uitgebreide test groen tegen de stub (sandbox claimen via `orch sandbox`).
3. `uv run ruff check`, `ruff format --check`, `pyright` schoon op de python-kant; `tests/test_probe_spec_drift.py` ongewijzigd groen.
4. Na publish + podherstart in de sandbox: handmatig één druk op de knop tegen de stub geeft het stub-antwoord op de pagina.

## Uitkomst

Alles gebouwd zoals beschreven. Twee afwijkingen, beide met reden:

- **De uitkomst komt in het antwoord op de POST, niet via een omleiding** zoals de
  testmail-knop. Een antwoord in een query-string belandt in elke access-log tussen pod en
  browser, en juist het antwoord mag nergens gelogd worden.
- **`POST /vlam-chat` geeft 404 als de vlam-dienst niet gebonden is.** Geen binding, geen
  knop, dus ook geen endpoint - dat is netter dan een foutmelding renderen in een blok dat
  dan toch niet getoond wordt.

Verify-punt 4 (publish + podherstart) is NIET uitgevoerd: `:latest` publiceren duwt
ongereviewde code naar een tag die alle sandboxsuites gebruiken. In plaats daarvan is het
image met `kind load` als `local/e2e-allservices:rc147` op de sandbox gezet en heeft de
suite daar echt tegen gedraaid (7 passed). Zie [[kind-load-via-de-local-prefix]].
