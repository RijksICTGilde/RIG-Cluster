# Een delete in de tenant eindigt schoon, met de manifestmap erbij (#205)

Het verwijderen van een deployment werkt in de tenant `rig-prd` op `odcn-production`, maar meldt soms vals "failed" en laat dan de manifestmap in de deployments-repo achter. Die map wordt daarna nooit meer opgeruimd. Getoetst op 9 oktober 2026 voor 15 deletes (PR 349 tot 362 in mpfb-8wh, mpfm-w3h en mpfpsm-lcl): cluster, projectbestand en argo-repo waren bij alle 15 schoon, de deployments-repo bij 13. `mpfb-8wh/pr-360` en `mpfpsm-lcl/pr-360` bleven staan; op 8 oktober gebeurde hetzelfde met `mpfb-8wh/pr-334` en `mpfpsm-lcl/pr-334`.

Issue: #205 op het ZAD-bord.

## Hoe het misgaat

In `opi/manager/delete_project_manager.py`, `delete_deployment`:

1. OPI verwijdert de Application uit de argo-repo, beëindigt de lopende operatie, en wacht (`wait_for_application_deletion`, 40 x 5 seconden).
2. Bevestigt ArgoCD het niet op tijd en staat `force` aan, dan volgt `_force_delete_stuck_application`: inventaris van de namespace, verwijderen wat bij de Application hoort, en pas dan de finalizer weg.
3. In de tenant mag de serviceaccount `namespace-manager` ongeveer 90 types niet lijsten. De inventaris is daardoor nooit volledig, de opruiming telt als `partial`, en de guard laat de finalizer terecht staan. `argocd_app_deleted` blijft `False`.
4. Stap 7 slaat de manifestmap over zolang `argocd_app_deleted` `False` is (terecht: een Application die nog bestaat, heeft zijn bron nodig). De map wordt alleen gemarkeerd voor uitgestelde opruiming.
5. Kort daarna maakt ArgoCD de cascade zelf af en verdwijnt de Application. Dat is gemeten: alle 15 waren in het cluster weg. Maar niemand kijkt nog: de taak staat op mislukt, en de nachtelijke reconciliation die de markering zou afhandelen draait in productie als dry-run (`RECONCILIATION_DRY_RUN` staat standaard op `True`).

Daarnaast logt `KubectlConnector.terminate_argocd_application_operation` bij elke delete een ERROR. Een json-patch `remove` op een afwezig `/operation` geeft op deze apiserver een generieke 422 ("The request is invalid: the server rejected our request due to an error in our request"), en de code herkent alleen "missing path" en "does not apply" als "niets te doen".

## Wat er moet gebeuren

1. **Na een gedeeltelijke opruiming opnieuw wachten.** Houdt `_force_delete_stuck_application` de finalizer vast omdat de opruiming `partial` was, wacht dan nog een keer met `wait_for_application_deletion` tot de Application weg is (de cascade van ArgoCD, die door de beëindigde operatie en de verwijderde resources inmiddels vrij is). Is hij weg, dan geldt `argocd_app_deleted = True` en loopt stap 7 gewoon door. Is hij er na die wachttijd nog, dan blijft het huidige gedrag: markeren voor uitgestelde opruiming en een fout melden.
   - De guard zelf verandert NIET: een halve inventaris blijft een reden om de finalizer niet weg te halen. Wat verandert, is dat OPI de cascade die daarna toch loopt nog afwacht.
   - Kies de wachttijd bewust en schrijf op waarom (de 15 gemeten gevallen waren binnen enkele minuten weg).
   - De uitkomst in `deletion_results` moet kloppen: een delete waarvan na het wachten alles weg is, eindigt als geslaagd, met de partial-opruiming als operatie in de historie, niet als fout.
2. **`terminate_argocd_application_operation` leest eerst.** Lees `.operation` van de Application en patch alleen als hij bestaat. Dan hangt de code niet meer af van de tekst van een foutmelding. Een Application die niet bestaat blijft "niets te doen".
3. **Tests** die het gedrag vastleggen:
   - partial opruiming, Application daarna weg: stap 7 verwijdert de map, de delete is geslaagd;
   - partial opruiming, Application blijft staan: de map wordt gemarkeerd, niet verwijderd, en de delete meldt een fout;
   - geen lopende operatie: geen patch, geen ERROR;
   - wel een lopende operatie: patch zoals nu.
   Bouw voort op `tests/test_argocd_stuck_delete.py`, waar de huidige volgorde al is vastgelegd.
4. **Docs**: `features/argocd-vastgelopen-verwijdering.md` bijwerken (de sectie "Wat er nu gebeurt" en de passage over #205 onder "Wat hier niet in zit").

## Wat er buiten valt

- De volledigheidstoets in het force-pad soepeler maken of op gerenderde kinds baseren. Wachten op de cascade lost het op zonder de guard aan te raken; alleen als de tests laten zien dat dat niet volstaat, is dit de volgende stap, en dan als voorstel in de PR, niet gebouwd.
- Het besluit over `RECONCILIATION_DRY_RUN` in productie. Dat is een menselijke keuze, nadat iemand de dry-run-lijst (172 paden) heeft bekeken; de orphan-sweep gaf eerder op dezelfde dag een vals positief op een levende CNPG-database.
- Opruimen van mappen die al zijn blijven staan; dat is met de hand gedaan.
- Uitrol van OPI.

## Verifieerbaar

- De nieuwe tests uit punt 3 groen, samen met `tests/test_argocd_stuck_delete.py` en `tests/test_argocd_orphan_sweep.py`; ruff en pyright schoon.
- Indien de sandbox beschikbaar is (`orch sandbox claim`): een PR-deployment aanmaken en verwijderen met `force`, en laten zien dat cluster, projectbestand, argo-repo en deployments-repo daarna alle vier schoon zijn. Let op: de sandbox is geen tenant, dus daar treedt de partial-tak niet vanzelf op; laat in de PR zien hoe die tak wel geraakt is (test of nagebootst).
