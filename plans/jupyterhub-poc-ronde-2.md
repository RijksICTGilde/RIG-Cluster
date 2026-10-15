# JupyterHub-POC ronde 2: van onderdelen naar een gebruiker in een eigen pod

Status: plan, 12 oktober 2026. Vervolg op RC-258 (PR #227), op dezelfde branch.

Ronde 1 leverde de onderdelen: een hub-image, een user-image, een referentie-projectbestand, een Role, 37 toetsen en een verslag. Op de sandbox draait nog niets: de hub-pod staat op ImagePullBackOff, en er is nog geen gebruiker ingelogd en geen user-pod gespawnd. Alle tien asserties staan op "niet gemeten".

Lees vooraf, want deze ronde bouwt erop voort en herhaalt het niet:

- `plans/samen-werken-in-jupyterhub-met-vscode.md`, het oorspronkelijke plan. De vraag, de tien asserties, de besluiten en wat erbuiten valt, gelden onveranderd. De taken 4 tot en met 10 daaruit zijn het werk van deze ronde.
- `docs/jupyterhub-poc-verslag.md`, wat ronde 1 gemeten heeft en waar het vastliep.

## Wanneer deze ronde af is

Dit ontbrak in ronde 1, en daardoor kon een verslag met tien keer "niet gemeten" door alle gates heen.

**De ronde is af als elke assertie 1 tot en met 10 in het verslag op groen of rood staat, met het bewijs erbij** (commando-uitvoer, schermafbeelding of log). Rood telt alleen met een gemeten oorzaak. "Niet gemeten" is geen geldige eindstand.

De enige uitzondering is een blokkade die een mens moet oplossen, bijvoorbeeld de sandbox die een uur onbereikbaar is, of een geheim dat niet in een worker hoort. Dan staat bovenaan het verslag een kop **Geblokkeerd**, met wat er precies blokkeert, wat er al geprobeerd is, en wat een mens moet doen. Een technisch probleem dat binnen deze taak op te lossen is, is geen blokkade.

Voor review: keur af als een assertie op "niet gemeten" staat zonder een benoemde blokkade.

## Wat ronde 1 al geleerd heeft, en wat dat nu betekent

### 1. Het image pullen gaat via `image-registries`, niet via een pull-secret met de hand

De push-API zet een image in de platformregistry, maar het project mag het daar niet uit halen: er is geen pull-secret (verslag, taak 3). De bedoelde weg is de dienst `image-registries` op het project (`features/image-registries.md`). Op de sandbox maakt ZAD dan een dockerconfigjson-secret in de namespace. De platformregistry van de sandbox mag daarbij als upstream (`_is_sandbox_registry`). De gegevens van die registry staan in `docs/sandbox-image-deploy-via-registry.md`.

Twee dingen moeten gemeten worden:

- Hoe heet het secret dat ZAD maakt, en hangt het aan de pod van het hubcomponent? Lees het af op het cluster.
- **De user-pods hebben het secret ook nodig.** KubeSpawner maakt die pods zelf, dus ze krijgen het niet van ZAD. Zet het in `c.KubeSpawner.image_pull_secrets`, met de naam uit een env var in het projectbestand, net als `JUPYTERHUB_POC_HUB_SERVICE`. Pin dat met een toets in `tests/test_jupyterhub_poc_project.py`.

Let ook op het verschil tussen het component en de deployment: `PATCH .../components/hub` met `image` zet niet het image dat draait. Daarvoor is `PUT .../deployments/{deployment}/image` met `componentName` nodig (verslag, eind taak 3).

### 2. Images naar de registry pushen, en meten waarom de sandbox omviel

Een image van 2,7 GB bouwen en draaien hoort op de sandbox gewoon te kunnen. Wat in ronde 1 misging, was niet de grootte maar de weg en de toestand van de host:

- De image ging als tarball via de push-API van OPI. nginx bufferde 2,8 GB naar schijf, en OPI deed er 214 s over.
- De load van de host stond boven de 30, en een andere sessie gebruikte 336% CPU.
- De control plane was al instabiel: de controller-manager stond op 364 restarts, en dat was niet allemaal van die middag.

De `docker save` was daarmee de druppel, maar de oorzaak is niet gemeten. Dat gebeurt in deze ronde:

- **Bouw en push rechtstreeks naar de registry**, zoals in `docs/sandbox-image-deploy-via-registry.md`. Daarbij reizen alleen de gewijzigde lagen, en de base één keer. De push-API van OPI is voor deze grootte de verkeerde weg, en een tarball (`docker save`, `kind load`) ook.
- **Meet de host tijdens de build en de push van de user-image.** Schrijf voor, tijdens en na op: `uptime`, `free -m`, swap, `iostat` of `vmstat 5` (I/O-wait), het geheugen van de containers van het kind-cluster (`docker stats --no-stream`), en het aantal restarts van `kube-apiserver`, `kube-controller-manager` en `kube-scheduler`. Bekijk ook de log van etcd op trage requests (`took too long`). De vraag die het verslag moet beantwoorden: valt de control plane om door geheugen (OOM of swap), door schijf-I/O (etcd fsync), of door CPU, en is dat ook zonder deze taak al zo?
- Blijft de control plane bij de registry-weg overeind, dan is dat het antwoord. Valt hij toch om, dan is dat een bevinding over de sandbox met de metingen erbij. Ga dan door zodra hij terug is, en zet het in `TODO.md`.
- **Stuur API-aanroepen naar OPI één voor één**, niet vlak na elkaar (verslag, taak 3).

### 3. De user-image kleiner maken: meten, geen eis

Met de registry-weg is 2,7 GB geen blokkade meer: de kubelet van de ene kind-node pullt hem één keer. Kleiner is wel beter voor elke latere node en elke update. Meet per laag waar de grootte zit (`docker history`). Probeer de goedkoopste winst: een kleinere base (`base-notebook` in plaats van `minimal-notebook`, als er niets wegvalt wat we nodig hebben), en code-server zonder wat we niet gebruiken. Schrijf de getallen voor en na in het verslag. Haal daarvoor geen functie weg die een assertie nodig heeft.

### 4. Nieuwe valkuil, nog niet gemeten: de PVC dekt de home af

`images/jupyterhub-user/Dockerfile` zet de code-server-instellingen (waaronder `chat.disableAIFeatures`, die het Copilot-paneel uitzet) en de geïnstalleerde extensies in `/home/jovyan`. KubeSpawner mount de PVC van de gebruiker standaard op diezelfde home. Gebeurt dat, dan zijn bij de eerste spawn de extensies weg en het Copilot-paneel terug. Lokaal met `docker run` is dat niet te zien, want daar is geen PVC.

Meet het bij de eerste spawn. Klopt het, zet de instellingen en extensies dan buiten de home: de extensie- en data-map van code-server op een pad in de image, of een machine-niveau-instelling. Toets dat het paneel weg blijft en dat de extensies er na een herstart nog zijn.

### 5. Open meetvraag uit ronde 1: `pods/exec`

`kubectl auth can-i get pods/exec --as=...` gaf `yes`, terwijl de Role alleen `pods` noemt. Meet het met een `SubjectAccessReview` met een expliciete `subresource`, en daarna met een echte `exec` vanuit de hub-pod met het token van de ServiceAccount. Dit hoort bij assertie 6 en bij taak 9, en mag niet als vermoeden blijven staan.

### 6. Al gerepareerd in ronde 1, nu op het cluster meten

`cleanup_servers = False`: een rollout van de hub mag de draaiende user-pods niet stoppen. Dit is pas te meten met een spawn die slaagt, en hoort bij assertie 9.

## Taken

Gebruik `orch sandbox claim` aan het begin en `orch sandbox release` aan het eind. Controleer per blok de context (`kind-rig-sandbox`), de draaiende OPI-build (`/version`) en de load.

1. **De hub draait.** `image-registries` op het project, de hub-image naar de registry, het image op de deployment gezet via de API. Verifieer: de hub-pod is Running, en `https://<host>/hub/` geeft de loginpagina. Werk `projects/ideas/jupyterhub-poc.yaml` bij met de dienst en de nieuwe env var.
2. **De user-image staat in de registry**, eventueel kleiner (punt 3), met `image_pull_secrets` in de hub-config (punt 1). Meet daarbij de host zoals in punt 2. Verifieer: `docker push` slaagt, de getallen voor en na staan in het verslag, en het verslag zegt waarom de control plane in ronde 1 omviel, of dat het met de metingen niet te zeggen is en wat er dan nog gemeten moet worden.
3. **De eerste spawn.** Gebruiker A logt in en krijgt een eigen pod. Meet punt 4 en repareer als het klopt. Verifieer: assertie 1, met schermafbeeldingen van JupyterLab en VS Code in de pod van A, zonder Copilot-paneel.
4. **De taken 4 tot en met 8 uit het oorspronkelijke plan**, in die volgorde: de rechten van ruim naar minimaal (met punt 5), inloggen, guard en verwijderen, isolatie, samenwerken, Git en data, culling en ArgoCD (met punt 6). Elke taak eindigt met een meting in het verslag.
5. **Taak 9 en 10 uit het oorspronkelijke plan.** De beschrijving van wat ZAD nodig heeft, nu met de gemeten Role. Het verslag met elke assertie groen of rood. Het feature-doc en `TODO.md` bijwerken. Haal TODO-punten weg die deze ronde heeft opgelost, en zet er nieuwe bij voor wat deze ronde heeft gevonden.

## Wat erbuiten valt

Hetzelfde als in het oorspronkelijke plan: geen wijzigingen aan OPI, geen VLAM, geen code.overheid.nl, geen productie of ODCN, geen helm. Een bug in ZAD die je onderweg vindt, zoals de lege Template in de snelstart "Toegangsbeperking" uit ronde 1, schrijf je op in het verslag en in `TODO.md`. Je repareert hem niet.

## Na de ronde

- `orch sandbox release`.
- Laat het project `jp-574` (of het project dat je in deze ronde gebruikt) op de sandbox staan. Zet in het verslag welke het is en hoe je het opruimt.
- Zet de stand bovenaan het verslag op "ronde 2", met de datum.
