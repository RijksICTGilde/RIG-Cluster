# RegistryProxy CRD

Kubernetes nodes op het Community Cluster kunnen niet direct bij externe registries (ghcr.io, docker.io, etc). Alle images moeten via RCR. De `RegistryProxy` CRD automatiseert het aanmaken van proxy caches in RCR.

## Object

```yaml
apiVersion: registryproxies.k8s.rijksapps.nl/v1alpha1
kind: RegistryProxy
metadata:
  name: ghcr-minbzk-amt              # vrij te kiezen
  namespace: my-project
spec:
  upstream: ghcr.io/minbzk/amt       # originele registry + pad
  downstream: rcr.rijksapps.nl/repository/rig  # RCR tenant, resultaat wordt {downstream}/{upstream}
  upstreamImagePullSecret:            # optioneel, alleen voor private registries
    name: ghcr-creds                  # bestaand dockerconfigjson Secret met upstream credentials
  imagePullSecret:
    name: ghcr-minbzk-amt-pull        # door operator aangemaakt dockerconfigjson Secret voor RCR
                                      # expliciet opgeven, of weglaten voor conventie-naam
                                      # conventie: upstream met / en . vervangen door -
                                      # bijv. ghcr.io/minbzk/amt → ghcr-io-minbzk-amt
status:
  ready: true
  image: rcr.rijksapps.nl/repository/rig/ghcr.io/minbzk/amt  # resulterende image URL
  imagePullSecretName: ghcr-minbzk-amt-pull
```

## Naamgeving

De `imagePullSecret.name` kan expliciet opgegeven of weggelaten worden. Bij weglaten wordt de naam afgeleid van de upstream via conventie: vervang `/` en `.` door `-`. Dezelfde conventie kan gebruikt worden voor een eventueel ServiceAccount.

## ServiceAccount

Niet strikt nodig — het imagePullSecret kan direct op de Pod spec gezet worden via `imagePullSecrets`. Voor afnemers die dit willen automatiseren kan de operator optioneel een ServiceAccount aanmaken of patchen met het imagePullSecret. De naam kan dezelfde conventie volgen.

## Operator gedrag

- **Create** — maakt proxy cache in RCR + imagePullSecret in namespace
- **Update** — watcht `upstreamImagePullSecret`, synct wijzigingen naar RCR
- **Delete** — reference counting: verwijdert proxy cache uit RCR alleen als geen andere RegistryProxy objecten dezelfde upstream gebruiken

## ZAD flow

1. Afnemer geeft image URL + credentials op in ZAD
2. ZAD detecteert non-RCR image, maakt RegistryProxy + upstream credentials Secret aan
3. Operator maakt proxy cache in RCR + imagePullSecret
4. ZAD herschrijft image: `ghcr.io/minbzk/amt:latest` → `rcr.rijksapps.nl/repository/rig/ghcr.io/minbzk/amt:latest`
