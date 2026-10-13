# jupyterhub-hub

De JupyterHub van de POC uit RC-258: een ZAD-component dat met KubeSpawner een pod per
gebruiker start. Geen helm en geen helmfile, zie `docs/jupyterhub-poc-verslag.md`.

De image is de hub plus `jupyterhub-kubespawner`, `oauthenticator` en
`jupyterhub-idle-culler`, met `jupyterhub_config.py` erin gebakken. `configurable-http-proxy`
zit al in de base-image en draait als subproces in dezelfde pod.

## Bouwen en op de sandbox zetten

Rechtstreeks naar de in-cluster registry (`docs/sandbox-image-deploy-via-registry.md`), en
**niet** via `docker save` plus de push-API van ZAD. Die weg is in ronde 1 gemeten en werkt
hier niet: een image van 2,7 GB komt er niet door en legt de control plane van de sandbox
om, en wat er wel doorkomt is daarna niet pullbaar omdat het project geen pull-secret heeft
(`docs/jupyterhub-poc-verslag.md`, taak 3).

```bash
# <projectnaam> is de technische naam die ZAD aan het project gaf, niet "jupyterhub-poc".
IMG=registry.sandbox.rijksapp.dev/rig:<projectnaam>_hub-rc258
docker build --network=host -t "$IMG" images/jupyterhub-hub
docker login registry.sandbox.rijksapp.dev -u admin -p admin1234
docker push "$IMG"
```

Dat het image in de registry staat is niet genoeg: de pods van het project mogen er pas bij
met de dienst `image-registries` (`features/image-registries.md`). Zonder die dienst komt de
pod niet verder dan ImagePullBackOff.

## Configuratie

Alles wat per project verschilt komt uit env vars. De diensten van het project leveren
`OIDC_*` (keycloak), `PUBLIC_HOSTNAME` (publish-on-web), `DATA_PATH` (persistent-storage) en
`OBJECT_STORE_*` (minio-storage). De rest staat als `user-env-vars` in
`projects/ideas/jupyterhub-poc.yaml`:

| Variabele | Betekenis |
|---|---|
| `JUPYTERHUB_POC_HUB_SERVICE` | ClusterIP-Service van dit component, waar user-pods op terugpraten (`<deployment>-<component>`) |
| `JUPYTERHUB_POC_DEPLOYMENT_LABEL` | Het podlabel `deployment` dat op elke user-pod komt, zodat de tenant-baseline NetworkPolicy hem ook selecteert |
| `JUPYTERHUB_POC_USER_IMAGE` | De image uit `images/jupyterhub-user` |
| `JUPYTERHUB_POC_USER_STORAGE` | Grootte van de PVC per gebruiker |
| `JUPYTERHUB_POC_USER_CPU_LIMIT` / `_CPU_GUARANTEE` / `_MEM_LIMIT` / `_MEM_GUARANTEE` | Resources per user-pod |
| `JUPYTERHUB_POC_CULL_TIMEOUT` / `_CULL_EVERY` | Idle-culler, in seconden |
| `JUPYTERHUB_POC_ADMINS` | Komma-gescheiden gebruikersnamen met het adminpaneel |

De hub heeft rechten nodig om pods en PVC's aan te maken. Die staan in
`features/jupyterhub-poc/hub-role.yaml` en worden in de POC met de hand toegepast.
