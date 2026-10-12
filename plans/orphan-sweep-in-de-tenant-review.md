# De orphan-sweep in de tenant: review en afronding

Op 9 oktober 2026 zat de tenant `rig-prd` op `odcn-production` op 99,9% van zijn CPU-limits. Een groot deel kwam van gesloten PR-previews waarvan de ArgoCD-Application al weg was maar de workloads niet: de oude forceerroute haalde de finalizer weg zonder op te ruimen (RC-226, gerepareerd sinds 28 september). `task argocd-orphan-sweep`, gebouwd om precies die resten te vinden, bleek in productie onbruikbaar. De opruiming is die dag met de hand gedaan; deze tak maakt het gereedschap bruikbaar en veilig.

Het werk staat al op de basistak, in vijf commits bovenop `forgejo/main`. Deze taak is **review, test, security en docs** van dat werk, plus reparatie van wat daarbij boven water komt. Geen nieuwe functionaliteit.

## Wat er op de tak staat

1. **De sweep werkt in een Capsule-tenant.** Het tenant-account mag ~90 types niet lijsten, dus elke namespace antwoordde voor een deel en de sweep weigerde alles. Met de deployments-repo erbij beslist nu wat die repo rendert: die kinds worden apart opgevraagd en moeten allemaal antwoorden. Kinds worden uit de regels `apiVersion:`/`kind:` gelezen, omdat helmfile-charts templates zijn. Nieuw in de connector: `list_namespaced_kinds`.
2. **Drie lagen vanuit git.** Met `--projects-repo` en `--argo-repo` meldt hij ook Applications in de argo-repo voor een deployment die het projectbestand niet meer heeft. Verwachte paden komen uit de naming-functies van OPI, projectbestanden worden na `migrate_to_latest()` gelezen. Alleen melden, niet verwijderen.
3. **Helmfile-projecten blijven buiten schot** in alle lagen, met dezelfde toets als `ProjectManager._deployment_uses_helmfile`.
4. **Wat nooit een wees is.** Een resource met een `ownerReference` (gemeten: de pod en services van de levende CNPG-database `algor-odc-db` werden als wees gemeld, `--delete` had ze verwijderd), en Endpoints/EndpointSlices. Dit zit in `opi/utils/argocd_tracking.py` en `opi/connectors/kubectl.py`, dus het raakt ook de forceerroute van OPI (`_force_delete_stuck_application`).
5. **De git-kant kijkt alleen naar het eigen cluster.** Anders las de hele `local/`-boom als wees.
6. **De task** geeft `CLUSTER_TYPE`/`RIG_NAMESPACE` uit `select-cluster` door als `CLUSTER_MANAGER`/`ARGOCD_MANAGER`, en `GITOPS=<map>` levert de drie checkouts.
7. **Docs**: `features/argocd-vastgelopen-verwijdering.md`, sectie "De veegactie", en `scripts/README.md`.

## Wat er moet gebeuren

1. **Review met de nadruk op te veel verwijderen.** Dit gereedschap verwijdert clusterresources met `--delete`. De vraag per wijziging is: kan hiermee iets levends als wees worden gelezen? Let in het bijzonder op:
   - de volledigheidstoets: is "elk gerenderd kind antwoordde" sluitend, ook voor `Kind.*` (getemplatete `apiVersion`) en voor kinds die alleen in getemplatete `kind:`-regels staan (die worden gemeld, niet meegeteld);
   - de helmfile-uitsluiting: alleen actief met de projects-repo erbij; is dat acceptabel of moet `--delete` zonder projects-repo weigeren?
   - de `ownerReference`-regel in de forceerroute van OPI: verandert die wat de forcering verwijdert op een cluster met label-tracking (sandbox)?
2. **Testen**: de bestaande en nieuwe unittests (`tests/test_argocd_orphan_sweep.py`, `tests/test_argocd_stuck_delete.py`, `tests/test_kubectl_connector.py`), plus de rest van de suite die de connector raakt. Als de sandbox beschikbaar is (`orch sandbox claim`): `task argocd-orphan-sweep` daar draaien in meldmodus, zonder `DELETE`, en de uitkomst rapporteren. De sandbox gebruikt label-tracking, productie annotation-tracking; beide horen te werken.
3. **Security**: het gereedschap verwijdert resources en leest projectbestanden; geen geheimen in uitvoer of logs.
4. **Docs**: nalopen of de featuredoc klopt met de code.

## Wat er buiten valt

- De volledigheidstoets in de forceerroute van OPI zelf: daar eindigt elke forcering in de tenant als `partial`, waardoor stap 7 (de manifestmap) wordt overgeslagen. Dat is issue #205, een eigen taak.
- Doorlopende detectie via `orphanedResources: warn` in het AppProject (voorstel in de featuredoc).
- Uitrol van OPI. Er wordt niets uitgerold.
- Het draaien van de sweep op productie; daar heeft de server geen toegang toe en dat hoort ook niet.

## Verifieerbaar

- De drie testbestanden groen, ruff en pyright schoon.
- Een review die per punt onder "Wat er moet gebeuren" 1 een oordeel geeft, niet alleen "ziet er goed uit".
- Indien de sandbox beschikbaar was: de uitvoer van de sweep in meldmodus, met per gemelde wees een verklaring.
