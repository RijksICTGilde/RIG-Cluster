# Open vragen uit de zad-cli-doorlopen

Vastgelegd 12 augustus 2026 door zad-cli, na vier volledige draaiboeken tegen
`https://zad.sandbox.rijksapp.dev/api`.

**Aan de lezer van dit bestand:** het zijn genummerde punten met per punt de reproductie en
wat wij al hebben uitgesloten. Antwoord er graag onder, per nummer, in het kopje "Antwoord"
dat er al staat. Alles is met `curl` te reproduceren; onze CLI is er alleen een client op.

**Stand van zaken.** Punt 1 tot en met 5 zijn beantwoord en opgelost; die staan hieronder
met hun antwoord, en onze reactie erop staat onderaan. **Punt 6 tot en met 13, aan het eind,
wachten nog op een antwoord; 12 is blokkerend en een regressie op `5c026ecc`.** Punt 10 en 11 zijn nagemeten op de releasebuild `5c026ecc`.

```sh
BASE=https://zad.sandbox.rijksapp.dev/api
KEY=<projectsleutel>
P=<projectnaam>
```

Twee eerdere bevindingen zijn al opgelost en staan hier niet meer: `DELETE` op een component
(kwam er op 11 augustus) en de 404 op `backup database|bucket|namespace`, die aan onze kant
bleek te liggen en door ons is verwijderd.

---

## 1. Een component met een ingress-pad anders dan `/` is onbereikbaar

De zwaarste van deze ronde, omdat alles er gezond uitziet.

Een component aangemaakt met `path: /api` krijgt een URL, de deployment wordt `Healthy`, de
pod draait en verifieert zijn diensten. Maar er is geen ingressregel die matcht:

```sh
for p in / /api /status /api/status; do
  curl -sS -o /dev/null -w "$p -> %{http_code}\n" "https://api-productie-$P.sandbox.rijksapp.dev$p"
done
# alle vier 404, en het is de nginx-404: er zit geen backend achter
```

Het pad zit ook niet op de host van het andere component: `https://web-…/api/status` geeft de
404 van de applicatie van `web`, dus die host stuurt `/api` niet door.

Isolerend experiment, en daarmee de oorzaak:

```sh
curl -X PATCH "$BASE/v2/projects/$P/components/api" -H "X-API-Key: $KEY" \
  -H 'Content-Type: application/json' -d '{"path": "/"}'
curl -X POST "$BASE/v2/projects/$P/:refresh" -H "X-API-Key: $KEY"

curl -s -o /dev/null -w "%{http_code}\n" "https://api-productie-$P.sandbox.rijksapp.dev/status"
# 200, binnen een minuut
```

Zelfde component, zelfde image, zelfde diensten. Alleen het pad terug naar `/`, en het werkt.

**Onze vraag:** is een niet-root `path` bedoeld om te werken? Zo ja, dan lijkt de ingressregel
niet gegenereerd te worden. Zo nee, dan zou het veld dat moeten zeggen bij het aanmaken, want
nu levert het een deployment op die gezond heet en niet bereikbaar is.

### Antwoord

Ja, een niet-root `path` is bedoeld om te werken, en de ingressregel wordt wél gegenereerd.
Wat ontbreekt is een herschrijving: het pad gaat **ongewijzigd** naar de container door. Dat
verklaart alle vier de 404's, en het is geen van beide dingen die jullie vermoedden.

Wat wij gemeten hebben, in drie stappen.

**1. De regel staat in git.** Jullie eigen run staat er nog. In `zad-deployments` zit
`sandboxed-local/p0-6lo/productie/api-ingress-api.yaml`, in dezelfde commit als de rest van
de deployment, en de kustomization noemt hem:

```
resources:
- api-deployment.yaml
- api-service.yaml
- api-ingress-api.yaml      <- de regel voor /api
...
```

De regel zelf:

```yaml
  rules:
    - host: "api-productie-p0-6lo.sandbox.rijksapp.dev"
      http:
        paths:
          - path: "/api"
            pathType: Prefix
```

(In `p0-ui9` is te volgen wat jullie daarna deden: dezelfde regel, en na de PATCH naar `/`
hernoemd naar `api-ingress.yaml` met `path: "/"`. De opruiming werkte dus ook.)

**2. Die regel werkt in dit cluster.** Wij hebben exact dat bestand — uit jullie commit, alleen
de hostnaam vervangen — op de sandbox toegepast, met een backend erachter:

```
rc77api/      -> 404      (buiten het prefix: nginx heeft hier geen regel)
rc77api/api   -> 200      (binnen het prefix: de backend antwoordt)
rc77api/status-> 404
rc77web/      -> 200      (dezelfde opzet met path "/")
```

**3. En dit is de kern:** de backend kreeg het verzoek als `/api/echo`, niet als `/echo`. Zonder
`rewrite` stuurt nginx het volledige pad door. Voor een applicatie die haar routes op de wortel
aanbiedt (`/`, `/status` — zoals het verificatie-image doet) betekent `path: /api` dus:

| verzoek | wat er gebeurt | code |
|---|---|---|
| `/api` | bereikt de container als `/api`; de app kent die route niet | 404 **van de app** |
| `/api/status` | bereikt de container als `/api/status`; idem | 404 van de app |
| `/status` | valt buiten het prefix, geen regel | 404 van nginx |
| `/` | valt buiten het prefix, geen regel | 404 van nginx |

Vier keer 404, en met alleen statuscodes (`-o /dev/null`) is de app-404 niet van de nginx-404 te
onderscheiden. Vandaar de conclusie "er zit geen backend achter"; die klopte voor twee van de vier.

**Wat wij hieraan doen.** Het veld zegt het nu bij het aanmaken. De beschrijving van `path` in de
API luidt voortaan dat een niet-root pad ongewijzigd wordt doorgestuurd, met het `/api/status`-voorbeeld
erbij, plus: gebruik `/` tenzij de applicatie het prefix zelf serveert. Vastgelegd in
`tests/test_multi_path_ingress.py::TestNonRootPathIngressRule`.

**Wat er nog wel ontbreekt** (en wat wij niet in deze PR hebben opgelost, omdat het een nieuw veld
in de API is): het projectbestand kent per pad een `rewrite`, en de ingress-sjabloon gebruikt die
ook — `path: [{match: /api, rewrite: /}]` levert `rewrite "^/api/?(.*)$" "/$1" break;` op, precies
wat jullie nodig hebben. De component-API accepteert `path` alleen als losse string en zet er
`[{"match": path}]` van, dus via de API is `rewrite` niet te zetten. Zeg het als jullie dat nodig
hebben; het is een klein veld erbij.

---

## 2. Een nieuw project geeft een sleutel terug die nog niet werkt, en er is niets om op te wachten

```sh
curl -X POST "$BASE/v2/projects" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"displayName":"Race"}'
# 202 {"project_name":"r0-abc","api_key":"…","task_id":"…","poll_url":"/api/tasks/…"}

curl -H "X-API-Key: <die sleutel>" "$BASE/v2/projects/r0-abc/components"
# 401 Authentication required
```

Even later werkt dezelfde sleutel wel. Het wachten is het probleem niet; het ontbreken van
een signaal is het:

- `poll_url` met het bearer-token: `401 provide X-API-Key header`
- `poll_url` met de zojuist ontvangen sleutel: ook 401, want die wordt pas geaccepteerd als
  het project bestaat

Een client kan dus niet zien wanneer het project klaar is. Wij hebben dit bewust **niet** in
de CLI opgelost: elke wachtlus zou een gok zijn, en stil doorgaan op een 401 zou een echte
authenticatiefout maskeren. Onze draaiboeken hebben nu een expliciete lus met uitleg erbij.

**Onze vraag:** kan `poll_url` bereikbaar worden met het bearer-token waarmee het project is
aangemaakt? Of kan de 202 pas komen als de sleutel bruikbaar is? Eén van beide is genoeg.

### Antwoord

De eerste van de twee: **`poll_url` accepteert nu het bearer-token waarmee het project is
aangemaakt.** Gerepareerd in deze PR.

Jullie diagnose klopte precies. `GET /api/tasks/{id}` zocht het project op in de projectstore en
vergeleek daar de `X-API-Key` mee. Voor elke andere taak is dat de juiste poort, maar de taak die
een project aanmaakt is de enige waarvan het project nog niet bestaat — dus de sleutel die met de
202 meekomt kan de taak die hem geldig maakt niet volgen. Een gat, geen bedoeling.

Wat er nu gebeurt, in volgorde:

1. `X-API-Key` die bij het project hoort: toegang (ongewijzigd).
2. Anders: een geldig `Authorization: Bearer`-token waarvan het e-mailadres gelijk is aan de
   `created_by` van de taak — de persoon die de taak startte. De taak weet dat al; wij vergelijken
   het alleen.

Dat werkt ook als jullie de verse sleutel wél meesturen (clients doen dat, hij komt immers met de
202 mee): als die sleutel nog niet geaccepteerd wordt, valt hij terug op het token.

Wat dit niet is: een tweede sleutel voor alles. Een geldig token zegt wie je bent, niet dat de taak
van jou is. Een taak zonder `created_by` is met geen enkel token te openen, en het token van iemand
anders krijgt 401. Vastgelegd in `tests/test_task_router.py::TestGetTaskWithBearerToken`.

Jullie wachtlus kan dus weg: pol `poll_url` met hetzelfde token als waarmee je het project aanmaakte,
tot 200. Daarna is de sleutel bruikbaar.

---

## 3. `:validate-clone` valt om op een ontbrekend attribuut

```sh
curl -H "X-API-Key: $KEY" "$BASE/v2/projects/$P/deployments/productie/:validate-clone"
# Error validating clone configuration:
#   'ProjectManager' object has no attribute '_clone_manager'
```

Dat leest als een interne fout, niet als een configuratiefout van de gebruiker.

Gevolg voor ons: het schrijvende klonpad is niet te beproeven. Zonder een geldige controle
vooraf is een echte kloon niet verantwoord te draaien, dus `clone database` en `clone bucket`
staan in ons draaiboek als niet-getest.

**Onze vraag:** is dit een regressie, of is `_clone_manager` verplaatst? Wat de CLI verstuurt
klopt volgens de spec; we hebben het verzoek nagekeken.

### Antwoord

Een regressie, en een oude. Gerepareerd in deze PR.

`_clone_manager` is niet verplaatst maar verdwenen: `opi/manager/clone_manager.py` is op
8 december 2025 verwijderd, en het endpoint bleef achter met een aanroep naar een attribuut dat
sindsdien niet meer bestaat. Elke aanroep liep dus op een `AttributeError` die als 500 met die
tekst naar buiten kwam. Jullie verzoek was inderdaad in orde.

Waarom niemand het zag: de twee tests op dit endpoint zetten `mock_pm._clone_manager` op een mock.
Ze toetsten daarmee alleen de statuscodes eromheen, en bleven groen terwijl het echte pad al maanden
stuk was. Die tests draaien nu tegen de echte controle, met een projectbestand als invoer.

De controle is opnieuw geschreven als een pure functie over het projectbestand
(`opi/manager/clone_validation.py`) en kijkt naar wat een kloon nodig heeft om te *kunnen*:

- bestaat de deployment, en heeft die een `clone-from`;
- `type: deployment` — bestaat de bron-deployment in dit project (en is het niet zichzelf);
- `type: remote-source` — bestaat de remote-source, heeft die een chisel `server-url` en diensten;
- `type: backup` — zijn er `backup_items`, elk met een bekend `resource_type` en een `snapshot_id`;
- plus een melding als een `mode: once`-kloon al gedraaid heeft (geldige toestand, geen fout: een
  nieuwe kloon vraagt dan om force-clone).

Antwoordvorm ongewijzigd: 200 met `status: valid`, of 422 met `status: invalid` en per controle een
regel met naam, status en reden. Bereikbaarheid van een externe host toetsen we bewust niet — dat
vraagt een tunnel en credentials, en dan is het geen droge controle meer.

Het schrijvende klonpad is hiermee weer vooraf te beproeven.

---

## 4. `restore database` en `restore bucket` geven 422

```sh
curl -X POST -H "X-API-Key: $KEY" \
  "$BASE/v1/restore/database/sandboxed-local/rig-$P/backup?project_name=$P"        # 422
curl -X POST -H "X-API-Key: $KEY" \
  "$BASE/v1/restore/bucket/sandboxed-local/rig-$P/bucket-backup?project_name=$P"   # 422
```

De snapshots bestaan wel:

```sh
curl -H "X-API-Key: $KEY" "$BASE/v1/restore/snapshots/sandboxed-local/rig-$P?project_name=$P"
# [{"snapshot_id":"41bbdb…","pvc_name":"bucket-backup","timestamp":"…"}]
```

Wat wij hebben uitgesloten: de verplichte `project_name`-queryparameter gaat wel degelijk
mee. We hebben de client daarop nagekeken en de spec zegt `required: true`; dat klopt dus.

**Eén spoor.** Er zijn drie namen in omloop die mogelijk hetzelfde bedoelen:

| Waar | Veld | Waarde in ons geval |
|---|---|---|
| `GET /v1/backup/runs/{p}/{d}` | `reference_name` | `backup`, `bucket-backup` |
| `GET /v1/restore/snapshots/{cluster}/{ns}` | `pvc_name` | `bucket-backup` |
| pad van de restore-endpoints | `{reference_name}` | wat wij invullen |

Als die drie niet hetzelfde zijn, verklaart dat waarom een naam uit het ene antwoord niet
past in het pad van het andere.

**Onze vraag:** wat verwacht `{reference_name}` precies, en is dat te halen uit een van de
twee lijstendpoints? Het antwoordlichaam van de 422 hebben wij niet vastgelegd; als daar de
veldnaam in staat, is dat waarschijnlijk al genoeg.

### Antwoord

Jullie spoor is het niet: de drie namen zijn wél dezelfde. De 422 gaat over het **verzoeklichaam**,
niet over het pad — beide endpoints eisen een JSON-body en jullie curl stuurt er geen.

Wat er in de 422 staat die jullie niet hebben vastgelegd: `{"detail":[{"type":"missing","loc":["body"],...}]}`.
Dat is FastAPI's body-validatie; het pad is op dat moment nog niet eens gebruikt.

Verplicht in de body van `restore/database`:

```sh
curl -X POST -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  "$BASE/v1/restore/database/sandboxed-local/rig-$P/backup?project_name=$P" \
  -d '{"target_database_host":"...","target_database_name":"...",
       "target_database_user":"...","target_database_password":"...",
       "snapshot_id":"<optioneel; standaard de nieuwste>"}'
```

en van `restore/bucket`: `target_minio_endpoint`, `target_bucket_name`, `target_access_key`,
`target_secret_key`. De reden dat die er zijn: een restore schrijft ergens *naartoe*, en die
bestemming staat niet in het pad. Vastgelegd in `tests/test_restore_request_body.py`.

Over de drie namen, want de vraag is terecht:

| Waar | Veld | Betekenis |
|---|---|---|
| `GET /v1/backup/runs/{p}/{d}` | `reference_name` | de logische naam van de geback-upte bron |
| `GET /v1/restore/snapshots/{cluster}/{ns}` | `pvc_name` | **dezelfde waarde**; het veld heet historisch zo omdat de eerste back-ups PVC's waren, en draagt nu ook database- en bucketnamen |
| pad van de restore-endpoints | `{reference_name}` | diezelfde waarde |

`bucket-backup` uit jullie snapshotlijst is dus de juiste padwaarde. Beide bronnen zijn goed; de
antwoordvelden in de spec zeggen dat nu ook (docstrings van beide endpoints aangevuld).

---

## 5. Ter overweging: `/version` liep tweemaal achter op wat er draaide

Geen bug, wel iets dat ons tweemaal op het verkeerde been zette.

Op 11 augustus gaf `/version` `2d04342f` van de avond ervoor, terwijl `/openapi.json` op dat
moment al drie nieuwe `…/values/…`-GET-endpoints bevatte die in die build niet zaten.
Hetzelfde gebeurde eerder bij de leesendpoints uit PR #60.

Wij gebruiken `/version` om te bepalen of een testronde zinvol is: draait mijn wijziging al?
Dat werkt zo niet, en we zijn een keer begonnen tegen een build waarvan we dachten dat hij
nieuwer was.

**Onze vraag:** is er een betrouwbaardere manier om te zien wat er draait? Wij vergelijken nu
op de aanwezigheid van een verwacht pad in `/openapi.json`, wat werkt maar omslachtig is.

### Antwoord

De waarneming klopt, en de oorzaak zit niet in de bron van het versienummer maar in **wie er
antwoordt**. Tijdens een rollout draaien er twee pods achter dezelfde Service (`maxSurge: 1`,
`maxUnavailable: 0`). De load balancer kiest er per verzoek een, dus twee opeenvolgende `/version`-calls
kunnen twee verschillende commits melden, en de oudste is de pod die nog verkeer bedient. Met alleen
een commit in het antwoord is dat niet te onderscheiden van "de build is misgegaan" — precies de
verkeerde conclusie die dit hier al twee keer heeft veroorzaakt.

Wat wij niet doen: de volgorde van de bronnen omgooien. Die is in orde en is bewust zo:
`opi/version.json` (door de bouwtaken vóór elke build weggeschreven, en tijdens skaffold live
meegesynct) gaat vóór de ingebakken `ZAD_*`-omgevingsvariabelen.

Wat wij wél doen: `/version` zegt nu wie er antwoordt.

```json
{
  "name": "ZAD",
  "version": "8373c72e",
  "commit": "8373c72e98bbc2b593cef9bf4a9fcd098dd05ec7",
  "branch": "vier-open-vragen-uit-de-zad-cli-doorlopen",
  "build_date": "2026-08-12T05:33:55Z",
  "dirty": false,
  "pod": "operations-manager-64884cd948-ngwjz",
  "image": "operations-manager:rc-77"
}
```

- `pod` komt uit de downward API (`POD_NAME`). **Twee verschillende podnamen bij twee calls = er
  loopt een rollout**; dan is wachten het juiste antwoord, niet opnieuw bouwen.
- `image` is wat de kubelet daadwerkelijk gestart heeft, één keer bij het opstarten opgevraagd bij
  het cluster (een pod wisselt niet van image, dus vaker vragen levert hetzelfde op). Dat is de
  enige waarheid over welke code draait; `commit` is een afgeleide van de build.
- Buiten Kubernetes blijven beide velden leeg — leeg is eerlijker dan geraden.
- `dirty: true` (en een image-tag die op `-dirty` eindigt) betekent dat er ongecommitte
  wijzigingen in de build zaten. Dan zegt `commit` niet welke code draait, en is `image` het
  enige waarop je je kunt baseren.

Jullie omweg via `/openapi.json` kan daarmee weg. Wat wij zelf als regel aanhouden: eerst kijken of
de podnaam over twee calls gelijk blijft, en dan pas het commit vergelijken.

---

# Terug van zad-cli, 12 augustus

Dank, dit is per punt te gebruiken. Hieronder het antwoord op de vraag die jullie bij punt 1
terugstelden, en per punt wat wij aan onze kant doen.

## Op de vraag bij punt 1: ja, we willen `rewrite`

Graag, en het is bij ons meer dan een gemak. Onze CLI heeft `zad component add --path /api`, en
iedereen die dat intikt bedoelt hetzelfde: dit component hangt extern onder `/api`, en de
applicatie erin luistert op `/`. Dat is de standaardvorm van een image die je niet zelf schrijft;
onze eigen testimage is er een. Zonder herschrijving is `--path` daarvoor onbruikbaar, en dat is
precies hoe wij erin liepen.

**Wat wij nodig hebben is het kleine veld.** Een `rewrite` naast `path`, allebei losse strings, en
jullie maken er `[{"match": path, "rewrite": rewrite}]` van zoals je nu `[{"match": path}]` maakt.
Meer niet.

Twee dingen die wij er expliciet **niet** bij vragen:

- **Geen standaardwaarde.** Laat `rewrite` weg betekenen: pad gaat ongewijzigd door, zoals nu.
  Een impliciete `/` zou het gedrag van bestaande componenten veranderen, en dat is voor een
  component dat zijn eigen prefix afhandelt precies verkeerd. Wij zetten het veld alleen als de
  gebruiker het intikt.
- **Nog geen samengestelde vorm.** `path` als lijst van objecten, met meerdere paden per
  component, is een grotere wijziging en wij hebben er nu geen gebruiker voor. Als jullie het
  toch die kant op willen, prima, maar wacht daar dit veld niet op.

Wat wij doen zodra het er is: `--rewrite` erbij op `component add` en `component update`, met in de
hulptekst het verschil in één zin. Tot die tijd zegt onze hulptekst bij `--path` dat het pad
ongewijzigd bij de container aankomt, zodat niemand er nog in loopt.

## Punt 4 was van ons

Kort, zodat het genoteerd staat: onze client stuurde bij `restore database` en `restore bucket`
helemaal geen body. Het stond gewoon in de spec die wij zelf gevendord hebben, `DatabaseRestoreRequest`
met vier verplichte velden, en wij zijn er met een verkeerd spoor (`pvc_name` versus `reference_name`)
langsheen gekeken. Excuus voor de verkeerde afslag; dat had ons eigen huiswerk moeten zijn. Wij
repareren het.

Dat het bij ons niet opviel heeft dezelfde vorm als jullie mock bij punt 3: onze dekkingscontrole
vergelijkt welke **paden** de client aanroept met de spec, en niet wat hij in het lichaam stuurt.
Een aanroep zonder verplichte body ziet er in die controle uit als volledige dekking. We nemen mee
of dat mee te controleren valt.

## Wat wij verder oppakken

| Punt | Bij ons |
|---|---|
| 1 | Hulptekst van `--path` scherpstellen; `--rewrite` zodra het veld er is. Onze conclusie "er zit geen backend achter" was voor twee van de vier URL's fout, en wij hadden dat aan de 404-body kunnen zien |
| 2 | De expliciete wachtlus uit de draaiboeken, en `project create` wacht nu zelf op `poll_url` met het bearer-token |
| 3 | Klonen staat bij ons als niet-getest; wij draaien dat draaiboek opnieuw zodra jullie PR erop staat |
| 5 | `zad version` toont `pod` en `image`, en onze controle "draait mijn wijziging al" kijkt eerst of de podnaam over twee calls gelijk blijft |

---

# Twee nieuwe vragen, 12 augustus (na een volledige doorloop)

Playbook 01 liep vandaag voor het eerst van begin tot eind door, tegen `edbda374`. Bevinding
1, 5, 6 en 7 uit onze vorige ronde zijn daarmee nagemeten en weg: `/api/status` geeft 200 en
`/status` geeft 404 op hetzelfde component, aliaswaarden zijn weer leesbaar, een alias naar
een onbestaande variabele faalt, en `env list`/`alias list` bestaan. Dank.

Er kwam ook één bug bij ons uit, voor de volledigheid: onze poll-URL verdubbelde het
`/api`-voorvoegsel (`/api/api/tasks/<id>`), wat pas opviel toen `project create` op jullie
`poll_url` ging wachten. Gerepareerd.

Twee dingen die aan jullie kant liggen.

---

## 6. Waarom is een net aangemaakt project asynchroon?

Dit is een ontwerpvraag, geen storing. Wij wachten nu netjes, dus er is niets kapot; de
vraag is of dat wachten er hoort te zijn.

```sh
curl -X POST "$BASE/v2/projects" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"display_name":"Race","description":"x"}'
# 202 {"project_name":"p1-nz2","api_key":"…","task_id":"…","poll_url":"/api/tasks/…"}

curl -H "X-API-Key: <die sleutel>" "$BASE/v2/projects/p1-nz2/components"
# 401 Invalid API key   (ongeveer 3,5 seconden lang)
```

Wat wij in jullie code lezen, en graag bevestigd of gecorrigeerd zien:

- De sleutel wordt **synchroon** gemaakt: `generate_base_project_file()` geeft
  `(project_dict, api_key)` terug vóór de taak bestaat.
- Authenticatie kijkt naar het *record*, niet naar de sleutel:
  `get_project_store().get(project_name)` en dan `compare_digest`. Zolang de store het
  project niet kent is elke sleutel ongeldig, vandaar 401 en niet 403.
- De taak die dat record aanmaakt raakt **het cluster niet**. Uit jullie eigen payload:
  `"rollout": False`, met de opmerking *"There is nothing to roll out: the project declares
  no deployments."*

Als dat klopt, dan is die 202 een git-commit plus een store-reconcile, en niet iets traags.
De asynchronie lijkt er dan te zijn omdat het schrijven door dezelfde taakmachinerie loopt
als elke andere mutatie, niet omdat er iets te provisioneren valt.

**Onze vraag:** kan dit ene endpoint synchroon zijn en `201` teruggeven? Dan is de sleutel
die je krijgt meteen bruikbaar, wat is wat iedereen verwacht van een antwoord dat een
credential bevat. Kan dat niet, dan is de andere uitweg dat de store het net gemunte project
al kent, zodat de sleutel werkt terwijl de commit nog loopt.

Wat wij hoe dan ook houden: `project create` wacht. Is het straks synchroon, dan kost dat
wachten niets, en het blijft correct tegen oudere builds.

### Antwoord

Jullie lezing klopt op alle drie de punten, en het antwoord op de vraag is: **het blijft
asynchroon, en die 401 blijft ook.** Hieronder per punt, en dan waarom.

**1. De sleutel wordt synchroon gemaakt. Klopt.** `POST /v2/projects` roept
`generate_base_project_file()` aan, krijgt `(project_dict, api_key)` terug en maakt daarna pas
de taak; de sleutel in de 202 is dus ouder dan de taak. Hij komt uit dezelfde bouwer als de
wizard, dus het is geen aparte sleutel voor de API.

**2. Authenticatie kijkt naar het record, niet naar de sleutel. Klopt, en dat is precies de
formulering.** `validate_api_token` doet `get_project_store().get(project_name)` en pas als dat
iets oplevert een `compare_digest`. Kent de store het project niet, dan is er niets om mee te
vergelijken en valt hij op dezelfde regel om als een verkeerde sleutel: `401 Invalid API key`.
De sleutel is niet fout; er is nog geen project.

**3. De taak raakt het cluster niet. Klopt.** `"rollout": False`, met die opmerking er
letterlijk bij. Wat de taak wel doet is niet niks: een `reconcile()` op de projecten-repo (een
`ls-remote`), de controle of die projectnaam al bestaat, en dan `save_and_commit_project` —
schemavalidatie, commit, push, cache verversen. Dat zijn twee netwerkrondes naar git, en daar
zit jullie 3,5 seconde in.

**Waarom het asynchroon blijft.** Niet omdat er iets te provisioneren valt — dat klopte al in
jullie lezing — maar omdat dit een schrijfactie naar git is, en die zijn hier allemaal een taak.
Dat levert twee dingen op die een synchrone 201 niet heeft: er is een record van de afloop dat
je kunt opvragen nadat je verbinding is weggevallen, en het is dezelfde vorm als elke andere
mutatie, dus jullie client heeft één weg in plaats van twee. Een `POST` die inline naar git
commit is bovendien de enige plek waar een trage of even onbereikbare git-remote een verzoek
van onbepaalde duur oplevert; nu is dat de taak, en die kun je bekijken. Eerlijkheidshalve:
`create_project` draait met `max_attempts=1`, dus opnieuw proberen doen wij hier *niet* — de
winst is de uniforme weg en het zichtbare resultaat, niet een retry.

**De andere uitweg die jullie noemen — de store het project alvast laten kennen — willen wij
niet.** De store is een spiegel van de projecten-repo, en de controle "bestaat deze naam al"
gebeurt daar, in de taak, na de reconcile. Een record vooruitschrijven betekent een sleutel die
werkt voor een project waarvan het bestand misschien nooit landt (schemafout, dubbele naam,
push geweigerd), en dan is de fout stiller en later dan nu. Een sleutel die 3,5 seconde te
vroeg is, is beter dan een project dat niet bestaat maar wel antwoordt.

**Blijft die 401?** Ja, en wel om deze reden. Wij zijn het met jullie eens dat "nog niet,
probeer zo weer" iets anders is dan "je sleutel klopt niet" — maar dat onderscheid hoort niet
op dit endpoint. Wie alleen een `X-API-Key` meestuurt is voor ons niemand in het bijzonder; een
ander antwoord voor "deze naam bestaat nog niet" dan voor "deze sleutel hoort niet bij deze
naam" maakt van elk projectendpoint een manier om projectnamen af te tasten. Dat signaal
bestaat wél, en het is preciezer dan een statuscode ooit kan zijn: de taak zelf. Sinds punt 2
is `poll_url` te volgen met hetzelfde bearer-token waarmee je het project aanmaakte, en die
zegt niet alleen "nog niet", maar ook of het misging en waaraan. Pol tot 200; daarna is de
sleutel bruikbaar. De 202 met `Location` zegt hetzelfde in het antwoord zelf.

Kortom: jullie `project create` die wacht, is hier niet een tijdelijke oplossing maar de
bedoelde weg. Hij blijft goed werken en er komt niets aan dat hem overbodig maakt.

---

## 7. Terugzetten in je eigen database vraagt om een wachtwoord dat je niet hebt

Dit volgt uit punt 4, dat aan onze kant lag: wij stuurden geen verzoeklichaam. Dat is
gerepareerd, en daarmee werd zichtbaar wat er daarna komt.

`DatabaseRestoreRequest` vereist vier velden, en `BucketRestoreRequest` ook:

```
target_database_host, target_database_name, target_database_user, target_database_password
target_minio_endpoint, target_bucket_name, target_access_key, target_secret_key
```

Voor een externe bestemming is dat precies goed. Maar de gewone handeling is terugzetten in
de database van je eigen project, en **die credentials beheert het platform**. Ze worden in
de container geïnjecteerd; de gebruiker ziet ze nergens. Wij hebben geen commando dat ze
teruggeeft, en in de spec staat geen endpoint dat ze teruggeeft.

Wat wij hebben nagekeken: de dienstconfiguratie van `postgresql-database` bevat ze niet,
`project describe` evenmin, en `env list` geeft de namen van de variabelen van de gebruiker,
niet de geïnjecteerde platformwaarden.

Gevolg: `zad restore database` en `zad restore bucket` zijn wel te *bouwen* maar niet te
*draaien* voor het gewone geval. In ons draaiboek staat de stap nu als niet-uitvoerbaar, met
deze reden erbij.

**Onze vraag:** wat is de bedoelde weg om in de eigen projectdatabase terug te zetten? Drie
vormen die wij ons kunnen voorstellen, en wij hebben geen voorkeur zolang er één is:

1. De doelvelden **optioneel** maken, en bij afwezigheid terugzetten in de database van het
   project waar de sleutel bij hoort. Dat is ook het veiligst: geen credentials over de lijn.
2. Een endpoint dat de verbindingsgegevens van de eigen dienst teruggeeft, zodat een client
   ze kan doorgeven.
3. Een aparte "restore in place" naast de bestaande, als het mengen van die twee gevallen in
   één endpoint onwenselijk is.

Ter overweging bij 2: dat maakt van een wachtwoord dat nu alleen in de pod staat iets dat
over de API opvraagbaar is. Dat is een echte verruiming, en wij vragen er niet om als 1 kan.

### Antwoord

**Optie 1, om precies de reden die jullie zelf noemen: er gaan geen credentials over de
lijn.** De vier doelvelden zijn optioneel geworden. Laat je ze alle vier weg, dan zet het
platform terug in de dienst van het project waar de API-sleutel bij hoort. Dat geldt voor
database en bucket, met dezelfde regels.

```sh
# terugzetten in je eigen database: leeg lichaam volstaat
curl -X POST "$BASE/v1/restore/database/$CLUSTER/$NAMESPACE/$REFERENCE?project_name=$PROJECT" \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' -d '{}'

# hetzelfde voor een bucket
curl -X POST "$BASE/v1/restore/bucket/$CLUSTER/$NAMESPACE/$REFERENCE?project_name=$PROJECT" \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' -d '{}'
```

Een verzoek zonder lichaam werkt ook; `{}` en "geen lichaam" betekenen hetzelfde. Wil je een
snapshot kiezen, dan mag `{"snapshot_id": "..."}` erbij zonder dat er een doel bij hoeft.

Wat er verder geldt:

- **Een volledig ingevuld doel doet exact wat het altijd deed.** Bestaande aanroepen naar een
  externe bestemming veranderen niet van gedrag.
- **Half ingevuld is een fout, geen gok.** Geef je drie van de vier velden, dan volgt een 422
  die zegt welke velden ontbreken. Wij vullen het vierde niet aan: dan zou je terugzetten op
  een plek waar je niet om vroeg. De melding noemt alleen veldnamen, nooit de waarden die je
  meestuurde.
- **De verwijzing bepaalt de deployment.** `reference_name` is de naam waaronder de backup
  geregistreerd staat -- de servicereferentie van het component (`{deployment}-postgresql`,
  `{deployment}-minio`) of de deployment-brede terugval (`{deployment}-database`). Uit die
  naam volgt de deployment, en daaruit het secret in je eigen namespace.
- **Geen dienst, geen stacktrace.** Kent geen enkele deployment die verwijzing, of is de
  database of bucket nog niet uitgerold, dan is het antwoord een 404 die zegt wat er mist.

Optie 2 hebben wij niet gedaan, om de reden die jullie er zelf bij zetten: het zou van een
wachtwoord dat nu alleen in de pod staat iets maken dat met een gestolen sleutel op te halen
is. Optie 3 evenmin: twee endpoints die op een haar na hetzelfde doen lopen uit de pas zodra
er iets aan verandert.

**En de vraag die daaronder zit: mag een sleutel terugzetten in de dienst van een ander
project?** Gemeten, en het antwoord is genuanceerd:

- De **bron** was en blijft dichtgezet. De namespace in het pad moet die van het
  geauthenticeerde project zijn, anders volgt een 403; je kunt dus alleen je eigen backups
  lezen.
- Het **doel** was en blijft vrij als je het expliciet opgeeft. Er is geen controle dat de
  opgegeven host bij jou hoort. Dat is geen rechtenverruiming: je moet de gebruiker en het
  wachtwoord van die database al kennen om er iets in te mogen schrijven, en de restore-pod
  draait in je eigen namespace, dus onder je eigen NetworkPolicy. Wat je ermee kunt, kun je
  met een `psql` in je eigen pod net zo goed.
- De **nieuwe weg is strikt smaller**: zonder doelvelden komt het platform nooit ergens
  anders uit dan bij de dienst van het project bij de sleutel.

Wij hebben het expliciete pad daarom gelaten zoals het was. Zou je daar een eigenaarscontrole
op zetten, dan verdwijnt het legitieme geval -- terugzetten in een database buiten ZAD --
zonder dat er iets dichtgaat wat nu openstaat.

---

## 8. Twee refreshes over elkaar heen: samengevoegd, en is dat veilig?

Playbook 04 doet dit sinds vandaag bewust, en het gedrag verraste ons positief. De vraag is
of we erop mogen bouwen.

```sh
TA=$(zad --no-wait project refresh -o json | jq -r .task_id)
# ... terwijl TA loopt, een wijziging opslaan zonder uitrol ...
TB=$(zad --no-wait project refresh -o json | jq -r .task_id)
test "$TA" = "$TB"     # klopt: hetzelfde task_id
```

De tweede refresh start dus geen tweede taak en breekt de eerste niet af; hij levert de
lopende op. Wat wij vervolgens maten: de wijziging die **na** de start van TA werd opgeslagen
(een component toevoegen en koppelen, met `--no-rollout`) was na afloop wel degelijk
uitgerold. Het component had een adres en antwoordde 200, en `project pending` stond op 0.

**Onze vraag:** is dat gegarandeerd, of hadden wij geluk met de timing? Concreet: leest de
lopende taak het projectbestand opnieuw, of bestaat er een venster waarin een wijziging die
net te laat komt stilzwijgend buiten die refresh valt terwijl `pending` op 0 gaat? Van
buitenaf is dat verschil niet te zien, en dat is precies het soort fout dat pas opvalt als
iemand zich afvraagt waarom zijn wijziging niet live is.

Is het gegarandeerd, dan is dit een prettige eigenschap die wij graag documenteren. Is het
dat niet, dan willen wij weten waar het venster zit.

### Antwoord

**Het samenvoegen is echt en deterministisch. Dat een late wijziging meelift is dat
niet -- daar hadden jullie geluk mee.** De lopende taak leest het projectbestand
precies één keer, aan het begin van zijn eigen run. Er was ook een venster waarin
`pending` daarover loog; dat deel hebben wij dichtgezet.

**1. Waarom jullie hetzelfde `task_id` terugkregen.** Dat is geen eigenschap van
refresh maar de algemene ontdubbeling op de taakwachtrij. Bij het aanmaken wordt
gezocht naar een taak met dezelfde `project_name`, dezelfde `deployment_name`,
hetzelfde type en de status `pending`, `claimed` of `running`. Is die er én is het
verzoeklichaam identiek, dan krijg je die taak terug. Voorwaarden om op te bouwen:

- Het geldt ook als de eerste nog in de wachtrij staat: het is "open", niet "running".
- Het geldt per project. Een refresh op een ander project raakt hem niet.
- **Een afwijkend lichaam voegt niet samen.** `force_clone=true` naast een lopende
  `force_clone=false` is een ánder lichaam en zet een nieuwe taak achter de lopende.
  Wil je zeker weten dat je meelift, stuur dan exact dezelfde parameters.
- Na afloop van de eerste levert een volgende refresh weer een nieuwe taak op.

**2. Wanneer hij het projectbestand leest: één keer, aan het begin.** Gemeten aan de
taakafhandeling zelf. De refresh doet eerst een `reconcile()` op de projectopslag
(dat is de fetch uit git), dan één opzoeking, en geeft daarna het pad door aan de
verwerking. Die verwerking leest het YAML één keer, in de stap "Projectbestand
ophalen en controleren", en werkt de rest van de run met díe momentopname. Daarna
leest niets meer opnieuw -- niet per deployment, niet na de ArgoCD-wacht.

Het venster is dus: **vanaf die lezing tot het einde van de taak.** Dat is in de
praktijk vrijwel de hele looptijd van de refresh, en die wordt gedomineerd door het
wachten op ArgoCD. Reken op minuten, niet op milliseconden. Het is dus een ruim
venster, geen randgeval.

**3. Wat er in jullie run gebeurde.** Je wijziging is niet door TA opgepikt; hij is
uitgerold door zijn *eigen* taak. Elke schrijfhandeling is zelf een taak, en die
verwerkt het bestand dat hij op dat moment aantreft -- inclusief alles wat TA net
had gedaan. Het resultaat is hetzelfde (component live, `pending` op 0), maar de
oorzaak is een andere, en dat is precies het verschil tussen "gegarandeerd" en
"toevallig goed afgelopen".

Twee dingen die daarbij helpen om het van buitenaf te zien:

- Een refresh die je aanroept ná het opslaan bevat je wijziging altijd: hij leest bij
  zijn start, en je schrijftaak was toen al afgerond of staat vóór hem in de rij.
- Krijg je een `task_id` terug dat je al eerder had gezien, dan lift je mee op een
  refresh die vóór je wijziging begon. Dat is het signaal dat je er nog één achteraan
  moet doen. Wacht die eerste af en roep dan opnieuw aan: dat levert een nieuw id.

**4. Waar `pending` wél loog, en wat wij eraan gedaan hebben.** Het gevaarlijke deel
dat jullie benoemden bestond. De drift werd gemeten vanaf het moment waarop de laatste
uitrollende taak **klaar** was. Een wijziging met `rollout=false` die tijdens een
lopende refresh werd opgeslagen, is eerder klaar dan die refresh, en werd dus
weggestreept door een refresh die hem nooit gelezen had. `pending` stond dan op 0
terwijl de wijziging niet op het cluster stond, en van buitenaf was dat niet te zien.

De grens is nu het moment waarop de uitrollende taak **begon**. Dat is een veilige
ondergrens voor het moment van lezen: de taak wordt op running gezet vóór hij leest.
Deze richting kan alleen méér melden dan er openstaat, nooit minder.

Wat er níet onder valt, zodat je weet wanneer dit speelt: taken zonder deployment in
hun sleutel (`add_component`, `update_component`, `add_service`, `configure_service`)
worden door de wachtrij achter een projectbrede refresh gezet en kunnen dus nooit
binnen het venster landen. Taken mét een deployment (`update_image`,
`upsert_deployment`) lopen wél gelijktijdig met een projectbrede refresh, en die
waren het die verdwenen.

**5. Wat wij bewust niet gedaan hebben.** Een refresh die na afloop nog eens kijkt of
er intussen iets veranderd is, hebben wij niet ingebouwd. Dat verdubbelt in het
slechtste geval de ArgoCD-wacht voor een geval dat je met één extra aanroep afhandelt,
en het maakt de looptijd van een refresh onvoorspelbaar. De eigenschap waar je op wilt
bouwen is er nu ook zonder: **roep refresh aan ná het opslaan, en controleer dat je een
nieuw `task_id` terugkrijgt.** Dan is de dekking gegarandeerd in plaats van waarschijnlijk.

Alles hierboven staat vastgelegd in `tests/test_refresh_merge_window.py`, inclusief de
meting van het venster zelf, zodat dit antwoord waar blijft als de code verandert.

---

## 9. Een restore naar een onbereikbare doelhost is een 500 zonder categorie

Klein, maar het raakt CI/CD.

```sh
curl -X POST -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"target_database_host":"doel.invalid","target_database_name":"d",
       "target_database_user":"u","target_database_password":"g"}' \
  "$BASE/v1/restore/database/sandboxed-local/rig-$P/backup?project_name=$P"
# HTTP 500
# {"status":"failed","message":"Failed to restore database backup: Restore pod failed.
#   Logs: ... psql: error: could not translate host name \"doel.invalid\" to address ..."}
```

De pod-logs zijn uitstekend: daar staat precies wat er misging. Maar de statuscode is 500 en
er zit geen `ErrorCategory` bij, terwijl de oorzaak de invoer van de aanroeper is: een
hostnaam die niet resolvet.

Bij ons betekent dat exit code 2, "platform, probeer later opnieuw". Voor een pijplijn is dat
het verkeerde signaal: die blijft een typefout in `--target-host` opnieuw proberen. Wij
kennen die code toe op de statuscode, en wij gaan niet raden op de tekst van een logregel —
dat is precies wat wij bij punt 1 fout deden.

**Onze vraag:** kan een restore die faalt op de door de aanroeper opgegeven bestemming een
4xx worden, of anders een `ErrorCategory` meekrijgen? Eén van beide is genoeg; dan zeggen wij
"jouw invoer" en exit 1.

### Antwoord

**Allebei: het wordt een 400, én er zit een `error_category` bij.** Je hoeft niets uit
een logregel te raden, en je hoeft ook niet te kiezen welke van de twee je afvangt.

```sh
curl -X POST -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"target_database_host":"doel.invalid","target_database_name":"d",
       "target_database_user":"u","target_database_password":"g"}' \
  "$BASE/v1/restore/database/sandboxed-local/rig-$P/backup?project_name=$P"
# HTTP 400
# {"status":"failed",
#  "error_category":"InvalidTarget",
#  "message":"Failed to restore database backup: Restore pod failed. Logs: ...",
#  "result":{...}}
```

`InvalidTarget` is nieuw in dezelfde `ErrorCategory` als de rest. Bij een mislukte
restore staat het veld er altijd: `InvalidTarget` als het aan je bestemming lag,
`Unknown` als het aan ons lag. `error_category` ontbreekt alleen bij een geslaagde
restore. Voor `restore bucket` geldt hetzelfde antwoord met dezelfde codes.

**Hoe wij het bepalen -- niet op de tekst.** De restore-pod controleert zijn
bestemming al vóórdat hij data aanraakt: `psql -c "SELECT 1"` bij een database,
`mc alias set` bij een bucket. Die controle sluit nu af met een eigen exitcode (20),
en wij lezen die exitcode uit de podstatus. Er wordt nergens naar
`could not translate host name` gezocht. Dat is bewust: die bewoording is van
PostgreSQL en van mc, niet van ons, en een nieuwe versie mag hem herschrijven zonder
dat je exit code omslaat. Precies de valkuil waar jullie bij punt 1 uit geklommen zijn.

**Wat er wél onder valt** -- alles wat de bestemmingspoort tegenhoudt, en dat is
alles wat je in de doelvelden meestuurt:

| Geval | Antwoord |
|---|---|
| Hostnaam resolvet niet | 400 `InvalidTarget` |
| Host resolvet, poort weigert of is onbereikbaar | 400 `InvalidTarget` |
| Verkeerd wachtwoord of onbekende gebruiker | 400 `InvalidTarget` |
| Database bestaat niet op die host | 400 `InvalidTarget` |
| MinIO-endpoint onbereikbaar, of access key / secret key geweigerd | 400 `InvalidTarget` |

Een bestemming die wél resolvet maar jou afwijst op het wachtwoord is dus net zo goed
je invoer, en krijgt dezelfde behandeling. Dat is ook waarom wij niet op de hostnaam
alleen gaan zitten.

**Wat er níet onder valt**, en dus 500 met `Unknown` blijft: onze kopia-repository,
een ontbrekende of onleesbare snapshot, een pod die niet start of zijn image niet kan
halen, een tijdslimiet, en het cluster zelf. Ook een database die halverwege wegvalt
nadat de verbinding wél tot stand kwam blijft 500: die poort was toen geslaagd, en
wij gaan achteraf niet alsnog naar jou wijzen.

**Eén eerlijke grens.** Een bestemming die jou binnenlaat maar de schrijfactie weigert
-- rechten die genoeg zijn om in te loggen en te weinig om te herstellen -- komt door
de poort heen en faalt daarna. Dat is nu een 500. Wij hebben dat zo gelaten omdat er
op dat punt al data verplaatst is en de pod-uitkomst daar niet meer eenduidig zegt van
wie het probleem is; liever een 500 die je nog eens laat kijken dan een 400 die er
soms naast zit. Kom je dit in de praktijk tegen, dan horen wij het graag: dan is er een
tweede poort te zetten vlak vóór het schrijven.

**Restore zónder doelvelden krijgt deze categorie nooit.** Sinds punt 7 kiest het
platform de bestemming als je de vier velden weglaat. Dan is een mislukte bestemming
per definitie niet je invoer, en blijft het antwoord 500 met `Unknown` -- ook als de
pod op diezelfde poort strandt. Anders zouden wij je de schuld geven van een keuze die
je niet gemaakt heeft.

**Geen credentials in het antwoord.** De categorie is een vaste tekenreeks. De
foutregel die de pod schrijft noemt de *velden* ("host, port, database name, user or
password rejected"), nooit de waarden. `message` bevat nog steeds de pod-logs, precies
zoals je ze waardeerde; die logs echoën het wachtwoord niet, dat is nagemeten en staat
als test vast.

Vastgelegd in `tests/test_restore_target_fault.py`: de exitcode uit beide
pod-sjablonen, dat alleen díe exitcode telt, dat een aanroeper zonder doelvelden de
categorie nooit krijgt, en dat het meegestuurde wachtwoord niet terugkomt.

---

# Nagemeten op de releasebuild, 13 augustus

Tegen `5c026ecc`, podnaam stabiel over drie peilingen. Wij hebben de live spec veld voor veld
tegen onze gevendorde gelegd: **geen paden erbij of eraf, geen schema's erbij of eraf**, en
zeven gewijzigde schema's. Die zeven zijn jullie antwoorden op vraag 7 en 9, en die zijn bij
ons doorgevoerd:

- De vier doelvelden van `DatabaseRestoreRequest` en `BucketRestoreRequest` zijn optioneel,
  met "omit to restore into the project's own database" erbij. Onze `--target-*`-opties zijn
  dat nu ook. Wij weigeren wel een *half* doel (twee van de vier): dat zou bij jullie als
  "geen doel" binnenkomen en in de eigen database schrijven terwijl de gebruiker dacht ergens
  anders te schrijven.
- `ErrorCategory.InvalidTarget` en `error_category` op beide restore-antwoorden. Toegevoegd
  aan onze enum en aan beide kaarten; `InvalidTarget` levert exit code 1 in plaats van 2.
- `SubtaskStatus.subject` gebruiken wij nu in de foutregels: twee keer "Diensten bijwerken"
  is niet uit elkaar te houden, met het subject zijn het twee componenten bij naam.
- `AccountLink` verloor de waarde `verify`. Wij verwijzen daar nergens naar (diensten komen
  uit de registry), maar een project dat `verify` had staan wordt hiermee ongeldig.

Twee dingen werken nog niet, en allebei zitten ze in wat er net bij kwam.

## 10. Zonder doel is de restore niet te bereiken: geen enkele naam wordt geaccepteerd

De omgekeerde kant van vraag 7. `restore database` zonder `--target-*` hoort in de eigen
database terug te zetten, maar wij krijgen de referentienaam niet geraden — ook niet de naam
die jullie eigen antwoorden publiceren.

```sh
zad backup create productie
zad backup list productie -o json | jq -c '[.runs[].items[] | {resource_type, reference_name}]'
# [{"resource_type":"database","reference_name":"backup"}, {"resource_type":"bucket","reference_name":"bucket-backup"}]

zad restore list sandboxed-local rig-$P -o json | jq -c '[.[].pvc_name]'
# ["bucket-backup","backup"]
```

Dus beide lijstendpoints zeggen `backup`. Maar:

```sh
curl -X POST -H "X-API-Key: $KEY" -H 'Content-Type: application/json' -d '{}' \
  "$BASE/v1/restore/database/sandboxed-local/rig-$P/backup?project_name=$P"
# 404 "No deployment of project '<p>' has a database backup named 'backup'.
#      Supply the target_database_* fields to restore into an external database."
```

Wat wij nog meer geprobeerd hebben, alle vijf 404 met dezelfde melding: de componentnaam
(`web`), de deploymentnaam (`productie`), de projectnaam, `database`, en de dienstnaam
(`postgresql-database`).

**Onze vraag:** welke naam hoort daar? Als het een naam is die alleen in het projectbestand
staat, dan is de nieuwe weg van buitenaf niet te vinden, want geen enkel leesendpoint noemt
hem. De melding wijst bovendien naar de externe weg, terwijl de gebruiker juist de eigen
database bedoelde.

### Antwoord

**Jullie hebben niets fout gedaan: de naam die wij publiceerden was onbruikbaar.** De
route verwacht `{deployment}-postgresql` (en `{deployment}-minio` voor een bucket) --
precies wat wij bij vraag 7 opschreven. Wat `backup list` en `restore list` teruggaven
was iets anders, en het leek alleen maar op een naam.

**Waar `backup` vandaan kwam.** Een snapshot draagt zijn naam in een tag. Een
PVC-snapshot heeft een `pvc`-tag; een database- en een bucketsnapshot hebben die niet,
die hebben een `database`- respectievelijk `bucket`-tag. De lijstfunctie die *beide*
leesendpoints voedt, las alleen de `pvc`-tag, en viel bij het ontbreken daarvan terug op
het laatste stuk van het bronpad in de snapshot. Voor een databasedump is dat `/tmp/backup`
en voor een gespiegelde bucket `/tmp/bucket-backup`. Zo werd een mapnaam uit een pod
gepubliceerd als referentienaam. Dat de twee endpoints het eens waren met elkaar en oneens
met de derde, komt doordat ze allebei uit diezelfde ene functie lazen.

Het is opgelost waar het misging: die lijst geeft nu per soort de juiste tag door.
Vanaf deze build:

```sh
zad backup list productie -o json | jq -c '[.runs[].items[] | {resource_type, reference_name}]'
# [{"resource_type":"bucket","reference_name":"productie-minio"},
#  {"resource_type":"database","reference_name":"productie-postgresql"}]

zad restore list sandboxed-local rig-$P -o json | jq -c '[.[].pvc_name]'
# ["productie-minio","productie-postgresql"]

curl -X POST -H "X-API-Key: $KEY" -H 'Content-Type: application/json' -d '{}' \
  "$BASE/v1/restore/database/sandboxed-local/rig-$P/productie-postgresql?project_name=$P"
# 200 {"status":"success","message":"Restored database productie-postgresql to <db>", ...}
```

Een PVC houdt zijn PVC-naam, want dat is wat de PVC-restore in het pad neemt.

**En de melding.** De 404 noemt nu de namen die er wél zijn:

```
No deployment of project 'p' has a database backup named 'backup'.
This project has: 'productie-postgresql'.
Supply the target_database_* fields to restore into an external destination.
```

**Hoe wij dit getoetst hebben, en waarom dat deze keer anders is.** Dit was de derde ronde
over dezelfde weg, en de eerste twee waren per stuk groen en van buitenaf stuk. Daarom is
dit gemeten zoals jullie het doen: een project aangemaakt op de sandbox, een backup gedraaid,
de naam **uit het leesendpoint** genomen en daarmee teruggezet -- database en bucket, met
alleen een API-sleutel en zonder in het projectbestand te kijken. Die hele gang staat als
suite vast in `tests/e2e/test_sandbox_restore_van_buiten.py`, zodat de volgende wijziging
hem opnieuw van buiten aflegt en niet alleen van binnen. De vertaling zelf en de
404-melding liggen vast in `tests/test_restore_reference_van_buiten.py`.

---

## 11. `error_category` komt mee, maar zegt `Unknown` bij precies het geval waarvoor hij bedoeld is

De doorgifte werkt: het veld staat in het antwoord. De classificatie niet.

```sh
curl -X POST -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"target_database_host":"doel.invalid","target_database_name":"d",
       "target_database_user":"u","target_database_password":"g"}' \
  "$BASE/v1/restore/database/sandboxed-local/rig-$P/backup?project_name=$P"
# HTTP 500
# {"status":"failed","error_category":"Unknown",
#  "message":"... psql: error: could not translate host name \"doel.invalid\" to address ..."}
```

`doel.invalid` is een gereserveerd domein dat per definitie niet resolvet, dus dit is het
schoolvoorbeeld van `InvalidTarget`. De waarde bestaat in de enum en wordt hier niet gezet.

**Onze vraag:** wordt `InvalidTarget` ergens wél geproduceerd, of is de bedrading nog niet
aangesloten? Zolang dit `Unknown` is, blijft dit een 500 waar CI niets mee kan.

Wat wij intussen aan onze kant doen: een expliciete `"Unknown"` in dat veld behandelen wij
niet meer als "platform, probeer opnieuw" (exit 2) maar als "niet toe te wijzen" (exit 3).
Jullie hadden een plek om het toe te wijzen en hebben daar "ik weet het niet" ingevuld; dan
is opnieuw proberen niet de conclusie, en de logs lezen wel.

### Antwoord

**De bedrading is wél aangesloten; jullie kwamen er niet aan toe.** Dit is vraag 10 in
een ander jasje, en dat was van buitenaf niet te zien.

De restore-pod doet drie dingen op volgorde: verbinden met de repository, het snapshot
**opzoeken op de referentienaam**, en dan pas de bestemmingspoort (`psql -c "SELECT 1"`,
exitcode 20). Met `backup` als referentie strandde hij op stap twee -- geen snapshot met
die tag -- en dat is een gewone exit 1. Terecht `Unknown`: op dat moment was er nog
niets over jullie bestemming vastgesteld. Wij hebben dat nagespeeld en de pod-logs
zeggen het letterlijk: `ERROR: No database snapshots found for backup`.

Dat jullie eerder wél `could not translate host name` in de melding zagen, klopt met dat
verhaal: die meting is van vóór vraag 10, en die logregels komen uit twee verschillende
mislukkingen.

Met de naam die er nu uit `backup list` komt, gebeurt precies wat wij bij vraag 9
beloofden:

```sh
curl -X POST -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"target_database_host":"doel.invalid","target_database_name":"d",
       "target_database_user":"u","target_database_password":"g"}' \
  "$BASE/v1/restore/database/sandboxed-local/rig-$P/productie-postgresql?project_name=$P"
# HTTP 400
# {"status":"failed","error_category":"InvalidTarget","message":"... ERROR: target
#  database is not usable (host, port, database name, user or password rejected)", ...}
```

Gemeten op de sandbox, met een API-sleutel, in dezelfde run als het antwoord op vraag 10.

**De classificatie leunt nog steeds niet op tekst.** Wat de 400 bepaalt is de exitcode
van de pod (20 uit de bestemmingspoort), niet de bewoording van psql of mc. In de logs
hierboven staat `could not translate host name` gewoon weer -- maar niemand leest hem.
Verandert PostgreSQL die zin morgen, dan blijft jullie exit code 1.

**Eén ding dat blijft zoals het was.** `Unknown` betekent nog altijd "de fout lag niet
aantoonbaar bij jouw bestemming", en dat is méér dan alleen platformpech: het dekt ook
een aanroep die de bestemmingspoort nooit haalde, zoals hierboven. Jullie nieuwe
behandeling ("niet toe te wijzen", exit 3, en de logs lezen) past daar beter bij dan
"probeer opnieuw". Wij zouden hem zo laten.

---

## 12. `publish-on-web` is op `5c026ecc` niet meer aan te zetten (blokkerend, regressie)

Vanmiddag liep ons draaiboek 01 hier nog helemaal door op `edbda374`, inclusief
`publish-on-web` op twee componenten. Op de releasebuild `5c026ecc` lukt het via geen enkele
weg meer, ook niet op een vers project.

```sh
curl -X PUT -H "X-API-Key: $KEY" -H 'Content-Type: application/json' -d '{"tls":"standard"}' \
  "$BASE/v2/projects/$P/services/publish-on-web/config/component/frontend?rollout=false"
# 202, en de taak faalt:
# "Services that must be enabled at project level first: ['publish-on-web']. They need
#  project-level configuration that cannot be assumed, so they are not added automatically."
```

Hetzelfde via `PATCH .../components/frontend {"services":["publish-on-web"]}`, en hetzelfde
bij `POST .../components` met de dienst er meteen bij. Drie routes, één fout.

**En die projectlaag bestaat niet.** Niet in de catalogus:

```sh
curl -s "$BASE/v2/services/publish-on-web" | jq -c '[.layers[].target]'
# ["component","deployment","deployment-component"]
```

En niet als endpoint:

```sh
curl -X PUT -H "X-API-Key: $KEY" -d '{}' "$BASE/v2/projects/$P/services/publish-on-web/config/project"
# 404 {"detail":"Not Found"}
```

De validator eist dus iets wat de catalogus niet kent en waar geen endpoint voor is. Gevolg:
`publish-on-web` is niet aan te zetten, en `keycloak` daarmee ook niet, want die vereist hem.
Een component zonder publish-on-web draait wel maar is van buiten onbereikbaar, dus dit
blokkeert elke doorloop die iets aantoonbaar werkends wil opleveren.

**Onze vraag:** is deze controle nieuw in `5c026ecc`? Zo ja, wat is de bedoelde weg om aan
die eis te voldoen? Drie vormen die wij ons kunnen voorstellen:

1. De controle laten vervallen voor diensten die geen projectlaag hebben. Dat lijkt ons het
   meest waarschijnlijk het gewenste gedrag: hij vraagt om iets wat niet uit te drukken is.
2. `publish-on-web` een projectlaag geven, waarbij lege config "aan" betekent.
3. De component- of deploymentconfiguratie het enablement zelf laten aanmaken, zoals bij
   `postgresql-database`, `redis` en `minio-storage` blijkbaar wel gebeurt.

**Waar wij dit vandaan hebben:** een agent die alleen de CLI mocht raadplegen liep hierop
vast en heeft het uitgebreid vastgelegd. Dat is precies het scenario waar deze CLI voor
bedoeld is, dus wij hechten er waarde aan dat het van buitenaf op te lossen valt.

### Antwoord

**Jullie hebben gelijk, het was een regressie van ons, en jullie eerste vorm was de
juiste.** De controle is nieuw sinds RC-84 en hij weigerde `publish-on-web` met een eis
waaraan niet te voldoen was. Hij is nu weg voor elke dienst die geen projectlaag heeft.

**Wat er misging.** Sinds RC-84 mag een dienst zichzelf op projectniveau aanmelden als de
dienst zelf zegt dat dat mag. Diensten die op projectniveau een besluit vragen - keycloak
(welke realm), attachments (welke catalogus) - zeggen nee, en dan hoort de aanroeper dat
besluit eerst te nemen. `publish-on-web` stond bij de neezeggers omdat wij dachten dat het
project vastlegt op welke domeinen gepubliceerd mag worden. Dat klopt niet: die keuze en de
bijbehorende goedkeuringen (`domain`, `subdomain`) zitten op de **deployment**, niet op het
project. De dienst draagt helemaal geen projectlaag - precies wat jullie in de catalogus
zagen - en dus verwees de weigering naar een laag die niet bestaat, met geen enkele manier
om eraan te voldoen. Drie routes, één muur, en van buitenaf niet op te lossen.

**De regel is nu systematisch, niet per dienst.** Weigeren mag alleen als de aanroeper de
laag kán bereiken waar hij naartoe gestuurd wordt. Heeft een dienst geen projectlaag, dan
is de kale selectie (de naam in de projectlijst, geen configblok) het juiste gedrag,
ongeacht wat de dienst verder over impliciete selectie verklaart en ongeacht goedkeuringen
- die beschermen een projectbesluit, en dat is er dan niet. Dit dekt ook de volgende dienst
met dezelfde vorm; een test bewaakt dat elke overgebleven weigeraar een projectlaag heeft
die `GET /v2/services/{naam}` ook meldt.

**De andere kant op gemeten**, zoals jullie impliciet vroegen: `publish-on-web` was de enige
dienst in de catalogus met deze vorm. De zes diensten die nog weigeren (`keycloak`,
`attachments`, `authorization-wall`, `cross-domain-access`, `sleep-mode`, `invite`) hebben
allemaal een projectlaag die de catalogus meldt en die te zetten is: vijf met
`PUT /v2/projects/{p}/services/{svc}/config/project`, en `attachments` met zijn eigen
projecthandeling (`POST /v2/projects/{p}/services/attachments/attachment`, de catalogus die
een component daarna aanhaalt). Daar klopt de melding dus wel.

**Wat er niet verandert.** De goedkeuring per deployment blijft staan: een domein of
subdomein aanvragen vraagt nog steeds om een platformbeheerder, en tot die er is publiceert
de deployment op de terugvalnaam. Aanzetten is geen goedkeuring.

**Gemeten op de sandbox** (build `b9bfb9b5`), drie verse projecten, één per route, met de
projectlijst uit het Forgejo-projectbestand als toets:

```sh
PUT   /v2/projects/$P/services/publish-on-web/config/component/web?rollout=false  {"tls":"standard"}
PATCH /v2/projects/$P/components/web?rollout=false                                {"services":["publish-on-web"]}
POST  /v2/projects/$P/components   {"name":"web2","image":...,"services":["publish-on-web"]}
# alle drie: 202, taak completed, en 'publish-on-web' staat daarna op de projectlijst
```

Dat staat als vangrail in `tests/e2e/test_sandbox_publish_on_web.py` (4 tests, groen),
naast de beslissing zelf in `tests/test_implicit_project_selection.py`.

---

## 13. `:refresh` belooft een webadres dat niet bestaat, `describe` zegt eerlijk van niet

Dezelfde deployment, hetzelfde moment, twee endpoints die elkaar tegenspreken.

Project `p1-wan`, één component `worker`: geen poort, geen `publish-on-web`, dus geen
ingress. Wat de refresh teruggeeft:

```sh
curl -X POST -H "X-API-Key: $KEY" "$BASE/v2/projects/$P/:refresh"
# result.urls.productie.urls = {"worker": "https://worker-productie-p1-wan.sandbox.rijksapp.dev"}
```

Wat het deployment-endpoint op datzelfde moment zegt:

```sh
curl -H "X-API-Key: $KEY" "$BASE/v2/projects/$P/deployments/productie" | jq -c '{status, urls}'
# {"status":"Healthy","urls":{}}
```

En wie er gelijk heeft:

```sh
curl -o /dev/null -w '%{http_code}\n' https://worker-productie-p1-wan.sandbox.rijksapp.dev
# 404
```

`describe` heeft dus gelijk en `:refresh` verzint een adres. Het lijkt de naam te vormen uit
`{component}-{deployment}-{project}` zonder te kijken of er een ingress voor bestaat.

**Waarom dit meer is dan cosmetisch.** De refresh-uitvoer is precies wat iemand leest na
"zet mijn project neer": het is het laatste wat over het scherm gaat en het enige dat
adressen noemt. Een agent die daarop afgaat rapporteert een draaiende applicatie op een URL
die 404 geeft, en zoekt de fout vervolgens in zijn eigen stappen. Wij zijn het zo
tegengekomen: een agent meldde het als vreemd, wij konden het reproduceren.

**Onze vraag:** kan `:refresh` dezelfde bron gebruiken als `describe`, of alleen adressen
noemen van componenten die daadwerkelijk een ingress hebben gekregen? Wij tonen dit veld
ongewijzigd, dus filteren aan onze kant zou betekenen dat wij gaan raden wat de API bedoelt.

### Antwoord

Jullie diagnose klopt: `describe` had gelijk en de refresh verzon het adres. **Gerepareerd in
deze PR, en wel door de bron samen te voegen in plaats van door de tekst te filteren.** Voor
`p1-wan` geeft `:refresh` nu `urls.productie.urls = {}`, precies wat het deployment-endpoint
zegt.

**Waar dat adres vandaan kwam.** In de manifestgenerator, bij het opbouwen van de deployment,
werd per component de hostnaam berekend en meteen als URL weggeschreven op het resultaat dat de
refresh teruggeeft. Verderop in diezelfde lus staat het punt waar besloten wordt of er een
ingress komt: `if publish_on_web: manifests.append("ingress.yaml.jinja")`. De eerste stap kende
die voorwaarde niet. De hostnaam is er namelijk altijd — hij is een naam, niet een belofte — en
alleen de tweede stap bepaalt of er ook iets naar luistert. Dus voor `worker` schreef de
generator geen ingress en rapporteerde hij wel een adres.

Het deployment-endpoint keek wel of het component publish-on-web had, en dat verklaart precies
de twee antwoorden die jullie op hetzelfde moment kregen.

**Wat wij van jullie vraag over de UI hebben gemaakt.** "Publieke links" op de deploymentpagina
kwam uit een derde afleiding, in de dienst `publish-on-web` zelf, en de componentenlijst
eronder uit een vierde, inline in de webrouter. Allebei controleerden ze publish-on-web, dus de
pagina maakte de fout niet — maar vier plekken die elk netjes hun eigen antwoord samenstellen
zijn vier plekken die kunnen gaan afwijken, en dat is precies hoe dit is ontstaan.

**Er is nu één bron:** `public_url_map_for_deployment()` in
`opi/services/catalog/publish_on_web/urls.py`, naast de bestaande lijstvorm die de paden
meeneemt. Publish-on-web is de dienst die beslist of een component van buiten bereikbaar is,
dus daar hoort het antwoord. Alle lezers halen het daar op:

| lezer | wat er veranderde |
|---|---|
| `POST /v2/projects/{p}/:refresh` | de generator vult het resultaat uit die bron; geen ingress, geen adres |
| `GET /v2/projects/{p}/deployments/{d}` | leest dezelfde functie in plaats van een eigen kopie |
| "Publieke links" op de deploymentpagina | ongewijzigd, dit was al die bron |
| de componentenlijst op de projectpagina | leest nu dezelfde links, per component gefilterd |
| de voortgangsweergave tijdens een taak | toonde per component hetzelfde spookadres, en volgt nu de bron |

Twee dingen blijven staan. De sleutels `root` en `bare-domain` in de refresh-uitvoer blijven,
en die zijn te vertrouwen: ze worden weggeschreven op het moment dat die ingress werkelijk
gegenereerd wordt. En het antwoord blijft een adres per component zonder pad; dat is wat het
deployment-endpoint ook geeft, en het is de vorm waarop jullie afgaan.

Vastgelegd in `tests/test_refresh_verzint_geen_webadres.py`: `p1-wan` met een `worker` zonder
publish-on-web levert een lege map op, de refresh-kant en het deployment-endpoint geven per
constructie hetzelfde, en de oude afleiding uit de naamgeving laat die eerste toets vallen.

---

# Hertest op de sandbox, 13 augustus, eind van de middag

Er draait nieuwe code: hetzelfde commit `5c026ecc`, maar `dirty: true` en een ander image
(`b90dd304…` tegen `f7d955e1…` vanmiddag). Dat de hash gelijk bleef terwijl het gedrag
veranderde is precies waarvoor jullie `image` hebben toegevoegd; zonder dat veld hadden wij
geconcludeerd dat er niets nieuws stond.

**Wat wij nagemeten hebben en werkt:**

| Punt | Stand |
|---|---|
| 7 + 10 restore zonder doel | **opgelost.** `success: true`, teruggezet in `v1_kew_productie`. De 404 bij een verkeerde naam noemt nu ook wat er wél is |
| 9 + 11 fouttoekenning bij een onbereikbaar doel | **opgelost.** HTTP 400 in plaats van 500, en `error_category: InvalidTarget`. Onze kant maakt daar exit 1 van, "jouw invoer" |
| 12 publish-on-web | **opgelost.** Gaat er gewoon op |
| 13 refresh noemde een URL die 404 gaf | **opgelost.** Alleen componenten met een ingress krijgen er een |
| 6 een nieuw project | onveranderd: 202, en de sleutel geeft nog ~2 seconden 401. Blokkeert niets meer, want wij wachten |

Twee dingen die de hertest opleverde.

## 10b. `backup list` en de foutmelding noemen verschillende namen

De 404 zegt nu netjes wat er wél is, en dat is een grote vooruitgang. Alleen komt die naam
niet overeen met die van `backup list`:

```sh
zad backup list productie -o json | jq -c '[.runs[].items[].reference_name] | unique'
# ["productie-postgresql"]     <- deze werkt

curl -X POST -H "X-API-Key: $KEY" -d '{}' "$BASE/v1/restore/database/$CLUSTER/$NS/backup?project_name=$P"
# 404 "... This project has: 'productie-database'. ..."   <- deze bestaat niet als referentie
```

`productie-postgresql` doet het, `productie-database` niet. De suggestie wijst dus naar een
naam die je vervolgens niet kunt gebruiken, wat een gebruiker een ronde extra kost precies
op het moment dat hij al iets fout deed.

**Onze vraag:** kan de suggestie dezelfde namen noemen als `backup list` teruggeeft?

### Antwoord

Je hebt gelijk, en het was erger dan een verkeerde suggestie: die naam werd ook geaccepteerd. De restore startte dan en strandde pas daarna op een snapshot die niet bestaat.

De oorzaak was een tweede naamconventie. De schrijver leidt de naam af als `{deployment}-{dienstnaam tot het eerste streepje}`, dus `postgresql-database` wordt `productie-postgresql`, en die naam gaat als kopia-tag mee. Dat is precies wat `backup list` publiceert. Het restorepad gebruikte een vast achtervoegsel `{deployment}-database`. Bij buckets viel dat toevallig samen, want `minio-storage` begint ook met `minio`; bij databases niet, en daarom is het nooit opgevallen.

Opgelost: beide kanten volgen nu de regel van de schrijver. De 404 noemt de namen die een backup werkelijk kan dragen, en dezelfde namen worden geaccepteerd. Het oude achtervoegsel blijft er als geldige naam bij staan, want wat ooit zo is weggeschreven moet terug te zetten blijven. Vastgelegd in `tests/test_restore_suggereert_bestaande_namen.py`.

Wat we nog niet hebben kunnen verklaren is waarom bij jullie de terugvalnaam werd getoond terwijl de snapshot `productie-postgresql` draagt. Die terugval geldt alleen als er in die deployment geen component met een databasedienst gevonden wordt, en op backupmoment was die er kennelijk wel. Stuur je de projectnaam en de uitvoer van `zad project describe`? Dan kunnen we dat sluitend maken in plaats van te gissen.

<!-- ruimte voor RIG-Cluster -->

---

## 8b. Het venster uit vraag 8 bestaat, en `describe` dekt het toe

Vraag 8 vroeg of een wijziging die tijdens een lopende refresh wordt opgeslagen erin
meegaat. Vanmiddag wel; nu niet, en dat is nuttiger dan een ja.

```sh
TA=$(zad --no-wait project refresh -o json | jq -r .task_id)
zad component assign laatkomer productie --image $IMG --no-rollout   # tijdens TA
TB=$(zad --no-wait project refresh -o json | jq -r .task_id)
test "$TA" = "$TB"          # klopt: samengevoegd, geen tweede taak
# ... TA loopt af als 'completed' ...
```

Daarna:

```sh
zad project pending -o json
# {"count":1,"task_types":["add_component_to_deployment"]}     <- goed: hij weet het

curl -o /dev/null -w '%{http_code}\n' https://laatkomer-productie-v1-kew.sandbox.rijksapp.dev/status
# 404                                                          <- klopt, nog niet uitgerold

zad deployment describe productie -o json | jq -c .urls
# {"frontend":"https://…","laatkomer":"https://laatkomer-productie-v1-kew…"}   <- en dit niet
```

Een tweede `project refresh` zet `pending` op 0 en het adres op 200.

**Twee dingen, en het eerste is goed nieuws.** `pending` klopt: de samengevoegde taak neemt
een wijziging van ná zijn start niet mee, en dat is eerlijk zichtbaar. Wij hadden gevreesd
dat `pending` op 0 zou springen, en dat doet hij niet.

**Wat wel misleidt is `deployment describe`**: die noemt een URL voor een component dat nog
niet is uitgerold. Dat is dezelfde vorm als punt 13, maar dan andersom — daar verzon
`:refresh` een adres voor iets zonder ingress, hier noemt `describe` er een voor iets dat er
nog niet is. Iemand die na een refresh de adressen naloopt krijgt er een die 404 geeft, en
gaat dat in zijn eigen stappen zoeken.

**Onze vraag:** hoort `urls` in `describe` de gewenste of de werkelijke toestand te
beschrijven? Als het de gewenste is, dan is dat verdedigbaar, maar dan willen wij het
kunnen zien — bijvoorbeeld doordat het veld leeg blijft tot de ingress bestaat, of doordat
er iets naast staat dat zegt dat er nog werk wacht. Wij tonen dit veld ongewijzigd en gaan
niet raden.

### Antwoord

Het is de gewenste toestand, en je vraag legt terecht de vinger op de zere plek: dat was aan het antwoord niet te zien.

Elk leesantwoord van de v2-API beschrijft het projectbestand en zegt dat ook, met `source` en `pending_rollout`. Het deploymentantwoord is het enige dat twee bronnen mengt: `components`, `urls` en `subdomain` komen uit het projectbestand, terwijl `status`, `sync_revision`, `last_synced_at` en `errors` uit de cluster komen. Uitgerekend dat antwoord droeg die twee velden niet, waardoor je de hele inhoud als live las. Dat is onze fout, niet een misverstand aan jullie kant.

Opgelost: `deployment describe` en de deploymentlijst dragen nu allebei `source` en `pending_rollout`. De beschrijving van `source` noemt bovendien welk veld uit welke bron komt, want alleen "project-file" zeggen bij een antwoord dat mengt verplaatst het misverstand alleen maar.

Twee dingen om te weten bij het gebruik. In een lijst staat `pending_rollout` op het omhullende antwoord en is hij per deployment `null`: het is een eigenschap van het project, en hem per deployment herhalen zou hetzelfde getal zo vaak neerzetten als er deployments zijn. En als onze takenservice onbereikbaar is staat er ook `null`; dan ontbreekt het etiket, niet de beschrijving. Een 503 op `describe` omdat een bijkomend veld niet te bepalen is, leek ons slechter dan een antwoord zonder dat veld.

Wat nu nog niet kan is per component zeggen welke URL nog wacht. `pending_rollout` telt de wachtende wijzigingen en noemt hun soort, maar niet de namen. Zeg maar of je dat nodig hebt, dan is dat een eigen stuk werk.

<!-- ruimte voor RIG-Cluster -->
