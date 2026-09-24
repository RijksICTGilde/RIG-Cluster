# De AGE-sleutel in het cluster: `sops-age-key`

Het contract achter één secretnaam die in veel namespaces voorkomt, met per namespace een andere inhoud. Wie dat voor één secret aanziet, overschrijft de sleutel van een project of laat er een op de oude sleutel achter.

## Eén naam, twee soorten inhoud

`sops-plugin.sh:23` leest de sleutel zo:

```bash
SOPS_KEY_B64=$(kubectl get secret ${SOPS_KEY_SECRET} -n ${ARGOCD_APP_NAMESPACE} -o jsonpath='{.data.key}')
```

`SOPS_KEY_SECRET` is `sops-age-key` tenzij de Application hem via `plugin.env` overschrijft, en `ARGOCD_APP_NAMESPACE` is de namespace waar die Application naartoe deployt. De plugin haalt de sleutel dus niet uit één centrale plek maar uit de bestemming van wat hij aan het renderen is. Daaruit volgt alles hieronder.

Er zijn twee soorten houders:

| soort | namespace | inhoud | wie zet hem neer |
|---|---|---|---|
| platform | `rig-prd-operations` (prod), `rig-system` (sandbox) | de platformsleutel | bootstrap |
| platform, kopie | `rig-prd-ron` (prod), `rig-ron` (sandbox) | dezelfde platformsleutel | met de hand, zie hieronder |
| project | `rig-prd-<project>` / `rig-<project>` | de EIGEN sleutel van dat project | OPI, `store_project_sops_key_in_namespace` |

De tweede rij is de val. `bootstrap/rig-system/kustomize/overlays/odcn-production/namespace-ron.yaml` zegt het in zijn eigen commentaar: het secret in die namespace "kan niet uit git komen" en wordt gekopieerd uit `rig-prd-operations`. Het staat dus in geen enkel manifest, het komt niet mee met een sync, en het valt alleen op als je ernaar kijkt. De mailrelay rendert `infrastructure/bootstrap/infrastructure/mail/controller/overlays/odcn`, en daar staat een `decrypt-sops.yaml`, dus zonder die kopie rendert die applicatie niet.

## Wat dit betekent als je de platformsleutel vervangt

Nooit alle `sops-age-key`-secrets overschrijven. Dat vervangt de eigen sleutel van elk project door de platformsleutel, en dan is elk projectgeheim onleesbaar.

De regel is: vervang alleen waar de OUDE publieke sleutel in staat. `scripts/set-sops-key-secret.py` doet precies dat, en het is de reden dat het script elke namespace langsloopt in plaats van een vaste lijst af te werken. Zijn droogloop toont twee groepen, en die lijst is de controle vóór de onomkeerbare stap:

```
N carry the OLD platform key and WILL be replaced:   <- hier horen alleen platformhouders
M carry a DIFFERENT key and are left alone:          <- hier hoort elke projectnamespace, met zijn eigen publieke sleutel
```

Gemeten op het sandboxcluster op 24 september 2026: 16 houders, waarvan 2 de platformsleutel (`rig-system`, `rig-ron`) en 14 een eigen projectsleutel. Op productie is het aantal projectnamespaces anders, maar de verhouding is dezelfde en de twee platformhouders heten daar `rig-prd-operations` en `rig-prd-ron`.

## Wie moet herstarten

De plugin niet: die leest het secret bij elke render opnieuw, dus de eerste render na de wissel draait al op de nieuwe sleutel. Forceer die render terwijl je kijkt, in plaats van hem bij een willekeurige sync tegen te komen.

OPI wel. Die krijgt de sleutel via `env.valueFrom.secretKeyRef` binnen (`bootstrap/rig-system/kustomize/operations-manager/base/deployment.yaml:117`), en een draaiende pod ziet een gewijzigd secret daar niet. Een `rollout restart` volstaat, en er is geen nieuwe image voor nodig: er staat geen `secretGenerator` in `bootstrap/`, dus het manifest verandert niet mee.

## Verder lezen

- `features/sops-sleutel-roteren.md` is het stappenplan voor de vervanging zelf
- `features/send-email.md` heeft de aanzetstappen voor de ron-namespace, inclusief het kopieercommando
- `bootstrap/rig-system/kustomize/sops-plugin.sh` is de plugin die de sleutel leest
