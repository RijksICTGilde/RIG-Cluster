# Alles groen op de sandbox, met de echte projectbestanden

Status: plan, 7 augustus 2026. Niet gebouwd. Aanleiding: er zijn vandaag twaalf taken gemerged. Elke afzonderlijke PR is geverifieerd, maar niemand heeft het geheel tegen een draaiend cluster gehouden met echte projectbestanden.

De sandbox is geclaimd voor deze run; er werkt niemand anders op.

## Waarom dit meer is dan de suite nog een keer draaien

De unittests draaien 6538 groen en de browsertests 151, maar allebei op een gemockte wereld. Wat er vandaag veranderd is, raakt juist de dingen die daar niet zichtbaar zijn:

- **de schemapoort in de wizard** (RC-44): een project dat het schema niet haalt wordt nu vroeg geweigerd. Dat is precies het gedrag dat de 22 productiebestanden van 6 augustus raakte;
- **`rollout=false`** (RC-46): opslaan zonder verwerken, met de drift die dat achterlaat;
- **de bijlage-endpoints** (RC-38, RC-52 en de hernoeming van vandaag): uploaden, verwijderen met bevestiging, en een pad dat vandaag van naam veranderd is;
- **tokenverificatie** (RC-51): een tweede manier om binnen te komen;
- **het haaksysteem en de basis-en-mutaties-verbouwing** (RC-39, RC-43): die raken elke wizard en elke dienst;
- **de templateopdeling** (RC-48): 125 bestanden aangeraakt, en vormgeving verplaatst naar CSS.

## Wat er getoetst moet worden

**1. Een verse image, op de sandbox.** Bouwen en uitrollen zoals het hoort, niet tegen een oude pod aan testen.

**2. Alle testsets die we hebben, tegen deze versie.** Niet alleen de snelle:

```
unit                6538 tests
e2e (niet-sandbox)   151 tests
e2e (sandbox)         42 tests   <- alleen deze draaien tegen het cluster
```

Die 42 sandbox-gemarkeerde tests zijn de kern van deze run; de andere twee sets zijn de nulmeting die moet blijven kloppen.

**3. De projectbestanden staan al klaar, geconverteerd.** Je hoeft ze niet zelf te maken en dat kan ook niet: er is de productiesleutel voor nodig om de geheimen te hersleutelen.

```
git.claude.robbertuittenbroek.nl/robbert/rig-cluster-projects-sandbox
  projects/   47 bestanden
```

Wat er al mee gedaan is: cluster naar `sandboxed-local`, domeinen naar `sandbox.rijksapp.dev`, repository-urls naar de sandbox-Forgejo, een sandbox-beheerder, elk component op de probe-image (`ghcr.io/minbzk/base-images/e2e-allservices:latest`) met het probe-resourceprofiel (32Mi/10m, limiet 128Mi/200m), de tuner-historie eruit, de waarden van user-env-vars vervangen door een placeholder (26 projecten), en de AGE-geheimen hersleuteld naar de publieke sleutel van het sandbox-cluster op de server.

**Controleer voor je begint dat je de goede set hebt.** De eerste poging draaide op een foute set:

```bash
grep -rhE "^ *image:" projects | sort | uniq -c | sort -rn | head -3
```

Vrijwel alles hoort de probe-image te zijn. Zie je daar een projectnaam of een productie-image staan, dan heb je de oude set. De hele weg staat in `docs/productiebestanden-naar-een-sandbox.md`.

**Doe ze een voor een, niet in een batch.** Het is een kind-cluster: 47 tegelijk laten verwerken loopt vast op capaciteit en dan meet je de sandbox in plaats van de code. En bij een fout wil je weten welk project hem veroorzaakte.

**4. De schemapoort op de ONGECONVERTEERDE originelen.** Op 6 augustus haalden er 22 de rauwe schemavalidatie niet en 0 na migratie (`features/project-schema-versions.md`). Diezelfde meting hoort opnieuw gedaan te worden tegen deze versie, want de schemapoort van RC-44 is nieuw en de vraag is of hij hetzelfde oordeelt. Een bestand dat vandaag geweigerd wordt terwijl het gisteren verwerkt werd, is een regressie die je alleen zo vindt.

**5. De sandbox-sleutel, niet de productiesleutel.** `security/sandbox-key.txt`, en de context `kind-rig-sandbox`. Dat laatste is sinds `31d9a8cd` vastgepind in de taken, maar controleer het, want het is een keer misgegaan.

## Hoe de uitkomst gemeld moet worden

**Per onderdeel, niet als één oordeel.** "Het cluster is rood" is onbruikbaar als er twaalf taken in zitten. Wat er nodig is per bevinding:

- welke testset of welk bestand,
- wat er precies faalde (de melding, niet de samenvatting),
- en waar mogelijk: welke van de twaalf taken het raakt.

Dat laatste hoeft niet zeker te zijn; een vermoeden met de reden erbij is meer waard dan niets. De taken zijn RC-38 tot en met RC-49, RC-51, RC-52 en RC-53.

**En groen hoort ook onderbouwd te worden.** Niet "alles groen" maar de aantallen per set, zodat te zien is dat er echt gedraaid is en niets is overgeslagen.

## Volgorde

1. Sandbox-status controleren en de context bevestigen (`kind-rig-sandbox`).
2. Verse image bouwen en uitrollen.
3. De drie testsets draaien, met de aantallen erbij.
4. De 47 productiebestanden door de schemapoort halen, vóór en ná migratie, en de uitkomst vergelijken met de meting van 6 augustus.
5. Per bevinding terugmelden zoals hierboven.

## Waar op te letten

**Rood is hier informatie, geen mislukking.** Twaalf taken op één dag samenvoegen zonder één clustertest is de aanleiding; als er iets stuk is, is dit precies de run die dat hoort te vinden. Rapporteer volledig en repareer niet onderweg, want dan is niet meer te zien wat er kapot was.

**De sandbox is geclaimd, dus geef hem ook weer vrij** (`orch sandbox release`) als de run klaar is.

**Verifieer tegen de git-repo, niet tegen Loki.** Bij de VPA-tuner is eerder een verkeerde conclusie getrokken door naar afgekapte logs te kijken in plaats van naar wat er in `zad-projects` stond.

**Een bestand dat zichzelf herschrijft is verwacht gedrag.** Projectbestanden migreren pas bij verwerking, dus de eerste taak per project levert een diff op. Dat is geen bevinding; een bestand dat ná migratie de validatie niet haalt wél.
