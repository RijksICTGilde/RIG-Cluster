# Negen componenten weer aanzetten na de quota-storing van 30 september

Vijf deployments, negen componenten. Een redeploy per deployment zonder de componenten te
noemen bestaat niet: allebei de aanroepers van de redeploy-hook ruimen alleen op voor de
componenten die de aanroep zelf noemt. Dus negen calls, gegroepeerd per deployment.

## Eerst het slechte nieuws: `:refresh` doet dit niet

Een refresh herverwerkt de deployment uit het projectbestand, en in dat bestand staat `disabled: true`. De generatie leest dat en maakt er opnieuw nul replicas van:

```
project_manager.py:5995    replicas = 0 if (is_disabled or is_sleeping) else 1
```

Een `POST .../:refresh` is hier dus een no-op. Hij draait, hij slaagt, en er verandert niets.

Wat de disable wel opheft is de redeploy-hook (`opi/services/catalog/deployment_health/__init__.py:166`, die `disabled` op `False` zet). Die hook heeft precies drie aanroepplekken in de hele codebase, en `:refresh` zit er niet bij:

| aanroeper | wat het is |
|---|---|
| `_upsert_deployment_once` (2x) | een deployment toevoegen of bijwerken |
| `update_component_image` | de image van een component zetten |

Dat is ook precies wat er vandaag bij `regel-k4c/regelrecht`, `wies/pr-673` en `nd-j7s/pr-59` gebeurde: hun CI zette een image en de hook ruimde de disable op. De meldingstekst zegt het zelf: "en is weer aangezet, want er is nieuwe inhoud uitgerold".

## Waarom dit tóch geen nieuwe image plaatsen is

De aanroep hieronder leest eerst de image die er nu al staat en schrijft diezelfde waarde terug. Er wordt niets nieuws geplaatst en er staat geen image-URL in dit document die met de hand is overgetypt. Het enige effect is dat de rollout-hook afgaat, en dat is precies de redeploy die je wil.

`update_component_image` vergelijkt niet op gelijkheid voordat het de hook aanroept (`project_manager.py:9228`), dus een identieke waarde werkt.

## De negen

| project | deployment | componenten |
|---|---|---|
| wies | pr-692 | frontend, worker |
| nd-j7s | pr-62 | nmcapi |
| mpfm-w3h | pr-334 | demopersonas, magazijna, magazijnb, magazijnsimulator |
| mpfb-8wh | pr-334 | uitvraag |
| mpfpsm-lcl | pr-334 | notificatie |

Vijf projecten, dus vijf API-sleutels. `mpfb-8wh/fsc-logius-logius-fscbootstrap` staat ook op nul replicas maar is nooit door OPI uitgeschakeld, die hoort hier niet bij en laat je met rust.

Alle negen images gaven op 30 september om 16:00 een HTTP 200 bij het register, dus de pull lukt en het aanzetten blijft staan. Dat is wel een momentopname: het quotum op `ghcr-rig` is niet opgelost en de gerepareerde guard (`d4d9969d7`) zit nog niet in een uitgerolde OPI.

## Het script

Sla dit op, vul de vijf sleutels in en draai het. Het draait de regels van onder naar boven pas als je ze aanzet, zie de laatste alinea.

```bash
#!/usr/bin/env bash
set -euo pipefail

BASE="https://zad.rijksapp.nl"   # of https://operations-manager.rig.prd1.gn2.quattro.rijksapps.nl

declare -A KEY=(
  [wies]="PLAATS_HIER_DE_API_SLEUTEL_VAN_wies"
  [nd-j7s]="PLAATS_HIER_DE_API_SLEUTEL_VAN_nd-j7s"
  [mpfm-w3h]="PLAATS_HIER_DE_API_SLEUTEL_VAN_mpfm-w3h"
  [mpfb-8wh]="PLAATS_HIER_DE_API_SLEUTEL_VAN_mpfb-8wh"
  [mpfpsm-lcl]="PLAATS_HIER_DE_API_SLEUTEL_VAN_mpfpsm-lcl"
)

redeploy() {
  local project="$1" deployment="$2" component="$3"
  local key="${KEY[$project]}"

  # 1. lees de image die er nu staat
  local image
  image=$(curl -fsS -H "X-API-Key: $key" \
            "$BASE/api/v2/projects/$project/deployments/$deployment" \
          | python3 -c '
import json, sys
ref = sys.argv[1]
d = json.load(sys.stdin)
for c in d.get("components", []):
    if c.get("reference") == ref:
        print(c["image"]); break
else:
    sys.exit(f"component {ref} niet gevonden")
' "$component")

  echo "== $project/$deployment/$component"
  echo "   image: $image"

  # 2. schrijf exact dezelfde waarde terug; dat vuurt de redeploy-hook af
  local body
  body=$(python3 -c '
import json, sys
print(json.dumps({"componentName": sys.argv[1], "newImageUrl": sys.argv[2]}))
' "$component" "$image")

  curl -fsS -X PUT \
    -H "X-API-Key: $key" \
    -H "Content-Type: application/json" \
    "$BASE/api/v2/projects/$project/deployments/$deployment/image" \
    -d "$body"
  echo; echo
}

# Begin met deze ene en controleer het resultaat voordat je de rest aanzet.
redeploy wies       pr-692 frontend

# redeploy wies       pr-692 worker
# redeploy nd-j7s     pr-62  nmcapi
# redeploy mpfm-w3h   pr-334 demopersonas
# redeploy mpfm-w3h   pr-334 magazijna
# redeploy mpfm-w3h   pr-334 magazijnb
# redeploy mpfm-w3h   pr-334 magazijnsimulator
# redeploy mpfb-8wh   pr-334 uitvraag
# redeploy mpfpsm-lcl pr-334 notificatie
```

## Hoe je het draait

Elke aanroep geeft een task-id terug en werkt asynchroon. Draai ze één voor één en wacht tot de vorige klaar is, zeker bij `mpfm-w3h/pr-334`, want daar gaan vier componenten door dezelfde deployment en elke aanroep is een commit plus een ArgoCD-rollout. Vier tegelijk laten racen op hetzelfde projectbestand is vragen om een conflict.

Begin met `wies/pr-692/frontend`. Werkt die, dan haal je de commentaartekens van de rest weg.

## Controleren

```bash
kubectl -n rig-prd-wies       get deploy pr-692-frontend pr-692-worker
kubectl -n rig-prd-nd-j7s     get deploy pr-62-nmcapi
kubectl -n rig-prd-mpfm-w3h   get deploy -l deployment=pr-334
kubectl -n rig-prd-mpfb-8wh   get deploy pr-334-uitvraag
kubectl -n rig-prd-mpfpsm-lcl get deploy pr-334-notificatie
```

Goed is `1/1`, of `0/1` met een pod in `ImagePullBackOff`. Dat tweede is geen mislukking maar precies de bedoeling: de pod blijft het zelf proberen en komt op zodra het register weer ruimte heeft. Fout is `0/0`, want dan staat de disable er nog.
