# ArgoCD-rollen op least privilege, en ZAD op een eigen account

ArgoCD is in dit platform een uitvoerder van wat in git staat: wijzigen hoort via git en OPI
te lopen. Deze wijziging zet de toegang daarop: de ontwikkelaarsgroep kan niet meer aanmaken,
wijzigen of verwijderen, en OPI werkt met een eigen account in plaats van met admin.

## Wat er staat

| Wie | Rol | Mag |
|---|---|---|
| `system:cluster-admins`, `rig-prd-admin-group` | `role:admin` | alles |
| `rig-prd-developer-group` | `role:readonly` + `role:zad-developer` | lezen, syncen, refreshen, resource-actions |
| `rig-prd-viewer-group` | `role:readonly` | lezen |
| account `opi` (OPI) | `role:zad-opi` | `applications, get` en `applications, sync` |

Op odcn-staging staat dezelfde verdeling, met `rig-staging-`-groepen.

De policy staat in de ArgoCD-CR van elke overlay
(`bootstrap/rig-system/kustomize/overlays/*/argocd-deployment.yaml`), met de inventaris van
OPI's aanroepen als commentaar erboven. `server.rbac.log.enforce.enable` staat aan, dus de
enforce-beslissingen staan in de log van argocd-server. Die regel staat op debug-niveau; de
`PermissionDenied`-waarschuwing ernaast staat er altijd, met subject, resource en actie.

Aanmaken en verwijderen van een Application of AppProject gaat via git en OPI, of via de
admingroep. Dat is de afspraak achter deze rolverdeling.

## Wie komt er nog in de UI

Deze wijziging raakt het lokale `admin`-account niet: dat houdt zijn `login`-capability en
blijft in ArgoCD een superuser, dus `defaultPolicy: role:none` beperkt hem niet. Op
de ODCN-clusters loopt menselijke toegang verder via SSO en de groepen uit de tabel hierboven;
op de sandbox bestaan die groepen niet en is `admin` de enige menselijke login, met het
wachtwoord uit `docs/sandbox-reference.md`. Alleen het account `opi` heeft geen login.

Gemeten op de sandbox (18-10-2026), met de policy van deze branch actief:

```
$ argocd account list
NAME   ENABLED  CAPABILITIES
admin  true     login
opi    true     apiKey

$ argocd account can-i create applications '*/*'      # als admin
yes
$ curl -k -X POST https://argo.sandbox.rijksapp.dev/api/v1/session -d '{"username":"admin",...}'
{"token":"eyJ..."}                                    # en GET /api/v1/applications geeft de apps
```

## Het account van OPI en zijn token

`accounts.opi: apiKey` in `extraConfig` maakt het account aan zonder `login`: er is geen
wachtwoord waarmee iemand als OPI de UI in komt. OPI leest zijn token uit het Secret
`opi-argocd-token` (key `token`) als `ARGOCD_TOKEN`; is dat gezet, dan slaat de connector
`POST /api/v1/session` over en stuurt hij het token als bearer.

`ARGOCD_USERNAME` en `ARGOCD_PASSWORD` hebben geen default meer. Zonder token en zonder
wachtwoord weigert `create_argo_connector()` met `ArgoAuthConfigurationError`, in plaats van
stil een poging met `admin`/`admin` te doen.

### De sandbox valt terug op het admin-account

Op `sandboxed-local` zet de overlay `ARGOCD_USERNAME: admin` en `ARGOCD_PASSWORD` uit
`argocd-cluster` (`admin.password`). Is `ARGOCD_TOKEN` gezet, dan wint het token: de
connector kijkt daar eerst. Dit is een bewuste keuze. De sandbox houdt zijn
admin-wachtwoorden om eenvoudig te blijven, en alleen `task sandbox:issue-argocd-opi-token`
geeft een token uit: een `sandbox-deploy` doet dat niet, dus zonder deze terugval verliest
een sandbox van voor deze wijziging zijn verbinding met ArgoCD.

Op `odcn-production` en `odcn-staging` blijft het strikt: geen wachtwoord in de deployment,
alleen het token, en zonder token een harde weigering.

Het token aanmaken en roteren:

```bash
uv run --project operations-manager/python python scripts/edit-secret.py \
  --component argocd-opi-token --cluster sandboxed-local --apply --yes
```

Die ronde maakt met het admin-account een nieuw token aan (geldig 8760h), zet het in het
Secret, herstart OPI en trekt het oude token in, met het bewijs dat het nieuwe werkt en het
oude niet meer. `task sandbox:setup` draait hem, zodat een verse sandbox meteen een token
heeft. Het admin-account blijft bestaan voor precies deze rotatie en voor noodgevallen.

## Kubernetes-RBAC op de CR's

Wie Kubernetes-schrijfrecht op `applications.argoproj.io` heeft gaat om de ArgoCD-rollen
heen. OPI's recht daarop staat daarom als Role in de namespace van ArgoCD
(`bootstrap/rig-system/kustomize/operations-manager/base/argo-application-role.yaml`), met precies de verbs die hij
gebruikt: `get, list, patch, delete` op `applications` en `get, list, delete` op
`appprojects`. De cluster-brede regel in de ClusterRole `namespace-manager` (die ook `update`
gaf) is weg.

Het schrijfrecht van `rig-prd-developer-group` op die CR's komt op odcn-production uit een
RoleBinding van Capsule en staat niet in git; smaller maken is daar een tenantinstelling.

## De GitHub-webhook

ArgoCD controleert een webhook alleen als `webhook.github.secret` in `argocd-secret` staat.
Dat secret bezit de ArgoCD-operator, dus het kan niet in git: het component
`argocd-webhook-github` van de rotatietool patcht alleen die key erin, en de verificatie is
een echte webhook-POST met een HMAC-handtekening (de nieuwe wordt aangenomen, de oude
afgewezen). De stap in GitHub (het veld Secret in de webhook-instellingen van de repo's) staat
als tekst in de rij. Tussen die twee stappen valt de webhook even weg; OPI vraagt zelf een
refresh aan, dus dat kost alleen snelheid.

De eerste ronde is tegelijk de eerste uitgifte: de key staat er dan nog niet. De rij draait
dan door en meldt bij de check dat ArgoCD nu elke webhook aanneemt, in plaats van af te
breken op een ontbrekende oude waarde.

## Toetsen

`operations-manager/python/tests/test_argocd_rbac_least_privilege.py` leest de policy uit
elke ArgoCD-CR onder de overlays (odcn-production, odcn-staging, sandboxed-local) en de
Role, en faalt op een binding op `role:admin` die er niet hoort, op een schrijfactie in een
eigen rol, en op een verb in de Role dat niet in de inventaris staat.
`tests/test_argo_token_auth.py` pint de tokenweg in de connector vast.
`tests/test_argocd_auth_env_per_overlay.py` rendert de overlays met `kustomize build` en
faalt als het wachtwoord in `base/` terugkomt of uit de sandbox verdwijnt.
