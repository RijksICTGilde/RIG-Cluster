# Samen werken in JupyterHub met VS Code, als ZAD-component

Status: plan, 12 oktober 2026. Niet gebouwd. Langlopend: dit is een puzzeltaak in rondes, niet een PR die in één keer af is.

Aanleiding: `features/online-ontwikkelomgeving-trainees.md`. Een groep mensen werkt aan hetzelfde project, deelt code via Git en eventueel datasets, en logt in via SSO Rijk. Ze werken in JupyterLab, en vanuit JupyterLab openen ze VS Code. Op een managed device is alleen een browser nodig.

## De vraag die deze taak beantwoordt

Kan JupyterHub met KubeSpawner draaien als **gewoon ZAD-component**, zonder helm en zonder helmfile, op de bestaande ZAD-tooling? En welke extra rechten heeft het daarvoor nodig, precies en niet meer?

Het antwoord is een werkende opzet op het sandboxcluster, plus een verslag. Rood is hier informatie: een stap die niet lukt, wordt opgeschreven met de oorzaak, en is geen reden om te stoppen of om eromheen te bouwen.

## Wat het moet aantonen

1. Een gebruiker met de juiste rol logt in via Keycloak, en de hub start een eigen pod voor die gebruiker. In die pod draait JupyterLab, en vanuit de launcher opent VS Code (code-server).
2. Een gebruiker zonder de rol komt er niet in. Een gebruiker die de rol kwijtraakt, komt er daarna niet meer in.
3. Twee gebruikers werken samen: de een deelt zijn server met de ander, en ze bewerken tegelijk hetzelfde notebook.
4. Code gaat via `git push` naar de Forgejo van de sandbox. Wat niet gecommit is, blijft op de eigen PVC en overleeft het stoppen en opnieuw starten van de server.
5. Beide gebruikers lezen dezelfde dataset uit een MinIO-bucket van het project.
6. Vanuit een user-pod lukt `kubectl auth can-i create pods` niet, en er is geen ServiceAccount-token gemount.
7. Een user-pod kan niet bij pods van andere projecten. Wat hij binnen het eigen project bereikt, staat gemeten in het verslag.
8. Een inactieve server wordt opgeruimd door de idle-culler. Een gebruiker die via het adminpaneel van de hub wordt verwijderd, verliest ook zijn pod en zijn PVC.
9. ArgoCD blijft Synced en Healthy terwijl er user-pods komen en gaan, en een sync met prune laat de user-pods staan.
10. De minimale Role die de hub nodig heeft, staat als YAML in de repo, met per regel waarom hij nodig is, gemeten door rechten weg te halen en te kijken wat er stukgaat.

## Wat al besloten is

- **Geen helm, geen helmfile.** De hub is een ZAD-component met een eigen image. Reden: we willen weten wat we draaien, en het moet passen op de ZAD-tooling (services, Keycloak, netpols, Argo).
- **JupyterHub + KubeSpawner + GenericOAuthenticator**, de standaard open-source onderdelen. We bouwen geen eigen spawner of authenticator.
- **JupyterLab als voorkant, VS Code als tegel** via `jupyter-vscode-proxy`, in dezelfde user-pod.
- **Een eigen project voor deze POC.** Elk component in een project gebruikt dezelfde ServiceAccount (`manifests/deployment.yaml.jinja:54`, `manifests/project-serviceaccount.yaml.jinja`). De rechten die de hub krijgt, krijgt dus elk component in dat project.
- **De Role en RoleBinding worden in deze taak met de hand toegepast** op de sandbox, buiten GitOps. Dat is een bewuste, tijdelijke uitzondering. OPI wordt in deze taak niet gewijzigd.
- **Datasets in MinIO, niet op een gedeelde schijf.** ZAD maakt alleen RWO-volumes (`opi/core/cluster_config.py:51`, `:227`).
- **Git naar de Forgejo van de sandbox**, met een personal access token per gebruiker. code.overheid.nl valt hierbuiten.

## Vooraf: de feiten die al gemeten zijn

**Het cluster.** Het gedeelde sandboxcluster op de dev-server (`docs/sandbox-on-dev-server.md`), op `https://zad.sandbox.rijksapp.dev`. `*.sandbox.rijksapp.dev` resolveert naar `127.0.0.1`, dus meet altijd vanaf de server. Claim het slot met `orch sandbox claim` en geef het vrij met `orch sandbox release` aan het eind van elke ronde. Controleer `kubectl config current-context` (moet `kind-rig-sandbox` zijn) en gebruik `security/sandbox-key.txt`, nooit de productiesleutel. Controleer per blok, niet alleen aan het begin, dat de context en de draaiende OPI-build (`/version`) nog kloppen.

**Images.** Project-images gaan naar de in-cluster `rig-registry`, zie `docs/sandbox-image-deploy-via-registry.md` en `docs/sandbox-kind-registry.md`. Zet elke versie vast (base-image-tag, code-server-versie, pip-versies, extensieversies). Gebruik geen `latest`.

**Keycloak in ZAD.** De keycloak-service zet `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `OIDC_DISCOVERY_URL`, `OIDC_URL`, `OIDC_REALM` en `OIDC_HOSTNAME` op het component (`opi/services/catalog/keycloak/variables.py`). Als toegangsguard bestaan `realm-roles` + `restrict-access` (`features/keycloak-realm-roles.md`, `features/client-access-restriction.md`). Zoek uit of de invite-service (`opi/services/catalog/invite/`) de rol meegeeft, of dat die met de hand in Keycloak moet.

**Netwerk.** De tenant-baseline NetworkPolicy selecteert per deployment op het podlabel `deployment: <deploymentnaam>` (`manifests/tenant-baseline-network-policy.yaml.jinja`, gevuld in `opi/manager/project_manager.py:7051`). Pods zonder dat label vallen erbuiten en zijn dan default-allow. De bedoeling is dat KubeSpawner met `extra_labels` op elke user-pod hetzelfde label `deployment` zet als de hub.

**Valkuil bij de labels.** Zet op user-pods **niet** het label `app: <hubcomponent>`. De Service en de Deployment van de hub selecteren op `app` (`manifests/deployment.yaml.jinja:7`, `:27`). Een user-pod met dat label komt in de loadbalancing van de hub terecht, en de Deployment rekent hem mogelijk tot zijn eigen pods.

**Orphan-sweep.** De reconciliatie ruimt manifests in git op, geen live pods (`opi/jobs/reconciliation.py:789`). Of er nog een ander pad is dat pods zonder git-manifest opruimt: meten, niet aannemen (`features/service-orphan-reconciliation.md`, `features/manifest-inventory-cleanup.md`).

**AppProject.** Cluster-scoped is dicht, namespaced is open (`manifests/argocd-appproject.yaml.jinja`). Een Role en RoleBinding zouden via Argo dus mogen. ZAD heeft alleen nog geen weg om ze te genereren.

## Taken

Elke taak eindigt met een meting. Schrijf de uitkomst in het verslag (taak 10), ook als die rood is.

### 1. Hub-image

Een image op basis van `quay.io/jupyterhub/jupyterhub:<vaste tag>` met `jupyterhub-kubespawner` en `oauthenticator`. De `jupyterhub_config.py` zit in de image en leest alles wat per project verschilt uit env vars (de `OIDC_*` hierboven, de MinIO-variabelen, de namespace). Een bijlage is niet nodig zolang env vars volstaan. Controleer of `configurable-http-proxy` in de base-image zit: de hub draait de proxy dan als subproces in dezelfde pod.

De config regelt minimaal:

- `GenericOAuthenticator` op het realm van het project. Callback `https://<host>/hub/oauth_callback`.
- `KubeSpawner` in de eigen namespace, met een PVC per gebruiker op de standaard StorageClass van het cluster.
- `extra_labels` met `deployment: <deploymentnaam>`, niet `app` (zie de valkuil).
- Op user-pods: `automount_service_account_token = False`, `runAsNonRoot`, `allowPrivilegeEscalation: false`, alle capabilities gedropt, `seccompProfile: RuntimeDefault`. Neem `manifests/pod-security-context.yaml.jinja` als maatstaf.
- `hub_connect_url` naar de ClusterIP-Service van het hubcomponent, op de API-poort van de hub (8081).
- De idle-culler (`jupyterhub-idle-culler`) als hub-service.
- Delen (JupyterHub 5 sharing) aan voor de gebruikers met de rol.
- De MinIO-variabelen van het hubcomponent doorgeven aan de user-pods.

Verifieer: `docker run` van de image start, en `jupyterhub --version` en `python -c "import kubespawner, oauthenticator"` slagen.

### 2. User-image

Een image op basis van `quay.io/jupyter/minimal-notebook:<vaste tag>` met `jupyter-collaboration`, `jupyter-vscode-proxy`, code-server, en van Open VSX de Python-extensie en basedpyright. Geen AI-extensies, VLAM valt buiten deze taak. `git` zit al in de base-image, controleer dat.

Verifieer: lokaal `docker run`. JupyterLab opent, de VS Code-tegel opent code-server, en daarin opent een terminal.

### 3. Het projectfile

Een projectfile voor een eigen POC-project, met één deployment en één component: de hub. Diensten: keycloak (met een realm-rol als guard), publish-on-web, persistent-storage voor `jupyterhub.sqlite`, en minio. Poorten: de proxy-poort (8000) naar het web, en de API-poort (8081) binnen de namespace. Zoek uit wat publish-on-web doet bij twee inbound-poorten: welke gaat naar de ingress?

Leg het projectfile vast in de repo. Pad als voorstel: `projects/ideas/jupyterhub-poc.yaml`. Rol het uit via de API of de UI van de sandbox-OPI, niet met de hand.

Verifieer: Argo is Synced en Healthy, `https://<host>/hub/` geeft de loginpagina, en de hub-log toont geen spawner-fouten bij het opstarten.

### 4. De rechten, van ruim naar minimaal

Begin met een Role in de eigen namespace op `pods`, `persistentvolumeclaims` en `events` (create, delete, get, list, watch, patch waar nodig), plus een RoleBinding naar de ServiceAccount van het project. Pas die met de hand toe. Haal daarna regel voor regel rechten weg en kijk wat er stukgaat: spawnen, stoppen, de PVC opruimen, de status lezen. Doet KubeSpawner ook iets met `services` of `secrets`? Meten.

Leg de minimale Role vast als YAML met per regel waarom. Pad als voorstel: `features/jupyterhub-poc/hub-role.yaml`. Argo past deze YAML niet toe, hij is documentatie en de basis voor een vervolgtaak.

Verifieer: `kubectl auth can-i --as=system:serviceaccount:<ns>:<sa> ...` per werkwoord, en een spawn die slaagt met precies deze Role.

### 5. Inloggen, guard en verwijderen

Drie gebruikers in de Keycloak van de sandbox: A en B met de rol, C zonder. Zoek uit hoe je ze aanmaakt op de manier die ZAD bedoelt (invite, of met de hand), en schrijf op welke het is.

Verifieer, met schermafbeeldingen (`operations-manager/python/scripts/kijk_sandbox.py`):

- A en B komen in hun eigen pod, met JupyterLab en VS Code.
- C wordt door Keycloak geweigerd.
- Haal de rol weg bij B. Een nieuwe login van B wordt daarna geweigerd. Wat gebeurt er met de server van B die nog draait? Opschrijven.
- Verwijder B in het adminpaneel van de hub: de pod en de PVC van B zijn weg.

### 6. Isolatie

Verifieer vanuit de pod van A:

- `ls /var/run/secrets/kubernetes.io/serviceaccount` bestaat niet.
- Een verbinding naar een pod in een ander project lukt niet (de sandbox heeft er genoeg).
- Wat A bereikt binnen het eigen project: de hub, de pod van B, MinIO, Forgejo. Meet het en schrijf het op. Dat A de pod van B bereikt, is in deze opzet verwacht: het valt onder dezelfde deployment-netpol. Opschrijven is genoeg, oplossen hoort niet bij deze taak.

### 7. Samenwerken, Git en data

- A deelt de server met B. B opent hetzelfde notebook, en een wijziging van de een verschijnt bij de ander.
- A maakt in de Forgejo van de sandbox een token en een repo, clonet in VS Code, commit en pusht. Een bestand dat niet gecommit is, staat er nog na het stoppen en opnieuw starten van de server van A.
- Een dataset in de MinIO-bucket van het project. A en B lezen hem uit een notebook.

Verifieer elke stap met een meting of een schermafbeelding.

### 8. Culling en ArgoCD

- Zet de culler-timeout voor de meting kort. De server van A verdwijnt na de timeout, de PVC blijft staan.
- Laat terwijl er user-pods draaien een Argo-sync met prune lopen op de applicatie van het project. De user-pods blijven staan, en de app blijft Synced en Healthy. Staan de user-pods in de resourceboom van de app? Opschrijven.
- Rol het projectfile nog een keer uit via OPI (bijvoorbeeld met een gewijzigde env var). De draaiende user-pods en de PVC's overleven dat.

### 9. Wat ZAD nodig heeft om dit zonder handwerk te doen

Geen bouwwerk, een beschrijving. Wat is er nodig om de Role uit taak 4 door OPI in git te laten genereren in plaats van met de hand toe te passen? Beantwoord:

- Wat wordt hierdoor schrijfbaar dat het nu niet is, en welke guard staat erop? Een project dat pods mag aanmaken in zijn eigen namespace, is een nieuw recht. Hoort daar een goedkeuring bij (`opi/services/catalog/approval.py`), en op welke schrijfwegen (wizard, API, projectfile in git)?
- Krijgt alleen het hubcomponent die rechten, of de hele project-ServiceAccount? Wat betekent een eigen ServiceAccount per component voor `features/image-registries.md` (de reden dat er nu één per project is)?
- Wat doet de Capsule-tenant op ODCN met een Role die `pods/create` geeft? Dat is hier niet te meten: schrijf het op als open vraag, met het commando om het later read-only te toetsen.

### 10. Het verslag en de documentatie

- Een verslag in `docs/`, pad als voorstel: `docs/jupyterhub-poc-verslag.md`. Per taak wat er gedaan, gemeten en misgegaan is, en per assertie onder "Wat het moet aantonen" groen of rood met het bewijs. Sluit af met één zin als oordeel: dit kan op ZAD, of dit kan niet en dit is waarom.
- Werk `features/online-ontwikkelomgeving-trainees.md` bij naar deze opzet: geen helm, de hub als ZAD-component, de gemeten rechten. Laat de rest van het document staan.
- Vervolgwerk als punten in `TODO.md`, elk op zichzelf leesbaar: de ZAD-capability voor spawn-rechten (taak 9), VLAM op de user-pods, code.overheid.nl, de isolatie tussen gebruikers binnen een project, en ODCN.

## Wat erbuiten valt

- **Wijzigingen aan OPI.** Wat ZAD zou moeten kunnen, wordt beschreven (taak 9) en niet gebouwd.
- **VLAM en AI-extensies.** De netpol van de `vlam`-service selecteert op het label `app` van het component, en dat label mag niet op de user-pods (zie de valkuil). Dat vraagt een eigen ontwerp.
- **code.overheid.nl.** De aansluiting op de pilot is organisatorisch en loopt apart.
- **Productie en ODCN.** Niets uitrollen, niets wijzigen. Open vragen over ODCN komen in het verslag.
- **Helm-charts als vergelijking.** Bewust niet, ook niet "even kijken hoe Z2JH het doet" als uitrolweg. De chart lezen als bron voor de config en de rechten mag wel.

## Na elke ronde

- `orch sandbox release`.
- Laat het POC-project op de sandbox staan zolang de taak loopt, en zet in het verslag hoe je het opruimt (project verwijderen via OPI, plus de Role en RoleBinding met de hand).
- Commit wat er is, ook als het half af is. Het verslag zegt waar de ronde is gebleven en wat de volgende stap is.
