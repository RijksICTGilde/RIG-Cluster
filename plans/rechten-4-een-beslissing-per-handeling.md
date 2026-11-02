# Rechten, taak 4: één beslissing per handeling, en een commitlink die je kunt openen

Opdracht: zorg dat het portaal een handeling één keer autoriseert, zodat de audit ook één rij per handeling krijgt, en laat de commitlink in het audit-tabblad alleen wijzen naar een adres dat een gebruiker in de browser kan openen. Wat mag en niet mag verandert niet.

Bouwt op taak 1 tot en met 3 (RC-392, RC-393, RC-397), die op main staan. Ontwerp: `plans/rechten-op-paden-tad-en-wies.md`. De audit: `features/audit.md`.

## Waarom

Elke route in `opi/web` gaat door de poort `enforce_route_action` (`opi/web/authz.py`, als router-dependency op de routers). Zes handlers roepen daarna in hun body nog een keer `authorize` aan met **dezelfde** action. Dat is een tweede beslissing over dezelfde vraag, en sinds taak 3 schrijft de audit een rij per beslissing (`request.state.authz_decisions`, `opi/middleware/audit.py`). Gemeten in de review van RC-397:

```
DELETE /projects/demo/bijlagen/...   beslissingen=2  auditrijen=2
  -> action=attachment.manage allowed=True resource=project/demo
  -> action=attachment.manage allowed=True resource=project/demo
```

De zes plekken:

| plek | action |
|---|---|
| `opi/web/router_attachments.py:37` | `attachment.manage` |
| `opi/web/router_detail_edit.py:610` | `project.edit` |
| `opi/web/router_env_vars.py:118` | `env-vars.manage` |
| `opi/web/router_env_vars.py:170` | `env-vars.manage` |
| `opi/web/router_wizard.py:573` | `project.edit` |
| `opi/web/router_tasks.py:121` | `task.read` (bij een weigering twee weigerrijen) |

Daarnaast twee GET-routes die alleen een keuzelijst in een modal herrenderen, met `@action(Action.PROJECT_READ)` op de route en in de handler een `authorize` op een schrijfactie. Die leveren een rij `backup.create` of `restore.run` op terwijl er niets gemaakt of hersteld is:

| plek | route-action | handler-action |
|---|---|---|
| `opi/web/router_detail_edit.py:1229` (`backup_select_deployment`) | `project.read` | `backup.create` |
| `opi/web/router_detail_edit.py:1258` (`restore_select_mode`) | `project.read` | `restore.run` |

De commitlink: `commit_adres` in `opi/web/lotc_switch.py` bouwt hem uit `GIT_PROJECTS_SERVER_URL`. Dat is de kloon-URL. In productie en staging is dat een github.com-adres en werkt de link, maar in de sandbox is het `http://forgejo.rig-system.svc.cluster.local:3000/...`, een adres binnen het cluster dat geen browser kan openen.

## Wat er moet gebeuren

### 1. De poort beslist, de handler leest

De poort is de grens. Een handler die dezelfde action nog eens vraagt, voegt niets toe. Haal op de zes plekken de tweede `authorize` weg.

Wat de handler uit `authorize` haalde (het project, het e-mailadres), haalt hij uit wat de poort al heeft. Laat de poort het toegelaten project op het verzoek zetten (VOORSTEL: `request.state.authz_project`) en geef een kleine helper die het teruggeeft. Zoek eerst of er al zo'n helper is voordat je er een schrijft.

**Let op bij de env-vars-routes.** De handler geeft `deployment=` en `component=` mee. Op de componentroute (`/projects/{p}/componenten/{c}/env-vars`) staat de deployment niet in het pad maar in de query, en de poort leest alleen `request.path_params`. Daar weegt de handler dus een SMALLERE resource dan de poort, en dat verschil is in RC-392 een securitybevinding geweest (een developer kreeg zo de ontsleutelde waarden van een component dat alleen in productie draait). Laat de poort de deployment en het component ook uit de query lezen als ze niet in het pad staan, en haal pas daarna de tweede aanroep weg. De poort mag nooit ruimer beslissen dan de handler nu doet.

**Een guard tegen terugkeer.** Een test die over `opi/web` loopt en faalt als een handler met `@action(X)` in zijn body `authorize(..., action=X, ...)` aanroept. Een andere action in de body blijft toegestaan (bijvoorbeeld `router_wizard.py:918`, waar de poort `wizard.use` weegt en de handler `project.edit` op een bestaand project: dat zijn twee vragen).

*Bewijs:* per route een test die één verzoek doet en precies één beslissing in `request.state.authz_decisions` ziet, toegestaan en geweigerd. Voor de env-vars-componentroute: een developer krijgt 403 op een component dat alleen in een productiedeployment zit, met de deployment in de query, net als nu.

### 2. Een keuzelijst herrenderen is lezen

`backup_select_deployment` en `restore_select_mode` doen geen backup en geen herstel. Hun vraag is "mag deze gebruiker dit formulier zien". Laat de handler daarvoor `may(...)` gebruiken (`opi/web/authz.py`, beslist zonder vast te leggen) en zelf een 403 geven als het niet mag, of geef de route de leesaction die bij het formulier hoort (`backup.read`, `restore.read`) en laat de handler de tweede `authorize` weg. Kies wat het minst code vraagt en zeg in de PR waarom.

De echte handeling, het POST dat de backup maakt of het herstel start, houdt zijn schrijfaction en zijn rij.

*Bewijs:* een GET op elk van beide routes levert geen auditrij met `backup.create` of `restore.run`; een viewer die die schrijfaction niet heeft krijgt er nog steeds geen formulier.

### 3. Een commitlink alleen naar een webadres

`commit_adres` geeft alleen een link als er een adres is dat een browser kan openen. Voeg een instelling toe voor het webadres van de projecten-repo (VOORSTEL: `GIT_PROJECTS_WEB_URL`). Is die gezet, dan komt de link daaruit. Is hij leeg, dan blijft de afleiding uit `GIT_PROJECTS_SERVER_URL` zoals nu, maar niet voor een host die op `.svc.cluster.local` of `.cluster.local` eindigt: daar geen link, alleen de SHA als tekst.

Zet in de sandbox-overlay (`bootstrap/rig-system/kustomize/operations-manager/overlays/sandboxed-local/configmap.yaml`) het webadres op `https://forgejo.sandbox.rijksapp.dev/rig-admin/zad-projects`; dat is dezelfde host als `PROJECT_REPO_URL` in die configmap. Productie en staging hoeven niets, daar werkt de afleiding al.

*Bewijs:* `commit_adres` met een cluster-interne server-URL en geen webadres geeft leeg; met een webadres geeft hij `<webadres>/commit/<sha>`; met de huidige github.com-URL ongewijzigd.

### 4. Documentatie

Werk `features/audit.md` bij: een handeling levert één rij; een herrendering van een formulier is geen handeling; waar de commitlink vandaan komt. Zet de nieuwe instelling bij de configuratie.

## Wat er buiten valt

- De regels in `opi/core/authz/rules.py`, behalve wat stap 2 nodig heeft.
- De API-kant (`opi/api`): de review van RC-397 vond de dubbele beslissing alleen in `opi/web`. De upsert met `cloneFrom` neemt twee beslissingen over twee VERSCHILLENDE actions, en dat blijft zo.
- Een dedupe in de audit-hook. Die kan een bewust tweede besluit niet van een overbodig besluit onderscheiden; de oorzaak wordt hier weggenomen, niet het gevolg.

## Wat niet mag

- Geen OPI uitrollen.
- De poort mag op geen enkele route ruimer beslissen dan de route nu doet.

## Verifieerbaar

1. `uv run ruff check . --fix && uv run ruff format . && uv run pyright` schoon in `operations-manager/python`.
2. De guard uit stap 1 staat en faalt op een handler die dezelfde action nog eens vraagt.
3. Op de acht routes uit de tabellen hierboven precies één beslissing per verzoek.
4. De bestaande suite groen, inclusief de audit- en matrixtests van taak 1 en 3.
