# SOPS-bestandsnamen

## Wat het is

De twee namen die een secretbestand draagt op weg door de versleutelpijplijn, met
`opi/utils/sops.py` als enige eigenaar:

| Naam | Wanneer | Betekenis |
|---|---|---|
| `<naam>.to-sops.yaml` | direct na generatie | het secret staat er in platte tekst in |
| `<naam>.sops.yaml` | na `encrypt_to_sops_files()` | de versleutelde tegenhanger, dit gaat naar git |

Beide namen zitten in het GitOps-contract. De ArgoCD CMP-plugin en `decrypt-sops.yaml`
matchen op `.sops.yaml`, en de commitgrendel in de git-connector weigert elk
`.to-sops.yaml` dat nog in de werkboom staat (zie
`features/sops-skip-unchanged-reencryption.md` voor de versleutelstap zelf).

## Gebruik

```python
from opi.utils.sops import SOPS_SUFFIX, TO_SOPS_SUFFIX, sops_filenames

sops_filenames("demo-secret").plaintext   # 'demo-secret.to-sops.yaml'
sops_filenames("demo-secret").encrypted   # 'demo-secret.sops.yaml'
```

`sops_filenames()` accepteert een kale naam en een naam die een van beide suffixen al
draagt, dus dezelfde aanroep bouwt het paar voor een nieuw secret en zet de ene naam van
een bestaand paar om in de andere:

```python
sops_filenames("odcn/demo/values.to-sops.yaml").encrypted   # 'odcn/demo/values.sops.yaml'
sops_filenames("productie-docs-helm-values.sops.yaml").plaintext
```

De constanten zijn voor herkenning (`endswith`, globpatronen, overslaglijsten), de
functie voor het bouwen en omzetten van namen. Bouw een naam niet met de hand: dat is de
dubbeling die deze module opheft.

## Afhankelijkheden

`opi/utils/sops.py` importeert alleen `opi.core.config` en `opi.utils.age`, dus elke laag
mag hem aanroepen (manager, generation, connectors, services, utils).
