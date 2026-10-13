# jupyterhub-user

De pod per gebruiker van de POC uit RC-258: JupyterLab met VS Code (code-server) als tegel
in de launcher, en `jupyter-collaboration` voor samen in hetzelfde notebook werken.

Extensies komen van Open VSX en niet van de Microsoft Marketplace: de Python-extensie en
basedpyright (Pylance staat niet op Open VSX). Geen AI-extensies, VLAM valt buiten RC-258.

`jupyter-server-proxy` staat expliciet in de Dockerfile: `jupyter-vscode-proxy` 0.7
declareert geen dependencies, dus zonder die regel ontbreekt de serverextensie die
`/vscode` bedient en blijft de tegel weg.

`code-server-settings.json` zet vijf standaarden van code-server 4.141 om:

- `chat.disableAIFeatures`: de editor bundelt zelf een Copilot-chatpaneel
  (`product.json`: `defaultChatAgent = GitHub.copilot`). Dat is de Big Tech-afhankelijkheid
  die `features/online-ontwikkelomgeving-trainees.md` uitsluit.
- `security.workspace.trust.enabled`: anders vraagt elke terminal in de eigen home eerst om
  vertrouwen. De isolatiegrens is de pod en niet dat dialoogvenster.
- `workbench.startupEditor`: de welkomstpagina staat anders over de tegel die net geopend is.
- `telemetry.telemetryLevel`: geen telemetrie uit een omgeving voor trainees.
- `extensions.autoUpdate`: de POC pint de extensieversies, dus een update onder de hand
  erdoor maakt de meting onherhaalbaar.

## Bouwen en op de sandbox zetten

Rechtstreeks naar de in-cluster registry (`docs/sandbox-image-deploy-via-registry.md`), en
**niet** via `docker save` plus de push-API van ZAD. Die weg is in ronde 1 gemeten en werkt
hier niet: een image van 2,7 GB komt er niet door en legt de control plane van de sandbox
om, en wat er wel doorkomt is daarna niet pullbaar omdat het project geen pull-secret heeft
(`docs/jupyterhub-poc-verslag.md`, taak 3).

```bash
# <projectnaam> is de technische naam die ZAD aan het project gaf, niet "jupyterhub-poc".
IMG=registry.sandbox.rijksapp.dev/rig:<projectnaam>_user-rc258
docker build --network=host -t "$IMG" images/jupyterhub-user
docker login registry.sandbox.rijksapp.dev -u admin -p admin1234
docker push "$IMG"
```

Dat het image in de registry staat is niet genoeg: de pods van het project mogen er pas bij
met de dienst `image-registries` (`features/image-registries.md`). Zonder die dienst komt de
pod niet verder dan ImagePullBackOff.

## Lokaal bekijken

```bash
docker build --network=host -t jupyterhub-poc-user:rc258 images/jupyterhub-user
docker run --rm -p 8888:8888 jupyterhub-poc-user:rc258 \
  start-notebook.py --IdentityProvider.token=lokaal --ServerApp.ip=0.0.0.0
# http://127.0.0.1:8888/lab?token=lokaal, daarna de tegel "VS Code"
```
