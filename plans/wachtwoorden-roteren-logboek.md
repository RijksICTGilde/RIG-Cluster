# Logboek: wachtwoorden roteren per component

Verloop van dit werk. De verhouding tot de andere documenten: `plans/wachtwoorden-roteren-per-component.md`
was het voorstel (26-09), `plans/wachtwoorden-roteren-flow-analyse.md` de gemeasured analyse
(28-09), `docs/wachtwoorden-roteren.md` het draaiboek. Dit bestand houdt bij wat er gebeurde,
in omgekeerde volgorde.

## 2026-09-28 (middag) — De repetitie op de sandbox, beide rotaties gedaan

Eerste echte rotaties via `scripts/edit-secret.py --component <naam> --apply`, tegen
`kind-rig-sandbox`. Drie bevindingen, allemaal in code en toetsen verwerkt dezelfde dag:

1. **De superuser reconciliet de operator NIET** binnen het venster (operator-logs tonen
   alleen Cluster-defaulting/validation rond de sync). `postgresql` verhuisd naar live-app
   (ALTER USER eerst), cnpg-secret resteert voor de managed roles.
2. **De CNPG managed-role-route werkt wél** en is daarmee bewezen: bij `keycloak-db` landde
   de gate met "nieuwe waarde werkt, oude faalt". De asymmetrie staat nu op een meting,
   niet op documentatie.
3. **De sync-gate bewees zijn nut binnen de eerste uur**: toen een mirror-push een
   bestandswijziging liet liggen, weigerde de tool stil door te gaan (cluster-secret droeg na
   180s de nieuwe waarde niet) in plaats van de consumer op de oude omgeving te herstarten.

Verloop in punten: drift-recoverypad getest in het wild (file≠app → vraagt de app-waarde →
herstelt); beide rotaties eindtoestand groen ("oude faalt / nieuwe werkt"); OPI en Keycloak
herstart en Ready; slotcontrole `--check --all`: 15 groene regels, geen melding. Laat op de dag
ook nog de bulk-vorm strakgetrokken: bij een ronde met meerdere onderdelen waren er tot dan
per onderdeel restarts (ZAD tot zes keer); nu stelt `steps_for(defer_restarts=True)` die uit
naar één slotafronding, gededupliceerd, ZAD als laatste. Toetsen: 69 groen; ruff/pyright schoon.

## 2026-09-28 — Bouw van het gereedschap; twee driften aan het licht

Gedaan (worktree `RIG-Cluster-edit-secret`, nog te committen):

- `scripts/age_keyring.py`: de keyring (env → `~/.config/sops/age/keys.txt` → `security/*.txt`),
  selectie op recipient met bronvermelding, duplicaat-melding, en `probe()` voor metadata-loze
  waarden (eerst pad-regel; bij mismatch meten welke sleutel wél opent, melden, stoppen).
- `secret_edit.py`: `encrypt()` bewaart de recipient-set en bewijst met een decrypt-roundtrip.
- `secret_rotate.py`: categorie-model; Argo-sync via de Application-CR (refresh-annotatie,
  operation-patch, `operationState`-uitkomst) + secret-poll-gates + CNPG-operator-gate;
  nieuwe componenten `keycloak-db` en `forgejo-db`; `postgresql` en `mail-db` naar de
  cnpg-volgorde (de ALTER USER-volgorde was een race: de operator zette hem terug).
- Herkenning van legacy/plaintext overlay-bestanden + migratie in dezelfde ronde
  (schrijf canoniek versleuteld, verbind `decrypt-sops.yaml`/`kustomization.yaml`, plaintext weg).
- `--seed-overlay` op `edit-secret.py`, om overlays te schrijven voor clusters die er geen
  hebben. Eerste designfout meteen gevangen: de dry-run-guard stubde ook de leesacties, dus
  de preview rapporteerde nul secrets; lezen is read-only en hoort daar buiten.

Metingen onderweg:

- Beide clusters: de elf infra-secrets zijn Argo-beheerd met `automated + selfHeal`
  (sandbox via app `sandbox-infrastructure` uit de Forgejo-mirror; odcn via
  `production-infrastructure` uit GitHub `RIG-Cluster@main`). Op odcn toont alleen de
  **tracking-id-annotatie** het eigenaarschap; het `app.kubernetes.io/instance`-label ontbreekt
  daar. Wie alleen het label leest, ziet overal drift die er niet is.
- Bootstrap-set (geen Argo-eigenaar, bewust): `sops-age-key`, `argocd-secret`,
  `argocd-admin-credentials`, `operations-manager-env-secrets`, `operations-manager-keycloak`,
  pull-robots, dex-tokens, letsencrypt-keys.
- De MinIO-"drift" uit het voorstel bestaat niet: de CMP injecteert de destination-namespace
  over de hele build (`configmap-sops-plugin.yaml:272-276`), dus op odcn delen secret en
  deployment `rig-prd-operations`.
- `overlays/odcn/keycloak-db-credentials.yaml` is byte-identiek aan de template (inclusief
  de nooit uitgevoerde `@secret-gen`-annotatie): nooit gegenereerd, wél toegepast.
- Correctie onderweg (zelfde middag): de Forgejo-mirror bleek de canonieke SOPS-overlay
  óók al te bevatten, met waarden gelijk aan live (steekproef van drie secrets, allemaal
  gelijk). De eerdere notitie "alleen in de mirror, plaintext" klopt niet; de map is in
  deze repo alleen onzichtbaar door `.gitignore`. De seed-functie blijft nuttig voor wie
  de overlay lokaal niet heeft.
- Seed-preview op sandbox na de guard-fix: 13 templates → 13 live secrets teruggelezen
  (aantallen velden kloppen), 0 geschreven zoals bedoeld.

Validatie van de dag: 67 tests groen (`test_secret_rotate.py`, `test_secret_edit.py`,
`test_age_keyring.py`), `ruff check` en `ruff format` schoon, `pyright` 0 fouten.

Open einde van de dag: afgesloten middag — de overlay `--seed-overlay --apply` gedraaid op
sandboxed-local (13 versleutelde bestanden + `decrypt-sops.yaml` + `kustomization.yaml` in
`overlays/sandboxed-local/`), nagecontroleerd met een decrypt-proef op
`keycloak-db-credentials-secret.yaml.sops.yaml` (namespace rig-system, de live-waarden
kloppen met het cluster). Nog open: review/commit door de mensen, daarna `task sandbox:sync`
en `--check --all` als regressie-bewijs; rotaties nog niet buiten dry-run uitgevoerd.

## 2026-09-27 — Parallelle security-analyse (context)

Dezelfde week liep een bredere security-analyse van het platform. De punten die dit spoor
raken — secret-drift, rotatie als terugkerend proces, en de dekking van templates — zijn hier
adresbaar gemaakt als tooling in plaats van als eenmalige actie.

## 2026-09-26 — Keycloak-adminwachtwoord met de hand

De rotatie van het Keycloak-adminwachtwoord kostte vier handelingen in twee pijplijnen,
waarvan één met de hand in Keycloak, en liet zien dat het wachtwoord op drie plekken met twee
waarden stond (waarvan één bestand, `keycloak-admin-secret.yaml.yaml.sops.yaml`, door niets
werd gerenderd). Gemeten toen: `KEYCLOAK_ADMIN_PASSWORD` en `SECRET_KEY` van de odcn-overlay
vervangen (commit 7310526df). Dat was de aanleiding van dit hele spoor.
