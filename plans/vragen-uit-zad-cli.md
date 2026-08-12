# Vier open vragen uit de zad-cli-doorlopen

Vastgelegd 12 augustus 2026 door zad-cli, na vier volledige draaiboeken tegen
`https://zad.sandbox.rijksapp.dev/api`.

**Aan de lezer van dit bestand:** het zijn genummerde punten met per punt de reproductie en
wat wij al hebben uitgesloten. Antwoord er graag onder, per nummer, in het kopje "Antwoord"
dat er al staat. Alles is met `curl` te reproduceren; onze CLI is er alleen een client op.

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
