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

Staan er ongecommitte wijzigingen in de werkkopie, dan komt er `-dirty` achter en waarschuwt de task.
De hash beschrijft dan namelijk niet wat er gepubliceerd wordt, wat juist de eigenschap is waar het
om begonnen was.

De tag opvragen zonder te bouwen:

```bash
task image-version
```

## Publiceren

```bash
task publish-operations-manager
```

Die task doet drie dingen:

1. de CalVer-tag berekenen en tonen (met een waarschuwing bij een dirty werkkopie);
2. het image bouwen en pushen onder **twee** tags: de CalVer-tag en `latest`. Het is één build en één
   digest, dus `latest` kost niets extra en blijft werken voor scripts of handmatige pulls;
3. de odcn-production-overlay op de CalVer-tag pinnen.

Na afloop staat er een wijziging klaar in
`bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production/patches/deployment.yaml`.
Die commit je, waarna je deployt:

```bash
git add bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production/patches/deployment.yaml
git commit -m "deploy: operations-manager 2026.07.22.1432-7ccad61d"
task deploy-operations-manager CLUSTER_TYPE=odcn-production
```

De pin staat dus in git. Daarmee is de git-historie van dat ene bestand het draaiboek van wat er
wanneer in productie stond.

## Terugrollen

Zoek de vorige tag op en pin hem terug:

```bash
git log -p --follow bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production/patches/deployment.yaml
task pin-operations-manager-image IMAGE_TAG=2026.07.21.0913-4f2c9ab1
```

Committen en deployen zoals hierboven. Er wordt niets herbouwd: je wijst naar een image dat al in de
registry staat, bit voor bit hetzelfde als wat er eerder draaide.

## Wat bewust niet is meegenomen

- **Lokaal en sandbox.** Die bouwen een niet-gepubliceerde `operations-manager:latest` en laden die
  rechtstreeks in Kind. Er is geen registry en dus geen rollback-probleem. De sandboxed-local-overlay
  blijft daarom op `latest` staan.
- **De overige images** (rig-backup, cmp-kustomize-sops, hello-world). `docker-build-and-push`
  ondersteunt `IMAGE_TAG` en `EXTRA_TAG` inmiddels voor iedereen, dus dit is per image een kleine
  stap zodra het nodig is.
- **Automatisch deployen na een publish.** De pin wordt geschreven, niet gecommit en niet
  uitgerold. Dat blijft een bewuste handeling.
