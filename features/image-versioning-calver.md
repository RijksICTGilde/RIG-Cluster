# Image-versionering met CalVer

Gepubliceerde operations-manager-images krijgen een onveranderlijke CalVer-tag met de commit-hash
erin. Het deployment-manifest wijst naar die tag, niet meer naar `latest`.

## Het probleem

`publish-operations-manager` pushte naar `:latest`, en de odcn-production-overlay wees ook naar
`:latest`. Die tag beweegt: hij wijst altijd naar de laatste publish. Daardoor was er geen manier om
te zeggen welke build er draait, en geen manier om terug te gaan naar de vorige. Een rollback kwam
neer op opnieuw bouwen vanaf een oudere commit en hopen dat het resultaat hetzelfde was.

## Het tagformaat

```
{YYYY}.{MM}.{DD}.{HHMM}-{eerste 8 tekens van de commit-hash}

2026.07.22.1432-7ccad61d
```

De datum sorteert chronologisch en is meteen leesbaar. De tijd maakt elke build uniek, ook twee
builds van dezelfde commit. De hash zegt precies welke broncode erin zit.

Staan er ongecommitte wijzigingen in de BRONPADEN VAN HET IMAGE, dan komt er `-dirty` achter en
WEIGERT de task te publiceren. De hash beschrijft dan namelijk niet wat er gepubliceerd wordt, wat
juist de eigenschap is waar het om begonnen was. Eerder was dit een waarschuwing en publiceerde de
task toch; op productie draaide er dan een image waarvan niemand kan nagaan wat erin zit en dat ook
niet opnieuw te maken is.

Om dezelfde reden weigert de task een commit die op geen enkele remote staat: die bestaat na een
rebase of een verse clone niet meer, dus de tag wijst naar bron die niemand kan opzoeken. Dat wat in
productie draait herleidbaar is tot een vastgelegde wijziging, is wat BIO2 8.32 (wijzigingsbeheer)
en 8.19 (installeren van software) vragen.

De bronpaden en niet de hele repo: voor de operations-manager is dat `operations-manager/` (de
`COPY`-regels in `operations-manager/Dockerfile`), voor `publish-keycloak-image` zijn dat
`images/keycloak-zad/extensions/relay-email-sender`, `images/keycloak-zad/extensions/custom-mapper` en
`images/keycloak-zad`. Over de hele repo was de tag altijd `-dirty`, omdat
`publish-operations-manager` afhangt van `generate-env-secrets-for-operations-manager` en die het
secrets-bestand in `bootstrap/` herschrijft. Daarmee verloor `-dirty` zijn betekenis, en weigerde de
guard in `pin-operations-manager-image` ("komt van ongecommitte bron en hoort niet op productie") een
image dat in orde was.

De berekening staat in `scripts/calver-tag.sh`, dat zijn paden meekrijgt:

```yaml
IMAGE_VERSION:
  sh: scripts/calver-tag.sh operations-manager
```

Eén script en niet een blok per taak, want er stonden drie identieke kopieën en nu verschillen
alleen de paden. De vuilheidsmeting zelf doet `scripts/bron-tag.sh`, die hem al deed voor
`SANDBOX_OM_TAG`.

De tag opvragen zonder te bouwen:

```bash
task image-version
```

## Publiceren

```bash
task publish-operations-manager CLUSTER_TYPE=odcn-production
```

Die task doet drie dingen:

1. de CalVer-tag berekenen en tonen, en weigeren bij een dirty werkkopie of een commit die niet
   gepusht is. Dat gebeurt voor de bouw, zodat er geen image ontstaat dat toch niet gepind mag
   worden;
2. het image bouwen en pushen onder **twee** tags: de CalVer-tag en `latest`. Het is één build en één
   digest, dus `latest` kost niets extra en blijft werken voor scripts of handmatige pulls;
3. de odcn-production-overlay op de CalVer-tag pinnen.

Na afloop staat er een wijziging klaar in
`bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production/patches/deployment.yaml`.
Die commit je, waarna je deployt:

```bash
git add bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production/patches/deployment.yaml
git commit -m "deploy: operations-manager 2026.07.22.1432-7ccad61d"
CLUSTER_TYPE=odcn-production task deploy-operations-manager
```

De pin staat dus in git. Daarmee is de git-historie van dat ene bestand het draaiboek van wat er
wanneer in productie stond.

## Terugrollen

Zoek de vorige tag op en pin hem terug:

```bash
git log -p --follow bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production/patches/deployment.yaml
task pin-operations-manager-image IMAGE_TAG=2026.07.21.0913-4f2c9ab1
```

Die task weigert zelf ook een `-dirty`-tag, want pinnen en terugrollen zijn losse aanroepen die niet
via de publicatie lopen. Een oudere schone tag pinnen blijft gewoon werken.

Committen en deployen zoals hierboven. Er wordt niets herbouwd: je wijst naar een image dat al in de
registry staat, bit voor bit hetzelfde als wat er eerder draaide.

## De sidecar van ArgoCD en de back-upimage

Twee andere eigen images volgen sinds RC-348 dezelfde weg, met één verschil: ze dragen ook
de **digest**. Een tag in de registry is te overschrijven, een digest niet, en voor de
container die bij het renderen de SOPS-sleutel gebruikt is dat de tweede verdedigingslaag
(BIO2 8.19 en 8.09).

| Image | Publiceren | Pin in git |
|---|---|---|
| `rig-cmp-argo-kustomize-sops` | `task publish-cmp-kustomize-sops` | `task pin-cmp-image IMAGE_TAG=...` |
| `rig-backup` | `task publish-backup-image` | de standaard van `BACKUP_IMAGE` in `opi/core/config.py` |

Beide publicatietaken **weigeren** een `-dirty`-tag, waar `publish-operations-manager`
alleen waarschuwt: deze tags worden meteen in git gepind.

Voor deze twee images is nog nooit een CalVer-tag gepubliceerd: in de registry staat alleen
`latest`. De pin in git is daarom **alleen de digest** van de image die `latest` op 18 oktober
2026 aanwees. Dat is al onveranderlijk, en de eerste `task publish-*` zet er de CalVer-tag
voor: de pin wordt dan `<naam>:<tag>@<digest>`, en de tag maakt zichtbaar uit welke commit de
image komt.

### De sidecar staat drie keer in git

`pin-cmp-image` zet alle drie, en elk bestand houdt het registrypad dat het al had:

| Bestand | Wat het doet |
|---|---|
| `components/sops-plugin/kustomization.yaml` | de sidecar zelf, via `ghcr.io` (wat de sandbox pullt) |
| `components/cmp-odcn/kustomization.yaml` | de image-patch die op elk ODCN-cluster telt, via de rcr-spiegel |
| `overlays/odcn-production/argocd-deployment.yaml` | de ArgoCD-CR; de component vervangt `/spec/repo`, dus deze waarde bereikt het cluster niet |

Het rcr-pad staat in git omdat de ODCN-admission images daarheen herschrijft. Staat `ghcr.io`
in git, dan blijft ArgoCD OutOfSync op precies die herschrijving.

De digest komt uit de registry en niet uit een lokale bouw: `_pin-image-digest` leest hem met
`docker buildx imagetools inspect`. Diezelfde stap zet de digest ook bij
`pin-operations-manager-image`, zodat er één lezer is.

### De back-upimage komt uit de config

De zeven back-up- en restoresjablonen zetten `{{ backup_image }}` en niet meer een
registrypad. De waarde komt uit `BACKUP_IMAGE`; de standaard is de gepinde digest, en de
sandbox zet hem via zijn configmap op `:latest` omdat `task build-backup-image` de image daar
lokaal bouwt en onder die tag in kind laadt.

### Eén naam per image

De Taskfile publiceert naar `ghcr.io/minbzk/base-images/...`, `docker-images.yml` naar
`ghcr.io/rijksictgilde/zad/...`. Gepind wordt de eerste: daar staan deze twee images ook
werkelijk (gemeten op 18 oktober 2026 zijn de rijksictgilde-paden voor `rig-backup` en
`rig-cmp-argo-kustomize-sops` leeg). De workflow houdt zijn naam, want `GITHUB_TOKEN` mag
alleen in de eigen org pushen; bij die regel staat dat zijn naam niet gepind wordt.

### Een guard op de productiepaden

`tests/test_image_layer_guard.py::TestManifestImages` leest de manifesten die op ODCN
terechtkomen, de kustomizations volgend die ze opnemen, plus de standaard van `BACKUP_IMAGE`
en de zeven sjablonen. Een `latest`-tag of een verwijzing zonder tag is rood. De
uitzonderingen staan in `UITZONDERINGEN`, met de reden per regel.

## Wat bewust niet is meegenomen

- **De sandbox.** Die pusht naar de registry naast het cluster met de commit als tag
  (`docs/sandbox/sandbox-kind-registry.md`), en publiceert niet, dus er is geen rollback-probleem.
- **De overige images** (hello-world, argocd-rig, zad-waker). `docker-build-and-push`
  ondersteunt `IMAGE_TAG` en `EXTRA_TAG` voor iedereen, dus dit is per image een kleine stap
  zodra het nodig is.
- **Automatisch deployen na een publish.** De pin wordt geschreven, niet gecommit en niet
  uitgerold. Dat blijft een bewuste handeling.
