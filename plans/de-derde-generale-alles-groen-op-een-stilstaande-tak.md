# De derde generale: alles groen, op een tak die stilstaat

Dit is de doorloop waar RC-118 zelf om vroeg. Zijn oordeel was niet "deze tak kan naar main", maar:

> Deze tak vraagt eerst een review, en daarna een verse testronde door iemand anders. (...) De weg vooruit is dus review -> merge-beslissing -> een nieuwe doorloop door een andere sessie, op een tak die niet meer beweegt.

Aan die laatste voorwaarde was toen niet voldaan, en daarna zijn er nog zo'n twintig wijzigingen bij gekomen. Deze taak lost dat in. **Alles moet groen.** Niet "groen op de bekende falers".

## De eerste eis: de tak staat stil

De opdrachtgever bevriest `release-augustus-2026` voor de duur van deze doorloop. Noteer bij het begin de commit (`git rev-parse HEAD`) en **meet aan het eind of hij nog dezelfde is**. Is hij verschoven, dan is de uitkomst niet het oordeel over de tak die naar main gaat, en dan zeg je dat met zoveel woorden in plaats van de conclusie te laten staan.

## Vooraf, en dit is waar de vorige twee rondes geld verloren

Claim het slot met `orch sandbox claim`; dit houdt het cluster uren bezet.

1. `kubectl config current-context` is `kind-rig-sandbox`.
2. `curl -sk https://zad.sandbox.rijksapp.dev/version` geeft **vijf keer achter elkaar** dezelfde commit als `git rev-parse --short HEAD`. Vlak na een deploy antwoorden de oude en de nieuwe pod allebei.
3. **En blijf dat controleren tijdens de doorloop.** Dit ging in RC-118 mis: taak 2 draaide een tijd lang tegen de image van een ANDERE PR, en dat kwam pas uit doordat de opdrachtgever het zei. Vijf gemeten projecten zijn weggegooid. Het slot beschermde daar niet tegen — een andere PR kon de claim overnemen terwijl er nog een lease liep. Controleer dus per blok, niet alleen aan het begin.
4. `/version` heeft in RC-118 **twee keer gelogen**. De betrouwbare controle is de pod zelf vragen of de code erin zit (bijvoorbeeld een string uit een verse wijziging opzoeken in de container), niet het endpoint geloven.
5. `uv sync --all-groups` in de worktree, anders faalt de pytest-hook op ontbrekende testafhankelijkheden.
6. `E2E_SECRET_KEY` haal je zo op, niet met een jsonpath op een sleutel die niet bestaat (dat geeft geen fout maar een lege string):

```bash
kubectl -n rig-system get cm operations-manager-config -o jsonpath='{.data.\.env}' \
  | grep -E '^SECRET_KEY=' | cut -d= -f2-
```

## Taak 1: alle geautomatiseerde suites

- `uv run pytest tests/ -q` met de eigen standaardaanroep (geef **geen** eigen `-m` mee);
- `uv run pytest -m e2e -q`, **twee keer achter elkaar**, en meld beide uitkomsten;
- `uv run pytest -m sandbox -q` tegen het draaiende cluster;
- `uv run pytest -m reallife -q` en `-m punt14` gelijktijdig, zoals in RC-112;
- `go vet ./... && go test ./...` in `images/zad-waker/` — daar is deze week code bij gekomen.

Er zijn deze week tests toegevoegd die het cluster nodig hebben (`test_sandbox_secret_rollout.py`, `test_sandbox_restore_generation.py`, `test_sandbox_restore_extra_schema.py`). Die vallen onder `-m sandbox` en horen dus echt te draaien, niet overgeslagen te worden.

Faalt er iets, dan **eerst uitzoeken of het aan de test of aan de code ligt**.

## Taak 2: de 47 projecten uit de sandboxrepository

Alle projectbestanden uit `rig-cluster-projects-sandbox` (branch `main`, map `projects/`): <https://git.claude.robbertuittenbroek.nl/robbert/rig-cluster-projects-sandbox/src/branch/main/projects>. Volgens de opdrachtgever zijn het er 47; **tel ze zelf** en meld het aantal, zodat een stil overgeslagen bestand opvalt.

Rol ze uit op het cluster en stel per project vast:

- komt het overeind, en wordt het `Healthy` in ArgoCD;
- antwoorden de URL's;
- klopt wat er in de projectenrepository terechtkomt.

**Meet op het signaal, niet op de klok.** Dit is de derde les van RC-118 en hij scheelde daar een factor twintig: wachten op een synchrone `:refresh` kostte 20 minuten per project met een onbereikbare image; wachten op wat het cluster zelf zegt — ArgoCD-health en podstatus — bracht dat terug naar 24 tot 85 seconden.

Een project dat niet omhoog komt door iets buiten ons (een image die niet te trekken is, bijvoorbeeld) is geen fout van deze release; meld dat apart van een echte fout, met wat je zag.

## Taak 3: wat er sinds de tweede generale bij is gekomen

Per punt: werkt het, of werkt het niet en wat stond er dan. Meet in de browser waar het een scherm betreft.

1. **De cascade in de wizard** — kies in "Cross-domain toegang" een peer-project en daarna snel een deployment: de lijsten vullen zich en een keuze raakt niet weg. Dit is de bug van RC-127, en hij zat in ELK afhankelijk keuzeveld, dus kijk ook bij een andere dienst met een cascade.
2. **CAA-records** (RC-126) — de grendel op de certificaatuitgifte, op onze eigen zones.
3. **De wekker** (RC-124) — een slapende deployment zonder bezoekers pollt niet meer onophoudelijk; tel de `status`-verzoeken per uur. En wie wél wacht, wacht niet langer dan voorheen.
4. **De restore-generatie** (RC-123) — twee restores achter elkaar geven een ANDERE doelnaam, en de rijen verdubbelen niet. Tel ze.
5. **De bijlagen** (RC-119) — een vervangen bijlage rolt zelf uit (202 met een task-id) en de pod krijgt de nieuwe inhoud werkelijk te zien.
6. **De slaapstand** (RC-119) — het tweede veld met de echte toestand, en `disabled` als sleep-mode uit staat, terwijl `state` byte-identiek blijft.
7. **De takenlijst** — een LOPENDE taak is aanklikbaar en leidt naar de voortgang; een afgeronde niet, want die is opgeruimd.
8. **Het statusfilter op /admin/approvals** — filtert, de gekozen waarde blijft na de swap staan, en het staat er niet dubbel.
9. **Het projectenoverzicht** — de projectcode als chip, de bewerkdatum ernaast, sorteren op laatst bewerkt beide kanten op, en het zoekveld houdt zijn breedte en de focus tijdens het typen.
10. **De metingen** — de grafieken staan alleen nog op het tabblad Metrics en niet meer ook op Deployments; een deployment op een ander cluster zegt waarom er niets te zien is.
11. **Dienst binden aan een component** — `POST /services` met `components` op een dienst die al op projectniveau staat, slaat op EN rolt uit.
12. **Het opslaan van een project met een versleuteld veld** — dat gaf permanent "Project is gewijzigd sinds je begon met bewerken" (RC-118). Doe dat een paar keer achter elkaar.

## Taak 4: het verslag

Eén document in `docs/`, met per taak wat er gemeten is en wat er misging. **Ook wat er misging.** Sluit af met een oordeel in één zin: deze tak kan naar main, of niet en dit is waarom.

Noem in dat oordeel expliciet of de tak tijdens de doorloop is blijven stilstaan.

## Wat er buiten valt

- Repareren, tenzij het de merge blokkeert; dan staat dat er met zoveel woorden bij.
- De mailrelay (RC-114): geparkeerd, zie `TODO_NEXT_RELEASE.md`.
- Productie.
