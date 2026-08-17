# Services: audit en herstelplan

**Status**: Plan, nog niet uitgevoerd
**Aanleiding**: sessie 2026-07-28, waarin het bouwen van één echt project (VLAM-gateway) in een
paar uur zes losse consumenten blootlegde die hetzelfde formaat niet kenden, plus een reeks
verwante gebreken in de formulier-, opslag- en rapportagelaag.

Dit document is bedoeld om zelfstandig door te itereren. Elk werkpakket heeft een expliciet
verificatiecriterium; een pakket is pas klaar als dat criterium is gemeten, niet beredeneerd.

## Het patroon

Vrijwel alles wat die dag stukging deelt één oorzaak: **een aanname die klopt zolang alles
uniform is, en stilvalt zodra dat niet zo is.**

- Een service-entry wordt pas een record zodra hij **configuratie draagt**. Kale selecties bleven
  dus werken, en alleen geconfigureerde services braken.
- Een component zonder services, of zonder `publish-on-web`, of zonder storage, liep tegen code
  aan die aannam dat elk component die dingen heeft.
- Een deployment op het **clusterdomein** liep tegen code aan die aannam dat `base-domain` altijd
  gevuld is.

Het gevolg is telkens hetzelfde en het is het echte probleem: **het faalt stil.** De configuratie
staat er, de portal toont hem, en er gebeurt niets. Dat is erger dan een ontbrekende functie,
want het wekt vertrouwen dat er niet is. De ernstigste variant liet een rolbeperking op een VPN
volledig werkloos, terwijl alles er goed uitzag.

## Werkpakket 1: alle lezers van een services-lijst

De canonieke lezers bestaan al in `opi/services/services.py`: `service_entry_name`,
`service_entry_config` en `service_entry_type` (die laatste is 2026-07-28 toegevoegd). Ze kennen
alle drie de vormen: kale string, legacy single-key dict, en de uniforme record met `name` of
`reference`.

Zes consumenten zijn inmiddels omgezet:

| Plek | Wat er stuk was |
|---|---|
| `keycloak_manager._get_keycloak_service_config` | `restrict-access` deed niets: realm-rol nooit aangemaakt, restrictie nooit toegepast |
| `delete_project_manager` | namespace-database en -redis niet herkend, bleven achter bij verwijderen |
| `services.py project_uses_infrastructure_namespace` | zelfde detectie |
| `bootstrap_manager` | `oidc_url` en `oidc_realm` bleven leeg, matchte zelfs een kale string niet |
| `enforcers.py extract_service_names` | gaf letterlijk "reference, config" als servicenaam, opslaan geblokkeerd |
| `templates/project-details/section-keycloak.html.j2` | las de oude `project.config.keycloak` |

**Te doen**: een systematische veegactie, niet nog eens zes losse vondsten.

1. Zoek elke plek die een `services`-lijst uitleest zonder de canonieke helpers. Zoektermen die
   vandaag werkten: `in service_item`, `in service`, `.keys()` op een service-entry,
   `svc in ...`, `service_item[`, en directe indexering op een servicenaam.
   → verify: een lijst van alle vindplaatsen, met per plek "al goed" of "moet om".
2. Zet elke gevonden plek om naar de helpers.
   → verify: per plek een test die de drie vormen langsgaat.
3. Voeg een guard toe die dit niet opnieuw laat wegdrijven. Bij voorkeur een test die over de
   codebase zoekt naar het patroon, of een lint-regel. `tests/test_service_entry_formats.py`
   bevat al de basis.
   → verify: de guard faalt als je een van de zes fixes terugdraait.

Let op twee bekende resterende plekken:

- `scripts/migrate_project_to_sandbox.py` verwijdert `config.keycloak` (oude plek) en behandelt
  `comp["services"]` als een dict terwijl het in v2 een lijst is. Die lus doet dus niets.
- `scripts/migrate_project_to_production.py` (nieuw) valideert tegen het **nieuwe** schema terwijl
  het doelbestand in de oude vorm hoort te zijn. Dat faalt zodra productie zijn eigen
  `config.keycloak` heeft teruggeschreven.

## Werkpakket 2: services leveren hun eigen presentatie

Een service bezit al zijn configuratie-invoer (`config_editables`, `config_form_section`) en zijn
provisioning. Wat hij **niet** bezit is zijn presentatie op de projectdetailpagina: het
Keycloak-blok staat hardgecodeerd in `templates/project-details/section-keycloak.html.j2`, met een
`{% include %}` in de algemene template.

Daardoor drijft het weg bij elke verhuizing van de configuratie, en dat is precies wat gebeurde:
de realms verhuisden naar de service en het blok bleef naar de oude plek kijken, dus admins zagen
hun realmgegevens niet meer. De huidige oplossing (de view zet `keycloak_realms` in de context)
werkt, maar houdt de koppeling in stand.

**Te doen**:

1. Geef `Service` een hook voor detailpagina-secties, in dezelfde geest als
   `config_form_section`: de service levert de data en het sjabloon, de detailpagina rendert wat
   de geselecteerde services aanleveren.
   → verify: het keycloak-blok verdwijnt uit de algemene template en wordt geleverd door
   `opi/services/catalog/keycloak/`.
2. Inventariseer welke andere secties eigenlijk servicebezit zijn (denk aan storage, database,
   backup) en verplaats die in dezelfde beweging of noteer ze expliciet als vervolg.
   → verify: `templates/project-details/` bevat geen servicespecifieke kennis meer, of de
   uitzonderingen staan benoemd met reden.
3. Documenteer de hook in `instructions/services.md`, zodat een nieuwe service weet dat hij dit
   moet leveren.
   → verify: de instructie noemt alle hooks die een service kan implementeren.

## Werkpakket 3: formulier- en opslagintegriteit

Drie bugs uit dezelfde laag, alle drie inmiddels gefixt, maar met een gemeenschappelijk restrisico
dat nog niet is afgedekt: **transiente en virtuele velden lekken of verdwijnen stil.**

Wat er gefixt is:

- `_devirtualize` in `forms/wizard/state.py` popte de virtuele sleutel alleen op het hoogste
  niveau, dus `components[i]._services-config` bleef staan en werd door het schema geweigerd.
- Direct-aliases werden deployment-breed geresolved tegen de context van één component, dus een
  component zonder `publish-on-web` of storage kreeg een lege context en de verwerking brak af.
- `ensure_domain_requests` las een lege `base-domain` als "niets te doen" en sloeg de hele
  deployment over, waardoor elke subdomein-aanvraag op het clusterdomein stil verdween.

**Te doen**:

1. Inventariseer alle transiente en gevirtualiseerde velden (`_`-prefix, `virtualize=`,
   `transient=`) en stel per veld vast waar het wordt gezet, waar het wordt geconsumeerd en waar
   het wordt opgeruimd.
   → verify: een tabel met die drie kolommen, en geen enkele rij waarin "opgeruimd" leeg is.
2. Zorg dat opruimen een eigenschap van de datagrens is, niet een bijwerking van veldverwerking.
   `_devirtualize` doet dit nu goed; controleer of `strip_transients_from` hetzelfde doet voor
   componenten die geen bijbehorende editable hebben.
   → verify: test met een component zonder enige editable; geen enkel transient veld overleeft.
3. De bijwerking-strip in `forms/editables/processor.py` (rond regel 681) is nu overbodig maar is
   bewust blijven staan, omdat `router_detail_edit.py` rauwe submitted data in `step_data` zet.
   Loop dat pad na en verwijder de strip als hij echt dood is.
   → verify: verwijderen breekt geen enkele test, en het detail-edit-pad is aantoonbaar gedekt.

## Werkpakket 4: gelijktijdig opslaan

Het opslagpad in `core/task_handlers_project.py` maakte een verse `ProjectManager` en gaf dus geen
compare-and-swap-basis mee: last-writer-wins, zonder melding. Twee gebruikers die verschillende
velden bewerkten raakten elkaars werk kwijt. Dat is gefixt met een versietoken van render tot
save, plus de bestaande drieweg-merge in de store.

**Restrisico dat nog open staat**:

1. De modal-edit slaat het bestand **twee keer** op: eerst zelf, daarna nog eens via de
   deploy-taak. Dat is afgevangen met een vroege uitstap, maar het dubbele opslagpad zelf bestaat
   nog. Landt er een derde wijziging tussen die twee opslagen, dan kan er een onterecht conflict
   ontstaan, ná het opslaan.
   → verify: het pad slaat nog maar één keer op, of het gedrag bij een tussenkomende wijziging is
   aantoonbaar correct.
2. `_process_and_save_modal_edit` legt het formulier over vers gelezen data heen. Voor velden die
   de wizard aanraakt overschrijft de render-snapshot een verse waarde vóórdat de store er iets
   van ziet.
   → verify: twee gebruikers, één veld elk, beide wijzigingen overleven.
3. Het volle-pagina bewerkpad (`_save_existing_project`) heeft dezelfde structuur maar geen taak,
   en is niet nagelopen.
   → verify: expliciet vastgesteld of het hetzelfde lek heeft.

## Werkpakket 5: wat een component moet kunnen

Het bouwen van één realistisch project liep tegen zes grenzen aan. Elke grens is omzeild, maar de
omwegen staan nu in een projectbestand en dat is geen houdbare plek.

| Grens | Omweg die nu in gebruik is | Waarom dat niet blijvend is |
|---|---|---|
| Geen configbestand te mounten | `command`-override die het bestand schrijft en dan `exec` doet | Werkt alleen bij images met een shell; het headscale-image is een ko-build zonder shell |
| Geen extra containers per component | serve-config met `TCPForward` in plaats van een sidecar | Toevallig bruikbaar; de sidecar was de schone vorm |
| Geen RBAC per component | Kubernetes-state-opslag uitgezet, state in `/tmp` | Identiteit gaat verloren bij elke herstart, adres verschuift |
| `service.yaml.jinja` zet `port` altijd gelijk aan `targetPort` | losse handmatige Service | Buiten GitOps om, dus ArgoCD kent hem niet |
| `env-vars` (deployment-niveau, plat) is bestand-only | via git bewerken | Onzichtbaar en onbewerkbaar in de portal |
| Ingress-template zet geen `timeout-tunnel` | nog niet nodig geweest | Wordt nodig zodra langlevende streams over dezelfde route lopen |

**Te doen**: per grens beslissen of hij een productfunctie wordt of expliciet buiten scope blijft.
De eerste (`config-files` op een component) en de vijfde (`env-vars` in de portal) zijn de
goedkoopste en hebben de meeste kans om vaker nodig te zijn.
→ verify: per rij een besluit met reden, en voor wat gebouwd wordt een werkend voorbeeld dat de
omweg vervangt.

## Werkpakket 6: uitrolrapportage

Twee meldingen bleken vandaag onwaar, en samen maken ze elke uitrolmelding minder te vertrouwen.

- Een bewust uitgeschakeld component (`0/0` replicas) werd gemeld als "pods worden aangemaakt".
  Dat is de eindtoestand, geen wachtstatus. **Nog niet gefixt.**
- Een crashende pod uit een vervangen ReplicaSet werd gemeld terwijl de nieuwe generatie gezond
  draaide. Gefixt door te filteren op de `pod-template-hash` van de huidige ReplicaSet.

**Te doen**: de eerste alsnog oplossen, en breder nagaan of er meer statusmeldingen zijn die
toestand en voortgang door elkaar halen.
→ verify: een uitrol met een uitgeschakeld component meldt dat component als uitgeschakeld, niet
als bezig.

## Werkpakket 7: aannames die niet bleken te kloppen

Deze stonden als waarheid in documentatie of in ons hoofd en zijn vandaag gemeten weerlegd. Ze
horen gecorrigeerd te worden waar ze zijn opgeschreven.

1. **NetworkPolicies worden in de sandbox niet gehandhaafd.** Onjuist: een pod zonder het
   `deployment=<naam>`-label krijgt time-outs, met dat label HTTP 200. Een ontbrekende
   outbound-poort laat zich dus zien als een time-out, niet als een duidelijke fout.
2. **Productie draait oude code.** De image-tag is van gisteren, maar de fixes van vandaag staan
   er niet op. Elk project op productie met `restrict-access` verdient een controle of de
   realm-rol daadwerkelijk bestaat.
   → verify: in minstens één productie-realm gecontroleerd of de rol er is en of de restrictie op
   de client staat.

## Volgorde

1. **Werkpakket 1**, want daar zit de beveiligingsrelevante variant in en het is het breedst.
2. **Werkpakket 7 punt 2**, want dat is een controle van een uur die kan uitwijzen dat er op
   productie iets openstaat.
3. **Werkpakket 2**, want dat voorkomt dat werkpakket 1 zich herhaalt bij de volgende verhuizing.
4. De rest naar bevind van zaken.

## Wat er in de sessie al gefixt is

Ter voorkoming van dubbel werk. Alles hieronder heeft een falende test gehad vóór de fix.

- `_devirtualize` (component-niveau virtuele sleutels)
- `_scope_direct_aliases_to_component` (aliases per component in plaats van per deployment)
- `ensure_domain_requests` (lege `base-domain` betekent clusterdomein)
- Zes service-entry lezers, plus `service_entry_type` als nieuwe helper
- Compare-and-swap bij opslaan vanuit de portal (versietoken plus drieweg-merge)
- `check_pod_health` filtert op de huidige `pod-template-hash`
