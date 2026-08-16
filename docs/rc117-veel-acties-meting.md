# Veel acties op één project: waar de tijd heen gaat

Onderzoek bij RC-117. De vraag was of het commit-en-push per handeling ons in de weg
zit wanneer een agent tien acties achter elkaar op één project afvuurt, en wat we
mogen samenvoegen zonder iets stuk te maken. Het plan zei: eerst meten, dan ontwerpen.

Dat is gedaan. **De uitkomst is dat de push niet het probleem is, en dat het middel dat
het probleem wél oplost al bestaat.** Tien acties met `rollout=false` gevolgd door één
`:refresh` waren in de meting elf keer sneller dan tien gewone acties, zonder één regel
nieuwe code.

## Hoe er gemeten is

Twee metingen, beide herhaalbaar:

| Wat | Waarmee | Wat het isoleert |
|---|---|---|
| Het schrijfpad van het projectbestand | `operations-manager/python/scripts/bench_project_writes.py` | De echte `GitProjectStore` tegen een echte git-repo, zonder netwerk. De vloer. |
| De hele keten | `tests/e2e/test_sandbox_veel_acties.py` (`-m "e2e and sandbox"`) | Tien acties via de echte v2-API tegen een draaiend sandboxcluster, plus de OPI-logs eronder. |

De sandboxmeting draaide op commit `cf3b2346`, op een Kind-sandbox met één project met
één deployment. De ruwe getallen en het log-uittreksel waar de opsplitsing per stap uit
komt staan in `docs/rc117-metingen/`. Die opsplitsing komt dus uit de tijdstempels van
het cluster zelf, niet uit een schatting.

Eén waarschuwing vooraf: de ArgoCD-wachttijden hieronder zijn die van een drukke Kind-
sandbox. De verhouding tussen de stappen is wat telt, niet de absolute seconden.

## Vraag 1: waar gaat de tijd heen?

Eén `add_component` die uitrolt, gemeten aan de tijdstempels in de log (taak 97,9s):

| Stap | Duur | Aandeel |
|---|---|---|
| Projectbestand valideren, committen en pushen | 1,6s | **1,6%** |
| Manifesten genereren, twee repo's klonen en pushen | 8,1s | 8,3% |
| Wachten tot ArgoCD gesynchroniseerd en gezond is | 88,0s | **90,0%** |

Drie uitgerolde acties gaven 23,8s, 97,9s en 86,2s (gemiddeld 69,3s), met steeds
dezelfde vorm: de git-schrijfactie constant rond 1,6s, de manifestfase rond 8s, en al
het overige in de ArgoCD-wachttijd. Het verschil tussen 23,8s en 97,9s zit volledig in
die wachttijd — dat is het cluster dat containers moet starten, niet iets van ons.

**De push waar de vraag over ging is 1,6% van een actie.** Wie hem weghaalt, wint niets.

Binnen die 1,6s is de push wél het meeste: `store-persist` 1,42s, waarvan `store-push`
1,26s. Maar dat is 1,26 seconde in een handeling van anderhalve minuut.

## Vraag 2: zet elke actie een eigen werkboom op?

Deels — en het is goedkoop.

* **`zad-projects`**: nee. De `ProjectStore` houdt één warme werkkopie per proces. In de
  hele meting is er precies één keer gekloond, bij het opstarten. De tien uitgestelde
  acties kloonden nul keer.
* **`zad-deployments` en `zad-argo-user-applications`**: ja, elke uitrollende actie
  kloont beide opnieuw (`ProjectManager.close()` ruimt ze weer op).

Maar die klonen zijn `--depth 1` en staan in het cluster: gemeten **123ms en 56ms**. Samen
0,2% van de actie. Het vermoeden dat elke actie een werkboom opzet klopt dus, maar de
kostenpost die erachter werd vermoed is er niet.

Wat de manifestfase van 8s wél vult is het renderen van de manifesten, per component.
Dat schaalt met het aantal componenten in de deployment, niet met het aantal acties.

## Vraag 3: helpt wachten op stilte?

Nee, want er is niets om op te wachten. In alle negentien schrijfacties van de meting was
de wachttijd op het slot van de store **0,00s**. De acties botsten niet op elkaar: ze
stonden in de takenwachtrij achter elkaar, en elke schrijfactie was al klaar voordat de
volgende begon. Een debounce-venster van twee of drie seconden zou hier alleen
vertraging toevoegen aan iets dat niet in de weg zat.

## Groeit dit mee met de repo?

Nee. De lokale benchmark, tien schrijfacties op één project:

| Repo | Per schrijfactie | Push | `previous()` voor de prune-diff | Bootstrap (1× per proces) |
|---|---|---|---|---|
| 20 projecten, 51 commits | 0,108s | 0,022s | 0,017s | 0,22s |
| 200 projecten, 2001 commits | 0,104s | 0,021s | 0,019s | 1,73s |

Veertig keer zoveel geschiedenis en tien keer zoveel projecten veranderen niets aan de
kosten per schrijfactie. Alleen het opstarten wordt duurder, en dat betaal je één keer
per proces. De 1,26s push op de sandbox tegenover 0,02s hier is dus geen repo-omvang
maar de netwerkrondgang naar Forgejo: een constante, geen groeiende post.

## De vier richtingen uit het plan, gewogen tegen de meting

**A. Alleen de push uitstellen — afgeraden, en gevaarlijker dan het lijkt.**
Het levert hooguit 1,26s per actie op, en het botst met twee bestaande mechanismen die
allebei niet-gepushte commits weggooien:

1. `_commit_and_publish` telt aan het begin van élke schrijfactie de niet-gepushte
   commits en gooit ze weg (`reset_to_remote`) voordat het verder bouwt — bedoeld als
   zelfherstel na een mislukte rollback;
2. `reconcile()` draait elke `PROJECT_STORE_RECONCILE_INTERVAL_SECONDS` (standaard 300s),
   ziet lokaal ≠ remote, en doet `fetch` + `reset --hard origin/main`.

Een ontwerp dat lokaal commit en later pusht verliest zijn commits dus langs twee
onafhankelijke wegen, nog vóór een crash in beeld komt. Dat is te repareren, maar je
betaalt echt onderhoud aan de kern van de opslag voor 1,6% winst.

**B. Commits samenvoegen — niet doen, en niet nodig.**
Het herschrijft geschiedenis waar de prune-stap op leunt (`previous()` zoekt de vorige
versie van dít bestand via `git log -- <pad>`), en het levert hetzelfde niets op als A.

**C. Samenvoegen in de takenwachtrij — het juiste idee, maar het bestaat al in een
betere vorm.** Wat samenvoegen zou opleveren is precies wat `rollout=false` oplevert:
één keer verwerken in plaats van tien keer. En `rollout=false` doet het zonder te moeten
bewijzen dat twee taken samen te voegen zijn zonder hun volgorde of hun afzonderlijke
uitkomst te veranderen — elke taak houdt zijn eigen commit, zijn eigen uitkomst en zijn
eigen plek in de volgorde. Alleen het uitrollen wordt opgespaard.

**D. Niets aan git, wel aan wat eromheen zit — dit is het antwoord.**
De 90% zit in de ArgoCD-wachttijd. Die haal je niet weg door hem over te slaan (dan weet
niemand of het gelukt is), maar door hem één keer te betalen in plaats van tien keer.

## De meting van de aanbeveling zelf

Hetzelfde werk, op hetzelfde project, op hetzelfde cluster:

| | Client-tijd | Server-tijd |
|---|---|---|
| 10× `add_component` (standaard, `rollout=true`) | 735s (geprojecteerd uit n=3) | ~693s |
| 10× `add_component?rollout=false` + 1× `:refresh` | **67,4s** | ~41,3s |

Elf keer sneller. De uitgestelde acties kostten 3,05s elk aan de clientkant tegenover
1,6s serverwerk; het verschil is de pollinterval van 3s waarmee de client de taak
opvraagt, niet werk. Wie sneller polt, ziet die 1,6s.

De `:refresh` verwerkte alle tien wijzigingen in één taak van 25,3s, met twee klonen in
plaats van twintig.

## Wat de vier valkuilen uit het plan doen bij deze aanbeveling

**Het taakresultaat blijft een echte belofte.** Dit is de belangrijkste uitkomst. Met
`rollout=false` is de taak pas klaar als het projectbestand gevalideerd, gecommit én
gepusht is — precies de belofte van vandaag. Wat níet meer geldt is dat de wijziging op
het cluster staat, en dat wordt expliciet verteld in plaats van stilgezwegen:

* de taakuitkomst draagt `processing: {"status": "skipped", "reason": "rollout_disabled"}`
  met een bericht dat naar `:refresh` verwijst;
* `GET /api/v2/projects/{p}/pending-rollout` telt wat er wacht en sinds wanneer, en elk
  leesendpoint draagt dat mee.

De meting toetst dit ook echt: na tien uitgestelde acties stonden alle tien componenten
in het projectbestand in Forgejo, meldde `pending-rollout` er tien, en zette één
`:refresh` die teller op nul. Snelheid is hier dus niet geruild tegen een leugen. Dat is
precies het verschil met richting A, waar "klaar" zou gaan betekenen "staat lokaal, komt
misschien nog".

**De ProjectStore per proces is geen bezwaar meer.** De aanbeveling voegt geen
wachtvenster toe dat in één proces zou leven, dus er is niets dat bij twee replica's
stukgaat. Dat de store per proces is blijft waar — hij is er ook eerlijk over — maar
`rollout=false` verandert daar niets aan: het slot serialiseert nog steeds precies wat
het eerst serialiseerde. Bij een tweede replica gelden dezelfde beschermingen als nu:
een niet-fast-forward push die de schrijver dwingt opnieuw te lezen, opnieuw toe te
passen en opnieuw te valideren.

**De prune-stap blijft werken.** Er wordt geen commit samengevoegd of overgeslagen: elke
actie houdt zijn eigen commit, dus `previous()` vindt nog steeds de versie van vlak voor
de wijziging. Dit is precies wat richting B kapot zou hebben gemaakt.

**Uit elkaar lopen wordt niet erger.** Er ontstaat geen venster waarin onze werkkopie
vooruitloopt op de remote, want er wordt niets uitgesteld aan de push. Wat wél vooruit
gaat lopen is het projectbestand op het cluster — maar dat is zichtbaar
(`pending-rollout`), gevraagd, en met één aanroep in te halen.

**Verliezen kan niet.** Valt het proces om tussen twee acties, dan is elke afgeronde
actie al gepusht. Wat er hoogstens verloren gaat is de nog niet gedane `:refresh`, en die
is idempotent: hem opnieuw aanroepen verwerkt gewoon wat er in het bestand staat.

## Aanbeveling

1. **Bouw niets aan het samenvoegen van commits of het uitstellen van pushes.** De
   meting laat zien dat daar 1,6% van de tijd zit, en richting A vecht bovendien tegen
   twee bestaande mechanismen die niet-gepushte commits opruimen.
2. **Laat de zad-cli `rollout=false` gebruiken voor een reeks handelingen, en één
   `:refresh` aan het eind.** Dat is de elf-voudige winst, vandaag, zonder nieuwe code.
   De vlag en de regels eromheen staan in `features/opslaan-zonder-verwerken.md`.
3. **Wil je daarna nog wat**, kijk dan naar de manifestfase (8s, ~8%), niet naar git:
   die schaalt met het aantal componenten. Warme werkkopieën voor `zad-deployments` en
   `zad-argo-user-applications`, zoals de `ProjectStore` er al één heeft, besparen daar
   hoogstens de 0,2s kloontijd — de rest is het renderen zelf.
4. **Verandert er iets aan het gedrag voor een client?** Alleen dit: wie `rollout=false`
   meegeeft krijgt een taak die klaar is als het bestand in git staat, met
   `processing.status = "skipped"` en een `pending_rollout`-teller die zegt hoeveel er
   nog wacht. Dat is geen nieuw contract, het is het bestaande contract dat nu ook echt
   gebruikt wordt.

## De metingen herhalen

```bash
cd operations-manager/python

# schrijfpad, zonder netwerk, met twee repo-groottes
uv run python scripts/bench_project_writes.py --actions 10 --projects 20  --history 50
uv run python scripts/bench_project_writes.py --actions 10 --projects 200 --history 2000

# de hele keten, tegen een draaiende sandbox
E2E_BASE_URL=https://zad.sandbox.rijksapp.dev \
E2E_SECRET_KEY=<de SECRET_KEY van het cluster> \
FORGEJO_URL=https://forgejo.sandbox.rijksapp.dev \
FORGEJO_USER=rig-admin FORGEJO_PASSWORD=admin1234 FORGEJO_VERIFY_SSL=false \
uv run pytest tests/e2e/test_sandbox_veel_acties.py -m "e2e and sandbox" -o addopts="" --timeout=3600
```

De opsplitsing per stap komt uit de OPI-log naast die run:

```bash
kubectl -n rig-system logs deploy/operations-manager --since=40m \
  | grep -E "store-persist|store-push|store-lock|Cloning repo|synced and healthy|completed successfully in"
```
