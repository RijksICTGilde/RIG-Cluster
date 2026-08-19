# Een helmfile-project via de wizard: wat er nodig is, en waar de scheiding hoort

**Status**: voorstel, 18 augustus 2026 (RC-130). Geen implementatie behalve twee reparaties die
onderweg boven water kwamen; die staan onderaan. Dit stuk beantwoordt de zeven vragen uit de
opdracht en is bedoeld om te lezen zonder eerst `mb-docs-helmfile.yaml` te ontcijferen.

## Waar dit over gaat

Twee projecten op het platform worden niet uit componenten opgebouwd maar uit een **helmfile**:
`mb-docs-helmfile` en `mb-grist-helmfile`. Ze zijn met de hand geschreven. Het portaal kan ze
tonen, maar niet maken en niet bewerken: `opi/web/router_wizard.py` en de hele `opi/forms/`-laag
noemen `helmfile` geen enkele keer.

De vorm loopt parallel aan componenten. Op projectniveau staat een **catalogus** (`helmfile:`,
met per item `name`, `url`, `ref`, `path`, `files`, `helm-values` en een eigen `services`-lijst);
op deploymentniveau een **verwijzing** (`helmfile:`, met `reference`, `env-vars` en
`helm-values`). Het schema kent beide al: `helmfile-entry` en `deployment-helmfile` in
`opi/schemas/project_v2.json`.

---

## 1. Gooit een bewerking via het portaal het helmfile-blok weg?

**Nee.** Gemeten, niet beredeneerd, op een kopie.

De meting draait de echte opslagweg (`apply_modal_edit`, `opi/forms/wizard/save.py`) op een
project dat een helmfile-catalogus en een helmfile-verwijzing draagt, voor **alle veertien**
bewerkstromen die de detailpagina aanbiedt, en vergelijkt de gedumpte YAML byte-voor-byte. Het
resultaat staat als test in `tests/forms/test_helmfile_blijft_behouden.py`: de catalogus, de
verwijzing, het versleutelde `helm-values`-blok, de `.gotmpl`-bestanden en de dienstenlijst
binnen het item komen allemaal ongeschonden terug.

Het is geen toeval, en het zijn **twee onafhankelijke mechanismen**:

1. **De schrijfverzameling** (RC-26, `opi/forms/wizard/write_set.py`): een stroom schrijft
   alleen de `yaml_path`s die haar eigen editables noemen. Een blok dat geen enkele editable
   kent wordt niet aangeraakt.
2. **`state.base_data`**: alles wat de stroom niet volledig bezit
   (`_fully_owned_list_keys`) wordt bij het openen apart bewaard en bij het opslaan
   teruggelegd.

De negatieve controle onderbouwt dat: elk van de twee afzonderlijk uitzetten laat de helmfile
staan, en pas met **beide** uit verdwijnt hij. De bescherming is dus dubbel, en dat is precies
waarom er een test op hoort te staan: wie straks een wizardstap bouwt die `deployments` of
`helmfile` als "eigen lijst" opgeeft, haalt allebei de lagen in één keer weg.

**Er is dus geen spoedreparatie nodig.** Wat er wel uit de meting kwam, staat onder
"Wat er onderweg gerepareerd is" en "Wat er nog los ligt".

---

## 2. `helm-charts` en `helmfile`: wat is het verschil, en moeten ze allebei?

Twee dingen die allebei "helm" heten, met elk een eigen `$def`, en ze zijn niet uitwisselbaar.

| | `helm-charts` | `helmfile` |
|---|---|---|
| Schema (`project_v2.json`) | `helm-chart` (r510), `deployment-helm-chart` (r467) | `helmfile-entry` (r528), `deployment-helmfile` (r478) |
| Bronvelden | `source-type`, `git-url`, `git-ref`, `chart-path` | `url`, `ref`, `path`, **`files`** |
| Wat het is | EEN helm-chart, uit git gekloond | een helmfile-boom: meerdere charts, met eigen instapbestand |
| Hoe het gerenderd wordt | Kustomize `helmCharts:` (`opi/generation/manifests.py:505`) | ArgoCD-CMP draait `helmfile template` |
| Waar de waarden landen | `values.yaml` | `values.sops.yaml`, door de CMP ontsleuteld |
| Bouwweg | `_process_helm_chart_deployment` (`project_manager.py:4099`) | `_process_helmfile_deployment` (`project_manager.py:4456`) |

De keuze valt in `_process_deployment` (`project_manager.py:3715`): helmfile wint, dan
helm-charts, dan componenten. Ze zijn dus alternatieven binnen één deployment, niet lagen.

**Moeten ze allebei via de UI?** Nee, en dat is de helft van het werk. De vraag "wat is er in
gebruik" is met dit werkboek niet sluitend te beantwoorden - de projectbestanden staan in de
`zad-projects`-repository en niet hier - maar wat er wel te zien is wijst één kant op:
`helm-charts` heeft geen enkel bekend project achter zich, terwijl `helmfile` er twee heeft en
een eigen feature-document (`features/helmfile-single-app-deployment.md`) met de motivering
waarom het zo gebouwd is. **Toets dit één keer tegen de echte repository voordat er iets
gebouwd wordt** (tel `helm-charts:` en `helmfile:` over alle projectbestanden); is het
resultaat nul om twee, dan is `helm-charts` een variant om te tonen en niet te bewerken, en
scheelt dat de helft van elk scherm hieronder.

---

## 3. Eigen scherm, eigen dienst, of iets anders?

**Een eigen wizardstap plus een eigen bewerkscherm. Geen dienst.**

`instructions/services.md` is er duidelijk over wat een dienst is: een gebruikersgericht
bouwblok dat een project **aanzet** in zijn projectbestand, met een `ServiceType`, een
`ServiceDefinition`, een `binding` (per component of per deployment) en `config_layers()` die
zeggen waar zijn instellingen wonen. Elk van die drie past slecht:

- **`binding` heeft geen antwoord.** Helmfile bindt niet aan een component en wordt ook niet
  "aangezet" op een deployment - hij VULT de deployment, in plaats van componenten. Dat is
  dezelfde plek in het bestand als `components`, en `components` is geen dienst.
- **De dienstenkaart klopt niet.** Een dienst verschijnt als aanvinkbare kaart in de
  dienstenstap. "Helmfile" aanvinken naast Keycloak en PostgreSQL suggereert dat het iets is
  wat je erbij krijgt, terwijl het een keuze is over hoe het hele project in elkaar zit.
- **Helmfile draagt zelf diensten.** Het catalogusitem heeft een eigen `services`-lijst (zie
  vraag 6). Een dienst die diensten bevat is een laagfout.

Dezelfde spanning staat al beschreven in `features/futures/tabbladen-via-een-haak.md`: niet
alles wat uitbreidbaar moet zijn is een dienst. Het dienstensysteem is de goede haak voor
"wat kan een project erbij krijgen", en de verkeerde voor "waar bestaat een project uit".

**Wat het dan wel is**: een derde soort inhoud naast componenten en helm-charts, met in de
wizard een keuze VOORAAN - "waar bestaat dit project uit: componenten of een helmfile" - die
bepaalt welke stappen er daarna komen. De wizard kan dat al: `resolve_active_sections` kiest de
actieve stappen op grond van de ingevoerde gegevens, dus de componentenstap kan wegvallen zoals
een dienstconfiguratiestap dat nu doet wanneer de dienst niet is aangevinkt. Er komt geen
tweede wizard bij; er komt een tak bij in dezelfde.

Op de detailpagina is het spiegelbeeld al aanwezig: het blok Helmfile staat er, met dezelfde
`panel()`-vorm als Componenten. Daar hoeft alleen een bewerkknop naast, die een modale stroom
opent zoals `modal-edit-components` dat doet.

---

## 4. `helm-values` en `files`: geen formulier

Dit is de reden dat het een eigen scherm is en niet een paar velden erbij.

**`helm-values` is een willekeurige geneste boom.** In `mb-docs-helmfile` staan er tientallen
regels in: per applicatie aan/uit, autoscaling per onderdeel, pdb, securityContext,
OpenShift-compatibiliteit. Het schema geeft het veld geen type (`"helm-values": {}`) omdat de
vorm van de bron komt, niet van ons. Daar valt geen formulier voor te maken en dat moet je ook
niet willen: elk veld dat je erin modelleert is een veld dat verouderd raakt zodra de
upstream-chart verandert.

**`files` bevat hele bestanden, inclusief Go-templates.** Het item draagt een
`helmfile.yaml.gotmpl` met `{{ toYaml .Values | nindent 8 }}` erin. Dat is code.

**Het eerlijke antwoord is een YAML-editor met validatie**, en het is belangrijk om te zeggen
wat dat betekent: **de wizard bestaat juist om YAML te vermijden, en voor deze twee velden
lukt dat niet.** Doe dan niet alsof. Voorstel:

- een tekstvlak met YAML-syntaxcontrole bij het opslaan, en verder geen pretentie;
- een expliciete waarschuwing in de stap: dit deel is Helm-configuratie en de betekenis komt
  van de chart, niet van dit portaal, met een verwijzing naar de bron
  (`url` + `ref` + `path`);
- **`files` in de eerste ronde niet bewerkbaar.** Een `.gotmpl` bewerken in een tekstvlak in
  een browser is code bewerken zonder gereedschap. Toon de bestandsnamen (dat doet de pagina
  sinds deze taak), en laat de inhoud waar hij hoort. Wie dat wil bewerken, doet dat via de
  API of via het projectbestand.

Dat is ook de natuurlijke knip voor de omvang: `helm-values` bewerkbaar is een schermpje,
`files` bewerkbaar is een editor-project.

---

## 5. De versleutelde deployment-values bij bewerken en opslaan

De verwijzing op deploymentniveau draagt in `mb-docs-helmfile` een AGE-versleuteld
`helm-values`-blok; de catalogus op projectniveau draagt platte YAML. **Het schema laat beide
vormen op beide plekken toe**, en de bouwweg leest ze ook allebei
(`_decrypt_with_private_key` ontsleutelt alleen strings en laat een boom staan). Een
bewerkscherm mag dus niet aannemen "project = plat, deployment = versleuteld", maar moet naar
de waarde kijken.

Dit is precies waar eerder een realm-wachtwoord bij het opslaan verdween (RC-79). De
werkwijze die daaruit volgde is er al en moet hier gebruikt worden:

- **Redactie bij het openen.** `opi/forms/wizard/secrets.py` vervangt onbereikbare geheimen in
  de sessie door `REDACTED`; de wizardsessie gaat naar schijf, dus er hoort geen leesbare
  `helm-values` in.
- **Terugleggen bij het opslaan.** Komt er `REDACTED` terug, dan blijft het oude blok staan.
  Alleen een echt gewijzigde waarde wordt opnieuw versleuteld.
- **Opnieuw versleutelen met de projectsleutel**, met dezelfde blokvorm als er stond -
  `tests/forms/test_flow_write_isolation.py` vergelijkt byte-voor-byte en vangt precies het
  geval waarin een blok zijn vorm verliest.
- **De laag is een eigenschap van het scherm, niet van de gebruiker.** Het scherm dat de
  catalogus bewerkt en het scherm dat de verwijzing bewerkt zijn twee stromen met twee
  `yaml_path`s. Ze mogen niet één scherm zijn dat "raadt" welke van de twee bedoeld is.

---

## 6. De `services`-lijst binnen een helmfile-item

Het catalogusitem draagt zijn eigen `services`: in `mb-docs-helmfile` zijn dat
`publish-on-web`, `keycloak`, `namespace-postgresql-database`, `minio-storage` en `redis`.

**Het is dezelfde vorm en dezelfde betekenis als de `services` van een component**, en het
wordt ook zo gelezen: `ServiceAdapter.extract_service_names_from_project_services` gevolgd door
`parse_services_from_strings`, hetzelfde paar dat op componenten wordt losgelaten
(`keycloak_manager.py:853`). Het antwoord op "gebruikt deze deployment Keycloak" loopt bij een
helmfile-deployment langs `_deployment_uses_keycloak_via_helmfile`, dat de verwijzing volgt
naar het catalogusitem en dáár de dienstenlijst leest.

De les daaruit voor de wizard: **de dienstenstap kan hierop aansluiten, maar de plek verschilt.**
Bij componenten kiest de gebruiker per component welke diensten dat component gebruikt; bij een
helmfile is het catalogusitem de eenheid die diensten gebruikt. Het is dus dezelfde
dienstenkiezer op een andere houder - niet een nieuw soort ding, en niet zomaar de bestaande
component-kiezer.

Let op één asymmetrie: de projectbrede `services:`-lijst blijft leidend voor wat er
GEPROVISIONEERD wordt. De lijst in het helmfile-item zegt welke daarvan aan deze helmfile
worden doorgegeven (als secret, als env-var). Een dienst die in het item staat maar niet op het
project bestaat, bestaat niet - dat hoort de validatie te zeggen en dat is er vandaag niet.

---

## 7. Omvang, in stukken die los te bouwen zijn

Van klein en zeker naar groot en onzeker. Elk stuk is op zichzelf waardevol.

**A. Bekijken afmaken - klein, klaar in deze PR.**
De pagina las veldnamen die het schema verbiedt en toonde daardoor niets van een echt
helmfile-item; en een `helm-values` in platte tekst werd stil weggelaten. Beide gerepareerd,
zie onder.

**B. Bewerken zonder weggooien vastzetten - klein, klaar in deze PR.**
De test die vraag 1 vastpint. Dit is het vangnet dat de rest van deze lijst mag bouwen.

**C. `helm-values` bewerkbaar - een tot twee dagen.**
Eén modale stroom per laag (catalogus en verwijzing), een YAML-tekstvlak met syntaxcontrole, en
de geheimenweg uit vraag 5. Geen wizardwijziging nodig: de detailpagina krijgt er twee
bewerkknoppen bij. Dit is de kleinste stap die echt iets oplost, want dit is wat er in de
praktijk aan zo'n project verandert.

**D. Een helmfile toevoegen aan een bestaand project - drie tot vijf dagen.**
Catalogusitem aanmaken (`name`, `url`, `ref`, `path`, diensten), en er een verwijzing bij op een
deployment. Hier moet de validatie uit vraag 6 komen, plus de controle dat een deployment niet
tegelijk componenten en een helmfile draagt (`_process_deployment` kiest er stil één).

**E. Aanmaken via de wizard - een tot twee weken.**
De keuze vooraan, een tak die de componentenstap overslaat, de dienstenstap op het
catalogusitem, en de eerste deployment eromheen. Dit is het grootste stuk, en het is ook het
stuk waarvan de opbrengst het minst zeker is: er zijn twee zulke projecten, en ze werden
gemaakt door iemand die het projectbestand kende. Doe C en D eerst en meet dan of E nog nodig
is.

**F. `files` bewerkbaar - open einde.** Zie vraag 4. Niet doen zonder aanleiding.

---

## Wat er onderweg gerepareerd is

Twee dingen die niet in het plan stonden maar aantoonbaar kapot waren, allebei in de weergave -
de kant waarvan gezegd werd dat hij al werkte.

**De veldnamen op de projectpagina bestonden niet.** `project-tabs.html.j2` las
`helmfile.repository` en `helmfile.branch`, en voor helm-charts `chart` en `version`. Het schema
kent die namen niet en verbiedt ze met `additionalProperties: false`; de echte velden zijn
`url`/`ref` en `chart-path`/`git-ref`. Voor een echt projectbestand toonde het blok dus alleen
een naam en een pad, zonder te zeggen waar de helmfile vandaan komt. Nu de schemanamen, plus
de bestandsnamen uit `files` en de dienstenlijst van het item. De e2e-fixture
`tests/e2e/fixtures/projects/test-project-detail.yaml` droeg dezelfde verzonnen namen en is
meeverbeterd, zodat de fixture voortaan een geldig projectbestand is.

**Een `helm-values` in platte tekst werd stil weggelaten.** De weergaveweg riep
`decrypt_age_content` onvoorwaardelijk aan. Een boom (de vorm die `mb-docs-helmfile` op
projectniveau heeft) viel dan in de `except`, werd `None`, en omdat het sjabloon het blok alleen
toont `if ... is mapping` verdween het zonder melding van het scherm. De vier gelijke
kopieën van dat blok zijn nu één `ontsleutel_helm_values` die kijkt wat er staat: een AGE-blok
gaat door de ontsleuteling, een string zonder AGE-koptekst wordt als YAML gelezen, een boom
blijft staan. Dat is dezelfde regel die de bouwweg al hanteerde.

## Wat er nog los ligt

Drie waarnemingen die geen reparatie in deze PR kregen, omdat ze een beslissing vragen.

**`helmfile-entry.entry` staat in de code en niet in het schema.**
`_clone_helmfile_source` leest `helmfile_def.get("entry", "")` (`project_manager.py:4401`), maar
`helmfile-entry` heeft `additionalProperties: false` en kent `entry` niet. Een projectbestand dat
het veld gebruikt wordt bij het opslaan geweigerd, dus die tak is onbereikbaar. Of het veld hoort
in het schema, of het hoort uit de code - dat is een keuze van de eigenaar van de helmfile-bouwweg.

**De volledige wizardbewerking (`/forms/wizard/edit-project/edit/{project}`) werkt niet.**
De route bestaat en is niet vanaf enige pagina te bereiken. Een opslag via die weg loopt vast op
`apply_form_data_to_project`, dat `clusters` als onveranderlijk veld weigert terwijl de stroom
het wel indient - een 400 voor elke opslag, ongeacht helmfile. Als de meting daaraan voorbij
wordt geholpen, schrijft die weg bovendien `components: []` en een standaard
publish-on-web-configuratie in een project dat die niet had. Het helmfile-blok raakt hij niet
kwijt. Repareren betekent kiezen tussen "de route weghalen" en "clusters bewerkbaar maken"
(`features/futures/cluster-editing.md`), en dat is geen bijvangst van deze taak.

**Een deployment mag vandaag tegelijk componenten en een helmfile dragen.** Het schema staat het
toe en `_process_deployment` kiest er stil één (helmfile wint). Zolang niemand het via een
formulier kan aanmaken is dat theorie; vanaf stap D is het een validatie die er moet zijn.

## Waar het vandaan komt

De opdrachtgever vroeg om een vriendelijke scheiding - een eigen helmfile-scherm, of misschien
een eigen dienst omdat er veel bij komt kijken. Het antwoord op die tweede helft is: geen dienst,
en de reden staat in vraag 3.

Zie ook `features/helmfile-single-app-deployment.md` (hoe de bestaande projecten in elkaar
zitten), `instructions/services.md` (wat een dienst mag en moet) en
`features/futures/tabbladen-via-een-haak.md` (dezelfde afweging, ander onderwerp).
