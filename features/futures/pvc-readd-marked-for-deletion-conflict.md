# PVC-conflict: hertoevoegen van storage maakt de delete-markering niet ongedaan

## Status

Gepland, **bewust uitgesteld tot na de services-ombouw**. De persistent-storage- en
service-afhandeling wordt in die ombouw grondig herzien; deze fix nu doorvoeren zou te
veel conflicten geven. Dit document legt de volledige diagnose en de voorgestelde
oplossing vast zodat ze bij de ombouw meegenomen kunnen worden.

## Symptoom

Een deployment kwam in de portal in een mislukte staat: de ArgoCD-taak liep 300 seconden
in een sync-wait en faalde met "timed out waiting for sync; no progress reported". De
applicatie zelf was gezond (pods draaiden), en kort daarna stond de app gewoon op
`Synced`. Waargenomen op `ubbw-0i1/production` op 2026-07-27, maar het is niet
project-specifiek.

## Root cause

Niet de bekende ArgoCD-namespace-cache-invalidatie, niet load, geen echte deploy-fout.
Het is een ontbrekende **unmark-bij-hertoevoegen** in de PVC-generatie.

De keten:

1. Een component met `persistent-storage` wordt uit een deployment gehaald. OPP markeert
   de PVC correct voor uitgestelde verwijdering: `create_pvc_manifests_for_component`'s
   tegenhanger `handle_service_removal` **hernoemt** `<pvc>.yaml` naar
   `<pvc>.marked-for-deletion.yaml` en schrijft een record in de `marked_for_deletion`-tabel.
   Het hernoemde bestand blijft in de kustomize-map staan zodat ArgoCD de PVC tijdens de
   grace-periode niet meteen prunet.
2. Dezelfde component wordt (snel daarna) **weer toegevoegd** aan de deployment. OPP
   genereert de plain `<pvc>.yaml` opnieuw via `create_pvc_manifests_for_component`, maar
   dat aanmaak-pad kent geen unmark-logica: het verwijdert de `.marked-for-deletion.yaml`-twin
   niet en ruimt de DB-record niet op.
3. Nu staan `<pvc>.yaml` en `<pvc>.marked-for-deletion.yaml` samen in de gegenereerde
   `kustomization.yaml`, en beide definiëren dezelfde resource
   (`PersistentVolumeClaim/<deployment>-<component>-<storage>-pvc`).
4. Kustomize weigert twee resources met dezelfde id ("may not add resource with an already
   registered id"). De ArgoCD CMP-plugin (`rig-cmp-argo-kustomize-sops`) kan geen manifests
   genereren, dus ArgoCD rapporteert `sync=Unknown`. OPP's sync-wait eist `Synced+Healthy`,
   ziet 300 s lang `Unknown`, en faalt de taak.
5. Zodra de component opnieuw verwijderd wordt, verdwijnt de plain `<pvc>.yaml` en herstelt
   het zichzelf. Vandaar de transiente, "spookachtige" aard.

## Bewijs (ubbw-0i1/production, 2026-07-27)

De typesense-component werd in vier snelle modal-edits heen en weer getoggeld in de
`production`-deployment:

| tijd (CEST) | projectfile-commit | production-componenten | typesense/PVC |
|---|---|---|---|
| 09:47:14 | 1e6142e55 | [documentatie] | weggehaald -> PVC marked-for-deletion |
| 09:49:13 | 8bc2876da | [documentatie, typesense] | teruggezet -> plain PVC opnieuw gegenereerd |
| 09:57:13 | 34c3a027b | [documentatie, typesense] | aanwezig -> nog steeds beide twins |
| 09:59:12 | 5ad7b711a | [documentatie] | weggehaald -> opnieuw gemarkeerd (opgelost) |

In de deployments-repo (`rig-cluster-application-test`, pad
`odcn-production/ubbw-0i1/production`) had commit `bf2121c8c` (09:49:20) beide bestanden in
`resources:`, elk met `name: production-typesense-data-pvc`. De CMP-fout in de
argocd-repo-server viel op 09:49:26 ("CMP processing failed ... error generating manifests
... may not add resource with an already registered id"). OPP's sync-wait van taak
`5c5e368a` liep van 09:49:26 tot de timeout op 09:54:29, met 60x `sync=Unknown,
health=Healthy`. Commit `04472adfb` (09:59:22) had de plain weer weg en de app werd Synced.

## Voorgestelde fix

### 1. Unmark bij (her)aanmaken van een PVC (kern)

In `opi/manager/pvc_manager.py`, `create_pvc_manifests_for_component`: wanneer een
PVC-manifest wordt aangemaakt of geregenereerd, moet OPP een eventuele
`<pvc>.marked-for-deletion.yaml`-twin **verwijderen** en de bijbehorende
`marked_for_deletion`-DB-record opruimen. De storage is duidelijk weer gewenst, dus de
eerdere markering is superseded.

- Het aanmaak-pad moet daarvoor de `MarkedForDeletionService` meekrijgen (die heeft het nu
  niet; `handle_service_removal` wel).
- Idempotent en veilig: als er geen twin is, gebeurt er niets.

### 2. `sync=Unknown` niet als harde mislukking behandelen (verdediging in de diepte)

In de ArgoCD-sync-wait (`opi/manager/argo_manager.py`): een langdurige `sync=Unknown` met
gezonde pods betekent dat ArgoCD de vergelijking niet kan berekenen (bijvoorbeeld een
CMP-generatiefout), niet dat de deploy is mislukt. Overweeg dit als informatieve melding
te behandelen in plaats van een 300 s-timeout die de taak laat falen, zodat een transiente
generatiehik geen valse "mislukt" plus ntfy-melding oplevert. Dit is symptoombestrijding;
fix 1 neemt de oorzaak weg.

## Twee bedoelingen bij hertoevoegen (waarschijnlijk een UI-keuze)

Automatisch unmarken is niet in alle gevallen wat de gebruiker wil. Wie een storage die
voor verwijdering gemarkeerd stond weer toevoegt, kan twee dingen bedoelen, en OPP kan dat
niet uit de projectfile afleiden:

1. **"Ik wil een lege schijf."** De oude data hoort weg. Dan moet OPP een **nieuwe
   generatie** maken: de generation van de storage ophogen zodat er een PVC met een nieuwe
   naam ontstaat, en de oude (marked-for-deletion) PVC laten opruimen door de
   grace-periode. In dit pad ontstaat geen conflict, want de namen verschillen.
2. **"Ik wil de eerder verwijderde schijf hergebruiken."** De data moet behouden blijven.
   Dan moet OPP dezelfde PVC herkoppelen: de `.marked-for-deletion.yaml`-twin verwijderen en
   de `marked_for_deletion`-DB-record opruimen (de unmark van fix 1). Zelfde generatie,
   zelfde naam, data blijft.

De huidige impliciete werking is variant 2 (bij hertoevoegen wordt dezelfde PVC-naam
opnieuw gegenereerd), alleen zonder de opruiming, wat precies dit conflict veroorzaakt. Fix
1 maakt variant 2 correct.

Omdat de bedoeling niet af te leiden is, hoort hier een **expliciete keuze** bij, vermoedelijk
in de UI op het moment dat een gebruiker een storage terugzet die nog gemarkeerd staat:
"lege schijf (nieuwe generatie)" versus "eerder verwijderde schijf hergebruiken". Overwegingen:

- Veilige default is hergebruiken (variant 2), want dat is niet-destructief; een lege schijf
  is dan een bewuste extra actie.
- De controle "staat deze storage nog marked-for-deletion?" kan OPP zelf doen op basis van
  de `marked_for_deletion`-tabel; alleen dán is de vraag relevant.
- Bij variant 1 (nieuwe generatie) moet ook de oude DB-record en het oude bestand netjes
  hun grace-periode-pad volgen, niet blijven hangen.

Dit UI-aspect kan groter zijn dan de kern-fix en past qua timing goed bij de services-ombouw,
waar het storage/generation-model toch op de schop gaat.

## Verificatie

- Reproductie: toggle een component met `persistent-storage` uit en weer in een deployment
  (twee reprocessing-rondes). Verwacht: nooit twee twins van dezelfde PVC in de
  gegenereerde `kustomization.yaml`, en geen achtergebleven `marked_for_deletion`-record.
- Test faalt aantoonbaar op de huidige code (dubbele PVC), slaagt na fix 1.

## Relevante bestanden

- `opi/manager/pvc_manager.py`: `create_pvc_manifests_for_component` (aanmaak, mist unmark),
  `handle_service_removal` (markering), `MARKED_FOR_DELETION_SUFFIX`.
- `opi/manager/delete_project_manager.py`: de removed-service-detectie
  (`deployment_uses_service`-vergelijking van previous vs current) die de markering
  aanstuurt. Die detectie is correct; het probleem zit puur in het ontbreken van unmark bij
  hertoevoegen.
- `opi/generation/manifests.py`: `create_kustomization_files` / `collect_manifest_files`,
  die beide twins in `resources:` opnam. Eventueel hier een vangnet: nooit een PVC én zijn
  `.marked-for-deletion.yaml`-twin samen opnemen.
- `opi/manager/argo_manager.py`: de sync-wait met de `sync=Unknown`-afhandeling (fix 2).
