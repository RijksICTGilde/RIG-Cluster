# Hervatten: de mailrelay op de sandbox zetten

**Geschreven op**: 19 augustus 2026, als overdracht naar een volgende sessie
**Branch**: `claude/mailrelay-ready`, commit `ce9456c4`, staat ook zo op Forgejo
**Achtergrond**: `plans/mailrelay.md` (ontwerp), `features/send-email.md` (de dienst),
`docs/ron-koppeling.md` (de koppeling en de metingen)

## Waar het staat

De dienst `send-email` is af: 34 commits eigen werk, gemerged met
`fixes-na-release-augustus` (nog 2 commits achter), ruff en pyright schoon, 9646 tests
groen.

**En hij draait nergens.** De namespace `rig-ron` bestaat niet op de sandbox
(`kubectl --context kind-rig-sandbox get ns rig-ron` → NotFound). Wat RC-140 heeft
opgebouwd is weer weg; wat overblijft is dat het bewezen werkt. De identiteitstoets is
op 19 augustus geslaagd met exitcode 0, en het antwoord op de TLS-vraag staat in
`docs/ron-koppeling.md`: Stalwart valt NIET terug op platte tekst, dus dat is een
garantie en geen voorkeur.

## Drie dingen in de omgeving die je eerst moet regelen

**1. De worktree mist wat `task` nodig heeft.** `.env-taskfile-current` en `security/`
staan in `.gitignore` en leven alleen in de hoofdcheckout `../RIG-Cluster`. Zonder die
twee stopt elke taskopdracht met "No cluster selected", en zonder de AGE-sleutel kun je
het geheim niet versleutelen.

**2. De hoofdcheckout staat op een branch zonder dit werk** (`fixes-na-release-augustus`).
`task sandbox:sync` duwt de `infrastructure/`-map van je wérkmap naar de Forgejo-repo
`zad-argo-infrastructure` in het cluster, dus die map moet de mailmanifesten bevatten.
Vanuit de hoofdcheckout synct hij een boom zonder mail. **Wissel daar niet van branch**:
dat is een gedeelde checkout en dat zet andermans werk op het spel.

**3. De clusterkeuze staat op PRODUCTIE.** In `../RIG-Cluster/.env-taskfile-current` staat
`Taskfile environment variables for ODCN-PRODUCTION cluster`, en de kubectl-context stond
op `odcn-rig-production`. Zet dat om vóór je iets draait.

Oplossing voor alle drie, en het vervuilt de branch niet want beide zijn gitignored:

```bash
cd /Users/robbertuittenbroek/IdeaProjects/RIG-Cluster-mailrelay
cp ../RIG-Cluster/.env-taskfile-sandboxed-local .env-taskfile-current
cp -r ../RIG-Cluster/security .
kubectl config use-context kind-rig-sandbox
```

## Het stappenplan

1. **De drie kopieerstappen hierboven.** → verifieer: `kubectl get ns` toont `rig-system`.

2. **De geheimen staan al klaar** (aangemaakt op 20 augustus 2026, samen met de
   structurele fixes hieronder). Voor `sandboxed-local` staan
   `mail-relay-secret.yaml.sops.yaml` en `mail-db-credentials-secret.yaml.sops.yaml` in
   `infrastructure/bootstrap/infrastructure/secrets/config/overlays/sandboxed-local/`
   (gitignored, dus alleen in de hoofdcheckout op deze machine); voor `odcn` staan ze
   versleuteld in git. Ontbreken ze ergens toch: `task generate-secrets-for-cluster`
   slaat bestaande geheimen sinds diezelfde fix per bestand over, dus die kan nu veilig
   draaien zonder rotatie van Keycloak, MinIO of PostgreSQL.

   Wat er structureel is veranderd (de handstappen van dit plan zijn vervallen):
   de sjablonen zijn cluster-agnostisch gemaakt (`MAIL_UPSTREAM_HOST` en `MAIL_DB_HOST`
   staan nu in het Deployment; de basis draagt productie, de overlays en de
   sink-component zetten ze om), en de databasegebruiker staat in een eigen
   `mail-db-credentials` in de vorm die CNPG wil.

   → verifieer: `sops --decrypt` op beide bestanden toont `rig-system` als namespace.

3. **De database komt declaratief mee.** De rol `mailrelay` en de database staan in
   `infrastructure/bootstrap/infrastructure/postgresql/database/base/` (managed role +
   Database-resource, wachtwoord uit `mail-db-credentials`); CNPG maakt ze aan zodra de
   wijziging synct, ook op een cluster dat al draait. Er is geen handmatige
   CREATE DATABASE meer, op geen enkel cluster.

4. **De overlay bouwen** → verifieer: bouwt zonder fout en bevat `rig-mail-relay`,
   `rig-mail-sink`, het geheim en het netwerkbeleid, allemaal in `rig-ron`:
   ```bash
   SOPS_AGE_KEY="$(sed -n '3p' security/sandbox-key.txt)" kustomize build \
     --enable-alpha-plugins --enable-exec --load-restrictor LoadRestrictionsNone \
     infrastructure/bootstrap/infrastructure/mail/controller/overlays/sandboxed-local
   ```

5. **`task sandbox:sync`**, en wachten tot ArgoCD de Application `ron-infrastructure`
   oppakt → verifieer: `kubectl -n rig-ron get pods` toont `rig-mail-relay` en
   `rig-mail-sink` allebei Running.

6. **Een account**: OPI maakt bij het opstarten zijn platformaccount aan
   (`ensure_platform_mail_account`) en bewaart het wachtwoord in de Secret uit
   `MAIL_PLATFORM_SECRET_NAME` in de OPI-namespace → verifieer: die Secret bestaat.

7. **De toets draaien.** Dit is de assertie; slaagt hij, dan is de samengevoegde stand goed:
   ```bash
   kubectl -n rig-ron port-forward svc/rig-mail-relay 1587:587 &
   kubectl -n rig-ron port-forward svc/rig-mail-sink 8025:8025 &
   cd operations-manager/python
   uv run python scripts/mail_identity_check.py --user <account> --password <geheim>
   ```
   → verifieer: exitcode 0.

## Twee valkuilen die al geld hebben gekost

- **Zet de mail-overlay NIET in `infrastructure/bootstrap/clusters/sandboxed-local/kustomization.yaml`.**
  De CMP-plugin forceert daar `namespace: rig-system` op de hele build, en dat trok de
  `rig-ron`-resources plat tot een ID-conflict op een dubbele `mail-relay-credentials`
  Secret. De relay draait via zijn eigen ArgoCD Application `ron-infrastructure`
  (`bootstrap/rig-system/kustomize/overlays/sandboxed-local`).
- **Een ConfigMap die via `subPath` is aangekoppeld wordt nooit bijgewerkt.** Als de relay
  zich niet gedraagt zoals de configmap zegt, draait hij op oude configuratie. Pod
  herstarten.

## Wat er NIET van deze taak is

Vier vervolgpunten staan bewust apart en horen niet in deze ronde:

1. een burst-limiter naast de daglimiet (een dagplafond stopt geen uitbarsting)
2. het besluit of het spamfilter aan gaat, nu `auto-update` uit staat
3. `messages = 10` per SMTP-sessie is te laag en levert nauwelijks iets op
4. of een outbound throttle op `sender` het plusdeel meeneemt (bepaalt of afknijpen per
   project kan)

En twee dingen falen op de hoofdlijn zelf, dus niet aan beginnen: de test
`test_template_structure.py::test_content_blocks_are_compositions` (over
`bg/router.html.j2`) en `ruff format` op
`opi/services/catalog/cross_domain_access/config_model.py`.

## Daarna: wat productie nog nodig heeft

De odcn-overlay blijft uitgecommentarieerd. Voor die stap is nodig: het geheim voor odcn,
de afspraak met het mailteam dat wij als `noreply-rijksapp@rijksoverheid.nl` versturen, en
een bounce-postbus die OPI over IMAP mag legen. DNS hoeft niet meer: het afzenderdomein is
`rijksoverheid.nl` en hun SPF autoriseert de upstream al.
