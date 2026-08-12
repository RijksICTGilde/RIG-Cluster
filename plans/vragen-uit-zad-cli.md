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

<!-- ruimte voor RIG-Cluster -->

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

<!-- ruimte voor RIG-Cluster -->

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

<!-- ruimte voor RIG-Cluster -->

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

<!-- ruimte voor RIG-Cluster -->

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

<!-- ruimte voor RIG-Cluster -->
