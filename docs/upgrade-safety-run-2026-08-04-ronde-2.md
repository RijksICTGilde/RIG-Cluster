# Upgrade-safety test — run report ronde 2 (2026-08-04, server-sandbox)

**Dit is het uitvoerrapport voor RC-23.** Het draait het draaiboek uit
`features/upgrade-safety-test.md` op `kind-rig-sandbox`, nu met **alle zes projecten**.
Ronde 1 (`docs/upgrade-safety-run-2026-08-04.md`) kreeg er vier doorheen; de twee die
strandden zijn deze ronde alsnog gedekt, en de ene meting die ronde 1 niet kon doen is
alsnog gedaan.

## Oordeel (één zin)

**Deze release is veilig voor bestaande projectbestanden: de dienst-identiteit is over
alle 28 deployments van alle zes projecten volstrekt identiek, er verdwijnt niets uit de
gegenereerde manifesten (alle 51 gewijzigde onversleutelde bestanden zijn puur
hersorteerd, alle 108 versleutelde bestanden op één na ontsleuteld identiek), en de
projectbestand-migratie is verliesloos — met één verwachte wijziging die aantoonbaar
níet optreedt (aliassen worden niet versleuteld) en één onvermelde maar verliesloze
hernoeming van invite-sleutels.**

## Wat er draaide

- Cluster: `kind-rig-sandbox` (server), via `orch sandbox claim` — **dat subcommando
  bestaat inmiddels wél**, anders dan in ronde 1. Het cluster is netjes geclaimd en aan
  het eind vrijgegeven.
- NULMETING: `ghcr.io/minbzk/base-images/operations-manager/operations-manager:2026.07.27.0941-9d9c0764-dirty`
  (commit `f9e58071`, branch `main`) — de tag die de odcn-overlay vandaag pint.
- RELEASE: afgeleid image `operations-manager:rc23-release-7928d854` vanaf basisbranch-HEAD
  `7928d854`. Gebouwd als lichtgewicht afgeleide FROM `rc22-release-3d263f16`; afhankelijkheden
  zijn aantoonbaar ongewijzigd (`uv.lock`/`pyproject.toml` nul verschil) en het enige
  bronverschil is `deployment_order.py`. Dat vermijdt de bekende OOM van de Kind-control-plane
  bij een volledige build.
- Image **één keer** gewisseld voor de meting (nulmeting → release).

## Vastgelegde commitpunten

| Repo | Nulmeting | Na upgrade + migratie |
|---|---|---|
| zad-deployments | `2d6fa2db2fce360dcb42aebd6f7cc9513afe30ec` | `e2695263` |
| zad-projects | `aaa6672501096caad3eb3260483f9fe9807dde14` | `a248c83f` |

Bronbestanden: `zad-upgrade-test-projects` commit `7c10c77`.

## Per project

Alle zes gehaald, aan beide kanten. Dit is het verschil met ronde 1.

| Project | Deployments | Nulmeting | Herverwerking | Identiteit | Deploy-diff | Projectbestand |
|---|---|---|---|---|---|---|
| `mozad-dle` | 1 | PASS | PASS | identiek | geen verwijderingen | v2.6, verliesloos |
| `amt-odc` | 1 | PASS | PASS | identiek | geen verwijderingen | v2.6, verliesloos |
| `dp-bn7` | 1 | PASS | PASS | identiek | geen verwijderingen | v2.6 + invite verplaatst, verliesloos |
| `openp-4pw` | 1 | PASS | PASS | identiek | geen verwijderingen | v2.6 + invite verplaatst, verliesloos |
| `regel-k4c` | 6 | PASS | PASS | identiek | geen verwijderingen | v2.6, verliesloos |
| `wies` | 18 | PASS | PASS | identiek | geen verwijderingen | v2.6, verliesloos |
| **totaal** | **28** | | | | | |

`wies` en `regel-k4c` kwamen er deze ronde volledig doorheen. Beide reparaties uit de
opdracht werken aantoonbaar: `regel-k4c` levert zijn productiedomeinen op
(`regelrecht.rijks.app`, formaat `component.subdomain`) en `wies` verwerkt al zijn 18
deployments inclusief klonen.

## De drie mechanische controles

### 1. Identiteitstoets (`upgrade-safety-identity`) — **PASS**, het slaagcriterium

> Service identity is IDENTICAL across both sides (28 baseline / 28 upgraded deployments
> compared). Every database, realm, client, bucket and published host resolves to the
> same value.

Exit 0, over alle 28 deployments van alle zes projecten. Ontsleuteld met een keyring van
de clustersleutel plus elke projectsleutel (7 identiteiten). Geen enkele deployment wijst
na de upgrade naar een andere database, realm, client, bucket of hostname. Dit was de
vooraf afgesproken pass/fail-grens en hij is gehaald — nu voor de volledige steekproef.

### 2. Verwijderingsdiff op zad-deployments — **schoon**

132 bestanden gewijzigd, 2356 toevoegingen tegen 2356 verwijderingen. Uitgesplitst:

- **51 onversleutelde bestanden** (`kustomization.yaml`, `decrypt-sops.yaml`): mechanisch
  gecontroleerd dat de gesorteerde regelverzameling aan beide kanten gelijk is →
  **51 van 51 puur hersorteerd, 0 inhoudelijk verschillend**. De release sorteert de
  `resources:`- en `files:`-lijsten; er verdwijnt geen manifest, ingress, mount of secret.
- **108 SOPS-bestanden**: aan beide kanten ontsleuteld en volledig vergeleken.
  **107 van 108 ontsleuteld identiek**, geen enkel bestand verdwenen.
  De ene afwijking is `dp-bn7/productie/productie-website-oauth2-cookie-secret.sops.yaml`,
  sleutel `cookie-secret` — zelfde lengte, andere waarde, geen sleutel toegevoegd of weg.

Die ene is nagemeten: bij een tweede verversing op hetzelfde release-image verandert hij
opnieuw. Het is dus **bestaand niet-determinisme, geen upgrade-effect** (zie bevinding B).

Dit is een sterkere uitspraak dan ronde 1 deed: daar werd de SOPS-churn weggefilterd als
"waarschijnlijk hercodering", hier is de ontsleutelde inhoud daadwerkelijk vergeleken.

### 3. Projectbestand-diff op zad-projects — **verliesloos**

Toegestane verschillen, waargenomen:

1. **`schema-version` naar 2.6** — alle zes (`2`→`2.6` voor amt-odc, `2.2`→`2.6` voor de
   rest). ✔
2. **Invites verplaatst** van top-level `invites:` naar `services/invite/config`, voor
   `dp-bn7` en `openp-4pw`. Top-level blok is weg, nieuwe sectie aanwezig, alle waarden
   komen terug. ✔
4. **Uniform-service-normalisatie** — `- keycloak:` / `- persistent-storage:` naar de
   uniforme recordvorm, en `config.keycloak` naar de geneste keycloak-serviceconfig.
   Zoals ronde 1 al beoordeelde: verliesloos. ✔

Verwacht verschil dat **NIET optreedt**:

3. **Aliassen worden niet AGE-versleuteld.** Verwacht waren 10 blokken in `wies` en 15 in
   `openp-4pw`. Gemeten: **0 van 15 en 0 van 10, vóór én ná**, en de aliasinhoud is aan
   beide kanten **identiek**. Dit is de meting die ronde 1 niet kon doen: toen waren de
   aliassen met de hand hersteld, nu komen ze ongeschonden uit productie via de gerepareerde
   conversie. De verwachting uit het draaiboek is hiermee **weerlegd, niet onbeslist**:
   deze aliassen zijn pure `$`-verwijzingen (`$DATABASE_SERVER_HOST` enzovoort), dragen geen
   letterlijk geheim, en OPI laat ze daarom terecht plat. Het draaiboek moet op dit punt
   worden bijgesteld.

Niet in de lijst van vier, wél waargenomen en **verliesloos**:

5. **Invite-sleutels worden hernoemd van snake_case naar kebab-case** en `settings` wordt
   platgetrokken: `default_language`→`default-language` (uit `settings` omhoog),
   `realm_roles`→`realm-roles`, `application_url`→`application-url`,
   `contact_email`→`contact-email`, `success_title`→`success-title`,
   `success_button`→`success-button`. Alle waarden, inclusief de `nl`/`en`-varianten,
   komen ongewijzigd terug. Een naïeve sleutelvergelijking meldt dit als 11 "verloren"
   sleutels; dat is een meetfout, geen verlies.
6. **`totp_secret` toegevoegd** aan elke realm-entry (zie hieronder).

Geen onverklaarde verwijderingen in enig projectbestand.

## De twee open vragen uit de opdracht

**Verwijdert `delete_project_manager` de master-realm-adminuser echt volledig? — JA.**
Drie keer waargenomen deze ronde, en op **beide** images (de opruiming van ronde 1 liep op
het release-image, de tussentijdse opruimingen op het productie-image). Na `DELETE` zijn
telkens verdwenen: de projectrealm, de master-realm-adminuser
(`<project>_sandboxed_local_admin`), de namespace, en de bestanden in zad-projects én
zad-deployments. De handmatige Keycloak-opruiming die ronde 1 nodig had (bevinding F) is
niet meer nodig; die was het gevolg van het overschrijven van projectbestanden nadat OPI
er realmgegevens in had weggeschreven, niet van een tekortkoming in de verwijderroute zelf.

**Kan een project gedeeltelijk regenereren en dan afbreken? — deze ronde niet
gereproduceerd.** `regel-k4c` schreef in ronde 1 zes deployments weg voordat de zevende
faalde; deze ronde kwam hij volledig door, dus er was geen afbreekpunt. De waarneming van
ronde 1 blijft staan als reëel risico (OPI verwerkt deployments op volgorde en committeert
onderweg), maar is hier niet opnieuw aangetoond.

## Bevindingen

- **A. De verwachting "aliassen worden versleuteld" klopt niet.** Zie controle 3, punt 3.
  Nu schoon gemeten. *Actie:* draaiboek bijstellen; dit is geen bug.
- **B. `cookie-secret` van de oauth2-proxy wordt bij elke verwerking opnieuw gegenereerd.**
  Bestaand gedrag, geen upgrade-regressie (nagemeten met een tweede verversing op hetzelfde
  image). Wel een echt effect: elke reconcile verklaart alle lopende sessies achter de
  authorization-wall ongeldig, dus gebruikers worden uitgelogd zonder dat er iets wijzigde.
  *Actie:* overweeg het secret te behouden als het al bestaat.
- **C. De sandbox-node loopt vol op resource-*requests*, niet op werkelijk gebruik.** Met de
  oorspronkelijke productie-resourceprofielen vroegen de zes projecten samen ~19 Gi op een
  node van ~16 Gi (regel-k4c 7,3 Gi + wies 6,1 Gi + rig-system 4,5 Gi). Gevolg: de OPI-pod
  zelf kon niet meer worden ingepland (`Insufficient memory`, `Pending`), terwijl het
  werkelijke gebruik laag was — de containers draaien de piepkleine probe. Dat is een
  kip-eiprobleem, want verwijderen loopt via OPI. Opgelost doordat de bronbestanden nu het
  probe-resourceprofiel dragen (32Mi/10m request). *Actie:* het draaiboek moet vermelden dat
  de conversie het resourceprofiel moet vervangen, niet alleen de workload.
- **D. Deployment-strategie `RollingUpdate` verergert C.** Bij een image-wissel wil
  Kubernetes de nieuwe pod naast de oude plaatsen; op een volle node lukt dat nooit. Ik heb
  de strategie op `Recreate` gezet zodat de oude pod eerst afsluit. *Actie:* overweeg
  `Recreate` als standaard voor OPI in de sandbox-overlay.
- **E. `KEYCLOAK_ENFORCE_ADMIN_OTP` stond niet in de live configmap.** Hij staat wél in de
  sandboxed-local configmap in git, maar was er in ronde 1 uit gestript om het oude image te
  laten booten (`extra=forbid`). Mijn configmap-backup kwam uit de live staat en miste hem
  dus. Teruggezet vóór de image-wissel, samen met de `SLEEP_MODE_*`-regels. *Actie:* het
  draaiboek moet expliciet noemen dat álle regels die voor het oude image gestript zijn,
  terug moeten vóór de wissel — niet alleen `SLEEP_MODE_*`.
- **F. De OTP-countdown in het portaal klopte niet.** "Nog N seconden geldig" werd één keer
  server-side gerenderd en tikte nooit, dus hij was direct al verlopen. Op verzoek verwijderd
  (zie "Wijzigingen aan de code").
- **G. Vormgeving van het OTP-blok behoeft aandacht.** Waargenomen door de opdrachtgever;
  niet opgepakt in deze ronde. *Actie:* apart oppakken.
- **H. `sops` ontbrak op de server.** De identiteitstoets roept `sops --decrypt` aan. Zelf
  geïnstalleerd (v3.9.4). *Actie:* opnemen in de omgevingsvereisten van het draaiboek.

## Wat er zijdelings is aangetoond

**RC-16 (gedeelde OTP op realm-admins) is nu voor het eerst live gevalideerd.** Die stond te
boek als "niet op sandbox gevalideerd". Met `KEYCLOAK_ENFORCE_ADMIN_OTP=true` aan de
release-kant is de hele keten geverifieerd:

| Laag | Resultaat |
|---|---|
| Setting geladen in de pod | `ENFORCE_ADMIN_OTP = True` |
| `totp_secret` in het projectbestand | alle 5 keycloak-projecten |
| Credential in Keycloak | `['password', 'otp']` op de realm-admins |
| Portaal | "Gedeelde OTP" met werkende, roterende code |

De retrofit doet wat de docstring belooft: bestaande realm-admins worden hermaakt mét
OTP-credential, met behoud van hun wachtwoord. Let op dat dit betekent dat een upgrade mét
deze vlag aan een **extra projectbestand-verschil** oplevert (`totp_secret` per realm) dat
niet in de vier verwachte verschillen staat.

## Wat ik NIET heb kunnen doen

- **Laag 1 (offline replay over alle 47 productiebestanden) is deze ronde niet gedraaid.**
  Er staat geen checkout van de echte projectbestanden op deze server, en `RIG_PROJECTS_DIR`
  wijst nergens heen. Ronde 1 draaide dit wel (9 passed) op een machine die die checkout had.
  De uitspraak "elk van de 47 bestanden migreert en valideert" steunt dus op ronde 1, niet
  op deze.
- **De live probe (`/status`) is niet als poort gebruikt.** Net als in ronde 1 lezen de drie
  controles de git-gegenereerde staat die `refresh` (HTTP 200, "All project resources
  processed successfully") al had gecommitteerd. Of elke databinding daadwerkelijk
  rondloopt is daarmee niet aangetoond.
- **Gedeeltelijke regeneratie niet gereproduceerd** (zie hierboven).
- **De projecten zijn NIET verwijderd aan het eind** — expliciet gemeld, conform de grenzen
  van de opdracht. Ze staan er nog voor inspectie.

## Verloop van de run (waarom hij vier keer opnieuw begon)

De bronbestanden zijn tijdens de run vier keer bijgewerkt door de opdrachtgever, telkens naar
aanleiding van een bevinding. Omdat de nulmeting per definitie op de definitieve bestanden moet
staan, is hij elke keer volledig opnieuw opgezet vanaf een schone sandbox:

1. `8b82c0e` — eerste ronde-2-bestanden. Gestrand: `wies` viel om op een kloon uit een
   `staging` die niet meer bestaat.
2. `0206558` — kloonstatus en revisies behouden (`--as-existing-project`). Dit was de kern:
   de conversie maakte van een bestaand project een vers project, waardoor een `mode: once`
   kloon die in productie allang klaar was opnieuw werd geprobeerd.
3. `1d82558` — `openp-4pw` riep een shell aan die het distroless probe-image niet heeft.
4. `7c10c77` — resources op het probe-profiel, tuninghistorie eruit (bevinding C).

Een tussentijdse eigen omweg — het losse `pr-274` uit `wies` weglaten om de overige 17
deployments toch te kunnen meten — is daarmee **vervallen**; de definitieve run draait alle
18 met de echte kloonstatus. Er is dus niets aan de projectinhoud gewijzigd voor de meting.

## Wijzigingen aan de code

Deze opdracht is een uitvoeropdracht; er is één wijziging gedaan, op expliciet verzoek:

- **OTP-countdown verwijderd** (`opi/services/catalog/keycloak/otp-code.html.j2`,
  `opi/web/router.py`). De regel "Nog N seconden geldig" werd één keer server-side gerenderd
  en actualiseerde nooit. De knop "Nieuwe code" is de eerlijke manier om een verse code te
  krijgen. De nu ongebruikte `seconds_remaining` is uit de rendercontext gehaald;
  `totp_now()` blijft ongewijzigd. Test aangepast en uitgebreid
  (`tests/test_keycloak_otp_code_endpoint.py`): 27 passed.

Deze wijziging zit **niet** in het gemeten release-image `rc23-release-7928d854`; die is
gebouwd vóór de aanpassing, zodat de meting niet vervuild is.

## Eindstand van het cluster

- OPI draait op `operations-manager:rc23-release-7928d854`, met de configmap in volledige
  staat (`SLEEP_MODE_*` én `KEYCLOAK_ENFORCE_ADMIN_OTP=true` terug).
- De deployment-strategie staat op `Recreate` (bevinding D). Het geheugen-request is
  teruggezet op de oorspronkelijke 256Mi.
- **De zes projecten staan er nog** en zijn niet verwijderd. Opruimen kan met
  `scripts/sandbox_project_tool.py delete <project>`; die route ruimt aantoonbaar volledig op.
- Er staat nog een niet-gerelateerde restant-realm `invit-qel-sandboxed-local` uit een
  eerdere ronde; niet aangeraakt.
- Het sandbox-slot is vrijgegeven met `orch sandbox release`.
