# Release oktober 2026: wat er na de uitrol nog moet

Eerst vier controles direct na de uitrol, daarna één taak die pas ná de uitrol kan. De rest
van deze release is af en zit op main.

## Controles direct na de uitrol

Bron: de releasecontrole (`plans/releasecontrole-oktober-2026-uitkomst.md`) en de
releasenotitie. Alles hieronder is lezen in productie, behalve de refresh in punt 2.

1. **De ArgoCD-CR is echt toegepast.** RC-250 zet `--sync-timeout=600` in
   `spec.controller.extraCommandArgs` en RC-252 zet argocd-server op request 384Mi en limit
   1Gi, allebei in `bootstrap/rig-system/.../odcn-production/argocd-deployment.yaml`. Een
   eerdere fix op die CR is ooit nooit toegepast. Toets dat de controller met die vlag draait
   en dat argocd-server de nieuwe resources heeft; zo niet, dan is de CR blijven liggen en
   moet de controller herstarten.
2. **De projecten met een database op de standaardimage refreshen.** Die stonden in ODCN
   permanent OutOfSync (bijvoorbeeld mb-docs), omdat de CNPG-image niet langs het rcr-pad
   ging (`88112c326`). Na de uitrol is één refresh per project genoeg; daarna moeten ze
   Synced staan.
3. **De drie zombie-deployments van 9 oktober nalopen**: `dd-mco-pr-41`, `pm-5sj-pr139` en
   `toets-hn7-pr-32`, alle drie met een publieke Ingress. RC-254 voorkomt nieuwe zombies,
   maar ruimt bestaande niet op. Staan ze er nog, dan opnieuw verwijderen.
4. **De rcr-proxy kan de Keycloak-image halen.** `rcr.rijksapps.nl/ghcr-rig/rijksictgilde/zad/keycloak-zad:2026.10.13.1948-72684cb4`
   is nooit vanuit ODCN opgehaald; de bron op ghcr.io antwoordt wel. Kijk direct na de uitrol
   of de nieuwe Keycloak-pod Running is. De deployment heeft de standaard RollingUpdate met
   één replica, dus bij een ImagePullBackOff blijft de oude pod draaien en werkt inloggen
   nog. Maar bij de eerstvolgende herstart van die oude pod is het inloggen op het hele
   platform weg, dus los het meteen op of zet de vorige commit terug.

## De taak

Het Docs-project krijgt een eigen titel op zijn uitnodigingspagina's.

Wie een uitnodigingslink voor Docs of Grist opent, ziet nu de `display-name` van het
project als kop, en dat is de naam van de beheerder ("MijnBureau Docs Helmfile (ODCN)")
en niet van het product dat de uitgenodigde kent. PR #191 voegde daarvoor een optioneel
veld `title` toe aan een uitnodiging. Leeg valt het terug op `display-name`, daarna op de
projectnaam, dus zonder deze taak blijft alles werken zoals het was: er staat alleen nog
de verkeerde kop.

De waarde die erin moet:

```yaml
title:
  nl: Docs en Grist
  en: Docs and Grist
```

## Eerst de poort: draait prod al een OPI die dit veld kent?

**Niet doen voordat dit klopt.** `InviteEntry` staat op `extra="forbid"`, dus een OPI die
het veld niet kent weigert het hele projectbestand. Je zet dan niet een kop verkeerd, je
maakt het bestand ongeldig voor de draaiende versie.

Het veld zit in de squash-commit `023249da4` van 6 oktober 2026. De odcn-overlay pinde op
het moment van schrijven:

```
rcr.rijksapps.nl/ghcr-rig/minbzk/base-images/operations-manager/operations-manager:2026.09.02.2241-d81cdab4-dirty
```

Dat is een bouw van 2 september, dus **prod kende het veld toen nog niet**. Toets opnieuw
voor je begint:

```bash
git -C <checkout> show origin/main:bootstrap/rig-system/kustomize/operations-manager/overlays/odcn-production/patches/deployment.yaml \
  | grep 'image:'
```

Staat daar een tag van ná 6 oktober 2026, dan is de poort open. Twijfel je of die bouw de
commit echt bevat, kijk dan naar het commit-deel in de tag (`...-<sha>`) en toets
`git merge-base --is-ancestor 023249da4 <sha>`.

## Route A: via de portal, de makkelijke weg

Open het Docs-project, ga naar de service **Uitnodiging** en vul bij de uitnodiging die je
wilt aanpassen in:

| veld | waarde |
|---|---|
| Titel (Nederlands) | `Docs en Grist` |
| Titel (Engels) | `Docs and Grist` |

Het veld is optioneel en heeft bewust geen berekende standaardwaarde: die zou de
`display-name` van vandaag in het bestand vastzetten. Leeg laten betekent dus "val terug",
niet "leeg".

Opslaan verandert niets aan de applicatie en veroorzaakt geen nieuwe uitrol; een
uitnodiging aanpassen raakt alleen de pagina's.

## Route B: rechtstreeks in het projectbestand

### Welk bestand

Projectbestanden staan niet in deze repo maar in de git-repo **`zad-projects`**, als
`projects/<projectnaam>.yaml`. De projectnaam van Docs staat hier niet in de repo en die
verzin ik niet. Zoek hem op met:

```bash
grep -l 'MijnBureau Docs' projects/*.yaml
```

in een checkout van `zad-projects`, of pak hem uit de projectlijst in de portal.

### Welke plek in het bestand

Het pad is `services` → `invite` → `config` → `active` → de uitnodiging → `title`. Dus
binnen het item in `active` dat de juiste `key` heeft, op hetzelfde niveau als `message`:

```yaml
services:
  - invite:
      config:
        default-language: nl
        active:
          - key: <de bestaande sleutel, niet aanraken>
            title:                      # <-- dit blok erbij
              nl: Docs en Grist
              en: Docs and Grist
            message:
              nl: Welkom bij Docs en Grist.
              en: Welcome to Docs and Grist.
            # de overige velden blijven zoals ze zijn
```

Twee valkuilen:

- **Niet het blok `invites:` op het hoogste niveau.** Dat is de legacy-vorm van vóór de
  invite-service. De lezer accepteert hem nog wel, maar nieuw werk hoort op
  `services/invite/config`. Staat er in dit bestand nog een top-level `invites:`, dan is
  het bestand nog niet gemigreerd en moet dat eerst.
- **Verwar `title` niet met `success-title`.** `title` is de kop van de
  uitnodigingspagina, `success-title` is de kop die je ná het inwisselen ziet. Beide
  bestaan en staan naast elkaar.

Hyphen of underscore in de sleutels maakt niet uit: `extract_invites_config` normaliseert
beide. `title` heeft geen alias, die heet in beide vormen `title`.

## Toetsen dat het goed staat

1. Open een uitnodigingslink van het project, `/invite/<sleutel>`. Boven de naam staat "Je
   bent uitgenodigd voor", en de kop is nu "Docs en Grist" in plaats van de display-name.
2. Zet je browsertaal op Engels of vraag de pagina in het Engels op, en kijk of de Engelse
   titel verschijnt.
3. Haal de titel weer weg en kijk of de kop terugvalt op de display-name. Dat is de toets
   die bewijst dat het veld echt optioneel is en niemand anders breekt.

## Context

- PR #191, gemerged als `023249da4` op 6 oktober 2026. Het veld landt in
  `invite.v1.1.json`; de PR-beschrijving zegt nog 1.0, maar main stond toen al op v1.1 en
  v1.0 is bevroren historie.
- De bijbehorende CI-reparaties zitten in #192 (`28bfffcc2`) en de dependency-bump in #201
  (`56c838241`).
