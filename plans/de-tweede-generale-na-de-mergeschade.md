# De tweede generale: alles opnieuw meten na de mergeschade

Er is sinds de eerste generale (RC-113) veel bijgekomen, en er is één ding gebeurd dat het vertrouwen raakt: **bij een merge is een deel van een reparatie verdwenen zonder dat iets het merkte.** De htmx-omzetting van de goedkeuringsdialoog viel weg bij het samenvoegen van twee takken; de unittests bleven groen, en alleen de browsertests zagen het. Dat is precies het soort verlies dat je niet met lezen vindt.

Deze doorloop is er om dat vertrouwen met metingen te herstellen, niet met beweringen. **Alles moet groen.** Niet "groen op de bekende falers".

## Vooraf

Claim het slot met `orch sandbox claim`; dit houdt het cluster uren bezet.

De drie controles die eerdere rondes geld gekost hebben, in deze volgorde:

- `kubectl config current-context` is `kind-rig-sandbox`;
- `curl -sk https://zad.sandbox.rijksapp.dev/version` geeft **vijf keer achter elkaar** dezelfde commit als `git rev-parse --short HEAD`. Vlak na een deploy antwoorden de oude en de nieuwe pod allebei, en dan meet je een mengsel. Let op: tijdens de vorige ronde bleek de sandbox tien commits achter te lopen terwijl skaffold draaide, dus **controleer dit ook halverwege opnieuw**;
- `uv sync --all-groups` in de worktree.

En de valkuil die RC-110 een rode suite kostte: `E2E_SECRET_KEY` haal je zo op, niet met een jsonpath op een sleutel die niet bestaat (dat geeft geen fout maar een lege string):

```bash
kubectl -n rig-system get cm operations-manager-config -o jsonpath='{.data.\.env}' \
  | grep -E '^SECRET_KEY=' | cut -d= -f2-
```

## Taken

### 1. Alle geautomatiseerde suites

- `uv run pytest tests/ -q` met de eigen standaardaanroep (geef **geen** eigen `-m` mee);
- `uv run pytest -m e2e -q`, **twee keer achter elkaar**, en meld beide uitkomsten. Wisselvalligheid toont zich niet in één run, en er is deze week een gedocumenteerd geval van twee tests die een browsersessie delen;
- `uv run pytest -m sandbox -q` tegen het draaiende cluster;
- `uv run pytest -m reallife -q` en `-m punt14` gelijktijdig, zoals in RC-112.

Bekend en niet van deze release: vier rode in `tests/test_taken_voortgang_link.py` horen bij onafgemaakt werk van een andere sessie. Melden, niet repareren.

Faalt er iets anders, dan **eerst uitzoeken of het aan de test of aan de code ligt**.

### 2. Elk voorbeeldproject dat voor de sandbox bedoeld is

Rol ze uit op het cluster, één voor één, en stel per project vast: komt het overeind, wordt het `Healthy` in ArgoCD, antwoorden de URL's, en klopt wat er in de projectenrepository terechtkomt.

Dit is het deel dat de suites niet dekken: die meten onderdelen, een projectbestand meet de keten.

### 3. Wat er sinds de vorige generale bij is gekomen

Per punt: werkt het, of werkt het niet en wat stond er dan. Meet in de browser waar het een scherm betreft.

1. **De goedkeuringsdialoog** — dit is de reden van deze doorloop. Opent hij, staat de echte projectnaam in de URL, is er één kop, is er geen leeg foutvak, en werkt goedkeuren en afwijzen? En werkt de **gedeelde** schil nog: de bewerkdialogen van een project openen, opslaan en sluiten met Escape.
2. **Een niet-goedgekeurd domein** — de getoonde URL is het clusteradres en niet het aangevraagde, met de waarschuwing erbij, en de ingress wordt niet op het aangevraagde domein aangemaakt.
3. **`domain-format`** — een onzinwaarde wordt geweigerd, via de API én in het formulier, en de geldige waarden staan in `/openapi.json`.
4. **De storage-config** — `PUT` op persistent-storage, temp-storage en attachments geeft 202 en vervangt het blok; `PATCH` blijft werken.
5. **`check-subdomain`** op zijn nieuwe pad met de projectnaam erin.
6. **De introductiepagina** — bereikbaar zonder inloggen, `/` stuurt een anonieme bezoeker erheen, en het menu-item staat onder Platform.
7. **De metrics-explorer** — de keuzelijsten overlappen niet, ook in Firefox.
8. **De invitecode** — komt terug bij het aanmaken en is terug te lezen; env-varwaarden blijven `***`.
9. **Sleep-mode** — een nieuw project krijgt `wake-mode: confirm`, en de twee endpoints accepteren de projectsleutel.
10. **De platformvelden** — een PUT die `realms` meestuurt wordt geweigerd, een PUT zonder laat het staan.

### 4. Het verslag

Eén document in `docs/`, met per taak wat er gemeten is en wat er misging. **Ook wat er misging.** Sluit af met een oordeel in één zin: deze branch kan naar main, of niet en dit is waarom.

## Wat er buiten valt

- Repareren, tenzij het de merge blokkeert; dan staat dat er met zoveel woorden bij.
- De mailrelay: geparkeerd, zie `TODO_NEXT_RELEASE.md`.
- Productie.
