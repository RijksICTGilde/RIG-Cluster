# Online ontwikkelomgeving voor trainees (Python in de browser, met AI-assistentie)

**Status**: Idee, met een POC in uitvoering (RC-258). Zie `docs/jupyterhub-poc-verslag.md` voor wat er gemeten is.
**Created**: 2026-10-12
**Aanvrager**: Lorin Hendriks (trainee-programma), via Robbert Uittenbroek

## Aanleiding

Twee trainee-groepen willen voor hun opdrachten "met Python aan de slag". Op de managed devices van de overheid mag dat niet: ze kunnen er geen Python, IDE of packages installeren. Het gevolg is dat trainees nu op hun eigen devices werken. Dat willen we juist niet.

Het doel is niet dat we zelf techniek bedenken. We willen een bestaande open-source oplossing op ZAD hosten, zodat er een plek is waarvan duidelijk is **dat het mag**: afgeschermd, achter SSO Rijk, voor een beperkte groep, en met de code op code.overheid.nl in plaats van op een lokale schijf of bij Big Tech.

## De wens

| # | Wens | Toelichting |
|---|---|---|
| 1 | Een volwaardige code editor in de browser | Het moet als een IDE voelen (bestanden, terminal, debugger, Git) en niet alleen als notebooks. Op het managed device is alleen een browser nodig. |
| 2 | Eigen Python-omgeving per gebruiker | Elke trainee een eigen, geïsoleerde omgeving met Python, packages en een terminal. |
| 3 | Multi-user, met SSO Rijk | Inloggen via de bestaande Keycloak/SSO Rijk-koppeling van ZAD. |
| 4 | Toegang en rechten per groep | Alleen geautoriseerde groepen komen erin. Per groep kunnen resources, images en tools verschillen. |
| 5 | Code op code.overheid.nl | Werk wordt gepusht naar code.overheid.nl (Forgejo). De persistente schijf in de omgeving is alleen een werkkopie. |
| 6 | AI-assistentie bij het coderen | Chat en autocompletion in de editor, en liefst ook een open-source code agent. Dit moet op **onze eigen modellen** draaien via VLAM, en niet bij een externe AI-leverancier. |
| 7 | Open source, of in elk geval betaalbaar, en zonder Big Tech-afhankelijkheid | Geen Microsoft Marketplace, Copilot, Google Colab en dergelijke. |

## Voorgestelde oplossingsrichting

### Overzicht

```
Browser (managed device)
   |
   | HTTPS, SSO Rijk via Keycloak (ZAD)
   v
JupyterHub als gewoon ZAD-component (eigen image, geen helm)
   |-- Authenticator: GenericOAuthenticator (OIDC naar Keycloak), rol als poort
   |-- Spawner: KubeSpawner, 1 pod + 1 PVC per gebruiker
   |-- Idle culler: ruimt inactieve pods op
   v
Pod per gebruiker (eigen image)
   |-- JupyterLab als voorkant, code-server als tegel, via jupyter-vscode-proxy  -> /vscode
   |-- Python + vaste set libraries
   |-- Extensies via Open VSX: Python, basedpyright, AI-assistent (Continue / Cline / Roo Code)
   |-- Terminal-agent: OpenCode en/of Aider
   |
   |-- egress --> code.overheid.nl   (git clone/push)
   |-- egress --> VLAM-proxy          (eigen modellen, OpenAI-compatible /v1)
   +-- egress --> PyPI of interne mirror (optioneel)
```

### 1. Multi-user laag: JupyterHub

We gebruiken JupyterHub als multi-user laag en niet als notebook-omgeving. Trainees landen direct in VS Code (code-server) en zien geen notebooks, tenzij een groep daar juist om vraagt.

**Hoe we hem uitrollen: als ZAD-component, zonder helm en zonder helmfile** (besloten in
RC-258). De hub is een eigen image met `jupyterhub_config.py` erin, en leest alles wat per
project verschilt uit env vars die de diensten van het project leveren. Reden: we willen
weten wat we draaien, en het moet op de bestaande ZAD-tooling passen (diensten, Keycloak,
netpols, Argo). De Zero to JupyterHub-chart blijft wel bruikbaar als bron voor de config en
de rechten; we rollen hem niet uit.

- Zero to JupyterHub (Helm, als leesbron): https://z2jh.jupyter.org
- JupyterHub: https://jupyterhub.readthedocs.io
- KubeSpawner, voor een pod per gebruiker en profielen: https://jupyterhub-kubespawner.readthedocs.io
- De licentie is BSD. Er is geen betaalde laag, en groepsbeheer zit in de gratis versie.

Waarom niet code-server los of Coder:

| Optie | Waarom niet (nu) |
|---|---|
| code-server los (https://github.com/coder/code-server) | Eén instantie per persoon, zonder gebruikersbeheer of SSO. Het is wel de editor die we gebruiken, maar dan binnen JupyterHub. |
| Coder (https://github.com/coder/coder) | De beste developer-ervaring. Maar OIDC group sync en rechten per template zitten in de betaalde Premium-licentie. |
| Eclipse Che / OpenShift Dev Spaces (https://eclipse.dev/che/) | Volledig open source, maar zwaar in beheer. Overkill voor deze doelgroep. |
| JupyterLite (https://jupyterlite.readthedocs.io) | Python volledig in de browser (Pyodide). Geen echte terminal, beperkte packages en geen agent. Wel een mogelijke fallback voor alleen "leren programmeren". |

### 2. Editor: code-server in de gebruikers-pod

- code-server: https://github.com/coder/code-server, met docs op https://coder.com/docs/code-server
- Proxy: https://pypi.org/project/jupyter-vscode-proxy/
- Base image: `quay.io/jupyter/minimal-notebook` (https://quay.io/repository/jupyter/minimal-notebook)
- Extensies komen van Open VSX (https://open-vsx.org) en niet van de Microsoft Marketplace. Pylance is daar niet beschikbaar. Het alternatief is basedpyright.

Schets van de image (niet getest; zet versies vast en gebruik geen `latest`):

```dockerfile
FROM quay.io/jupyter/minimal-notebook:<vaste-tag>

USER root
RUN curl -fsSL https://code-server.dev/install.sh | sh -s -- --version <vaste-versie>
USER ${NB_UID}

RUN pip install --no-cache-dir jupyter-vscode-proxy \
 && code-server --install-extension ms-python.python \
 && code-server --install-extension detachhead.basedpyright \
 && code-server --install-extension Continue.continue
```

```yaml
# Zero to JupyterHub values (schets)
singleuser:
  image:
    name: <registry>/trainee-vscode
    tag: <tag>
  defaultUrl: "/vscode"
  cpu: { limit: 1, guarantee: 0.25 }
  memory: { limit: 2G, guarantee: 512M }
  storage:
    capacity: 5Gi
cull:
  enabled: true
  timeout: 3600
```

### 3. SSO Rijk en groepen

- Maak een Keycloak-client in de realm van het project. Dat is het bestaande ZAD-mechanisme, zie `keycloak-additional-clients.md`.
- Login loopt via SSO Rijk, net als bij andere ZAD-applicaties. Zie `sso-rijk-migration.md` en `keycloak-auto-link.md`.
- **Toegang:** gebruik client- of realm-rollen als poort. Dat is de bestaande feature, zie `client-access-restriction.md` en `keycloak-realm-roles.md`. Daarnaast filtert JupyterHub zelf op `allowed_groups`.
- **Groepen naar profielen:** zet de rollen in een claim (bijvoorbeeld `groups`). JupyterHub neemt die over (`manage_groups: true`). KubeSpawner `profile_list` bepaalt dan per groep het image, de resources en de tools. Bijvoorbeeld: trainee-basis met 1 CPU, 2 GB en VS Code; data-analisten met meer geheugen en JupyterLab; een groep met of zonder AI-agent.

```python
# jupyterhub_config (schets)
c.JupyterHub.authenticator_class = "generic-oauth"
c.GenericOAuthenticator.oauth_callback_url = "https://<host>/hub/oauth_callback"
c.GenericOAuthenticator.authorize_url = "https://<keycloak>/realms/<realm>/protocol/openid-connect/auth"
c.GenericOAuthenticator.token_url = "https://<keycloak>/realms/<realm>/protocol/openid-connect/token"
c.GenericOAuthenticator.userdata_url = "https://<keycloak>/realms/<realm>/protocol/openid-connect/userinfo"
c.GenericOAuthenticator.username_claim = "preferred_username"
c.GenericOAuthenticator.claim_groups_key = "groups"
c.GenericOAuthenticator.allowed_groups = {"trainees-python", "trainees-data"}
c.GenericOAuthenticator.manage_groups = True
```

### 4. Code op code.overheid.nl

code.overheid.nl is het Forgejo-codeplatform van de overheid. BZK (OSPO) en SSC-ICT (DAWO) zijn ermee begonnen, en het is sinds april 2026 in soft launch als pilot. Aansluiten gaat via codeplatform@rijksoverheid.nl.

- Achtergrond: https://www.nldigitalgovernment.nl/netherlands-digitalisation-strategy-nds/dossierpostcontext/soft-launch-for-government-open-source-code-platform/
- code-server heeft Git ingebouwd, dus clone, commit en push werken gewoon.
- **Route A, simpel, voor de pilot:** de trainee maakt in Forgejo een personal access token aan en slaat dat op met `git credential-store` op de eigen PVC.
- **Route B, netter:** registreer JupyterHub als OAuth2-applicatie in Forgejo. Bij het opstarten van de pod haalt een `pre_spawn_hook` of `auth_state` een token op en zet dat als credential helper. Dan zijn er geen handmatige tokens. Dit moet nog uitgezocht worden, omdat de primaire login via Keycloak/SSO Rijk loopt en niet via Forgejo. Het werkt alleen als het account in Forgejo te koppelen is aan dezelfde identiteit.
- **Afspraak:** de PVC is werkruimte en geen archief. Wat bewaard moet blijven, staat op code.overheid.nl. Dat beperkt ook de backup-behoefte.
- **Open vraag:** zijn trainees (en hun organisaties) al welkom op code.overheid.nl, of moeten ze eerst aansluiten op de pilot? Moeten repo's publiek zijn, of kan het ook privé of intern?

### 5. AI-assistentie op eigen modellen (VLAM)

VLAM (SSC-ICT) staat al via de ZAD-service `vlam` binnen het cluster. Het is een OpenAI-compatible API (LiteLLM, `/v1/models`, `/v1/chat/completions`). Zie `vlam-service.md` en `vlam.md`. Een component met de service `vlam` krijgt `VLAM_API_URL` en een egress-regel naar de VLAM-proxy. Dat sluit direct aan: alle onderstaande tools praten met een OpenAI-compatible endpoint.

| Tool | Soort | Waar | Link |
|---|---|---|---|
| Continue | Chat, autocompletion en edit in de editor | VS Code-extensie (Open VSX) | https://github.com/continuedev/continue |
| Cline | Agent in de editor (bestanden lezen/schrijven, terminal) | VS Code-extensie (Open VSX) | https://github.com/cline/cline |
| Roo Code | Agent in de editor, fork van Cline met modes | VS Code-extensie (Open VSX) | https://github.com/RooCodeInc/Roo-Code |
| OpenCode | Code agent in de terminal | CLI in de image | https://github.com/sst/opencode |
| Aider | Pair-programming agent in de terminal, Git-bewust | CLI in de image (`pip install aider-chat`) | https://github.com/Aider-AI/aider |

Voorstel voor de pilot: **Continue** (laagdrempelig, meteen nuttig voor beginners) plus **één agent**: OpenCode in de terminal, of Cline/Roo Code in de editor. Laat de trainees kiezen.

Configuratie: zet in de image of via env vars de base URL op `VLAM_API_URL`, met een vaste lijst modellen. Een deel van de tools leest `OPENAI_BASE_URL` en `OPENAI_API_KEY`. Volgens `vlam-service.md` kun je dat met een alias op het component regelen.

Aandachtspunten voor AI:

- **Sleutels en verbruik per gebruiker.** Als alle pods één gedeelde VLAM-sleutel gebruiken, zien we niet wie wat verbruikt en kunnen we niets per gebruiker begrenzen. Mogelijke routes, nog uit te zoeken:
  - LiteLLM virtual keys per gebruiker of team aan de kant van VLAM, als SSC-ICT dat aanbiedt;
  - een eigen dunne LiteLLM-proxy voor deze omgeving, die per gebruiker een key uitgeeft (via `pre_spawn_hook`) en budgetten en logging doet.
- **Een agent voert commando's uit.** Dat gebeurt binnen de pod van de gebruiker, dus de isolatie van die pod is de sandbox (zie Security). Een agent met een Git-token kan ook pushen. Beperk de token-scope tot de eigen repo's.
- **Logging en verantwoording:** prompts kunnen code en data bevatten. Bepaal wat er gelogd wordt (Logboek Dataverwerkingen), en welke dataclassificatie richting VLAM mag.
- **Kwaliteit:** de agent-ervaring hangt sterk af van de beschikbare modellen en hoe goed die tool calling ondersteunen. Test vooraf welke VLAM-modellen goed werken met Cline/OpenCode.

## Inpassing in ZAD: open punten (eerst uitzoeken)

1. **Mag een app dynamisch pods spawnen?** Dit is de grootste blocker, en RC-258 meet hem. KubeSpawner heeft rechten nodig op pods, PVC's en events in de eigen namespace. ZAD genereert nu geen Role en RoleBinding, dus die worden in de POC met de hand toegepast; de set die nu is toegepast staat in `features/jupyterhub-poc/hub-role.yaml`; of elke regel ook nodig is, is nog niet gemeten. Let op: elk component in een project draait op dezelfde ServiceAccount, dus die rechten gelden voor het hele project. Wat nog open staat:
   - Past dit binnen de Capsule-tenant en de RBAC die OPI genereert? Op de sandbox niet te meten.
   - Gaat ArgoCD deze pods als drift of orphans zien? (zie `manifest-inventory-cleanup.md` en `service-orphan-reconciliation.md`)
   - Waar komen ResourceQuota en LimitRange per namespace vandaan?
2. **Hoe deployen we de Hub?** Beantwoord in RC-258: als gewoon ZAD-component met een eigen image, zonder helm en zonder helmfile. Zie hierboven.
3. **NetworkPolicies:** de standaard staat nu op allow-all binnen de namespace (`restrictive-network-policies.md`). Voor deze use-case moet dat strikt:
   - pod naar pod tussen gebruikers dicht;
   - egress alleen naar Hub, VLAM-proxy, code.overheid.nl en PyPI of een mirror;
   - geen toegang tot interne cluster-services.
4. **Opslag:** een PVC per gebruiker (StorageClass, quota). Bepaal wat er met de PVC gebeurt als iemand uit de groep gaat, en of er backups nodig zijn. Het voorstel is geen backups: code.overheid.nl is de bron.
5. **Images:** waar bouwen en hosten we de user-image (`image-registries.md`)? Wie patcht hem, en hoe vaak? Het security-scanningpipeline geldt ook hier (`security-scanning-pipeline.md`).
6. **Packages:** mogen trainees `pip install` doen vanaf het internet? Of krijgen ze een vaste image en eventueel een interne PyPI-mirror?

## Security en compliance

- Dit is een platform waarop gebruikers **willekeurige code** draaien, ook via een AI-agent. Daarom:
  - pods zonder privileges (`runAsNonRoot`, geen privilege escalation, `seccompProfile: RuntimeDefault`);
  - strikte NetworkPolicies;
  - resource limits;
  - idle culling.
- Let op misbruik zoals cryptomining. Monitoring op CPU-gebruik per pod via Prometheus is al aanwezig.
- **Dataclassificatie:** het voorstel is alleen oefendata en geen persoonsgegevens of vertrouwelijke opdrachtdata, tenzij er een DPIA ligt. Dit moet met Lorin en de CISO afgestemd worden.
- Toets de opzet tegen de BIO (toegangsbeheer, logging, netwerksegmentatie) en `bio-network-access-no-vpn-compliance.md`.

## Fasering

**Fase 0: verkennen (vooraf)**
- ZAD-team: punt 1 (pods spawnen, RBAC, Capsule, ArgoCD) en punt 3 (NetworkPolicies).
- Aansluiting op code.overheid.nl aanvragen (codeplatform@rijksoverheid.nl).
- VLAM: per-gebruiker keys mogelijk? Welke modellen zijn geschikt voor tool calling?
- CISO of security-officer informeren over de scope.

**Fase 1: pilot (2 à 4 weken, eigen project)**
- Eén trainee-groep van 5 à 10 mensen, alleen oefendata.
- JupyterHub met code-server, SSO Rijk, één groep of rol als toegangspoort.
- Git via personal access tokens (route A).
- Continue en één agent op VLAM, met een gedeelde key en duidelijke afspraken.

**Fase 2: evaluatie en besluit**
- Levert het op wat de trainees nodig hebben? Wat missen ze?
- Eigenaarschap en beheer beleggen: image-onderhoud, support aan trainees, kosten.
- Daarna eventueel: meerdere groepen en profielen, Forgejo OAuth (route B), een key en budget per gebruiker voor AI, en een interne PyPI-mirror.

## Succescriteria pilot

- Een trainee kan op een managed device, met alleen een browser, inloggen via SSO Rijk, Python schrijven en draaien, debuggen, en pushen naar code.overheid.nl.
- AI-assistentie werkt op VLAM, zonder externe AI-diensten.
- Gebruikers kunnen niet bij elkaars pods of bij interne cluster-services.
- Inactieve omgevingen worden automatisch opgeruimd.
- De trainees werken niet meer op hun eigen devices.

## Referenties

- Zero to JupyterHub: https://z2jh.jupyter.org
- JupyterHub OAuthenticator (Generic OAuth): https://oauthenticator.readthedocs.io
- KubeSpawner: https://jupyterhub-kubespawner.readthedocs.io
- code-server: https://github.com/coder/code-server
- jupyter-vscode-proxy: https://pypi.org/project/jupyter-vscode-proxy/
- Jupyter Docker Stacks (Quay): https://quay.io/organization/jupyter
- Open VSX: https://open-vsx.org
- Continue: https://github.com/continuedev/continue
- Cline: https://github.com/cline/cline
- Roo Code: https://github.com/RooCodeInc/Roo-Code
- OpenCode: https://github.com/sst/opencode
- Aider: https://github.com/Aider-AI/aider
- LiteLLM (virtual keys, budgetten): https://docs.litellm.ai
- code.overheid.nl, soft launch: https://www.nldigitalgovernment.nl/netherlands-digitalisation-strategy-nds/dossierpostcontext/soft-launch-for-government-open-source-code-platform/
- Coder (alternatief, Premium voor groepen): https://github.com/coder/coder
- Eclipse Che (alternatief): https://eclipse.dev/che/
- JupyterLite (fallback zonder server): https://jupyterlite.readthedocs.io

Gerelateerde ZAD-features: `vlam-service.md`, `client-access-restriction.md`, `keycloak-realm-roles.md`, `keycloak-additional-clients.md`, `sso-rijk-migration.md`, `restrictive-network-policies.md`, `helmfile-single-app-deployment.md`, `system-services-with-a-ui.md`, `image-registries.md`, `security-scanning-pipeline.md`, `sleep-mode.md`.
