# Eén bevestiging op de projectpagina

Alles wat op de projectdetailpagina eerst vraagt -- verwijderen, herverwerken, slapen,
wekken -- gebruikt dezelfde dialoog, hetzelfde fragment en dezelfde regels. De pagina
zegt nooit waar iets naartoe post; ze noemt een actie bij naam, en de server leidt het
endpoint af uit het project zelf.

## Waarom

Er waren twee dialogen. De ene, voor dienst-acties, opende de gedeelde modal op een
fragment en kreeg het taak-voortgangsfragment terug. De andere, `showDangerConfirmation`
in `project-details.html.j2`, deed alles zelf: een hardgecodeerde titelmap, per
actionType kiezen welke van twee knoppen zichtbaar was, een eigen voortgangsblok, en een
eigen `fetch()` per soort verwijdering. Elke nieuwe bevestiging moest in die map.

Die tweede dialoog was ook weg te klikken terwijl de actie liep: Escape en klikken-buiten
werden alleen geremd door `window.isEditSubmitting`, en die vlag werd op dat pad nooit
gezet. Je zag een venster zonder knoppen, drukte Escape, en wist niet of het project nu
verwijderd werd of niet.

Opheffen in de generieke dialoog laat dat probleem per constructie verdwijnen: het
voortgangsfragment draagt de klasse die het wegklikken blokkeert, dus er valt niets meer
te vergeten bij een volgende actie.

## Hoe een bevestiging werkt

1. De knop op de pagina roept `openServiceModal(url, titel)` aan met een **actiesleutel**,
   nooit met een endpoint:

   ```jinja
   @click="openServiceModal('/projects/{{ project.name }}/actions/delete-component/confirm?target={{ component.name | urlencode }}', 'Component verwijderen')"
   ```

2. `GET /projects/{project}/actions/{action_key}/confirm` bouwt de actie server-side
   (`opi/web/project_actions.py`) en rendert `project-details/action-confirm.html.j2`.
3. De bevestigingsknop post via htmx naar het endpoint dat daar is ingevuld.
4. Het endpoint maakt een taak aan en antwoordt met het gedeelde voortgangsfragment, dat
   htmx in de dialoog swapt. De vraag wordt de lopende taak.

Dienst-acties (`DeploymentAction`, zie [sleep-mode](sleep-mode.md)) lopen langs
`GET /projects/{project}/deployments/{deployment}/actions/{key}/confirm` en renderen
hetzelfde fragment; alleen de herkomst van de actie verschilt.

## De acties

| Sleutel | `target` | Post naar |
|---|---|---|
| `refresh-project` | - | `/projects/{p}/refresh` |
| `delete-project` | - | `/projects/delete/{p}` |
| `refresh-deployment` | deployment | `/projects/{p}/refresh/{d}` |
| `delete-deployment` | deployment | `/projects/{p}/delete-deployment/{d}` |
| `delete-component` | component | `/projects/{p}/delete-component/{c}` |
| `delete-attachment` | bijlage-id | `/projects/{p}/attachments/{id}/delete` |

`target` wordt getoetst aan het project zelf: een deployment, component of bijlage die
dit project niet heeft levert geen actie op (404), en de projectbrede acties accepteren
helemaal geen `target`. De waarde gaat url-encoded het endpoint in, zodat een naam nooit
zelf padsegmenten kan toevoegen.

Een bijlage die nog gekoppeld is, is `blocked_reason`: de dialoog legt uit waarom het niet
kan en toont geen knop. De taak weigert het daarna alsnog zelf -- de dialoog is de
vriendelijke helft, niet de rem.

Bij een component ligt het net anders, omdat vrijwel elk component in een deployment zit.
De dialoog **noemt** de deployments en componenten die eraan verwijzen en zegt dat die
verwijzingen meegaan; de POST draagt daarom de bevestiging (`confirm_in_use`). Alleen een
component waar het webadres van een deployment omheen gebouwd is (`root-component`,
`expose-component-on-bare-domain`) krijgt een `blocked_reason` en geen knop -- dat weigert
de delete-guard ook mét bevestiging. Zie
[component-verwijderen-api](component-verwijderen-api.md).

## Waarom een sleutel en geen endpoint

Een parameter waar een URL in past is een open POST-doel. Dit pad verwijdert projecten,
dus het endpoint mag alleen ontstaan uit wat de server zelf over het project weet. Dat is
dezelfde eigenschap die de dienst-acties al hadden (`deployment_action_key`), en de reden
dat verwijderen een tweede, even smalle ingang kreeg in plaats van een generieke.

## Verwijderen is een taak

Alle vier de verwijderingen draaien als async taak, net als herverwerken:

| Taaktype | Handler |
|---|---|
| `delete_project` | `opi/core/task_handlers_project.py` |
| `delete_deployment` | `opi/core/task_handlers_deployment.py` (bestond al) |
| `delete_component` | `opi/core/task_handlers_components.py` |
| `delete_attachment` | `opi/services/catalog/attachments/task.py` |

Een project verwijderen sloopt git, ArgoCD, de namespace, databases en buckets; inline
liet dat de browser minutenlang op een open POST staan zonder iets te tonen. Component
verwijderen deed het bovendien half: het projectbestand werd in het verzoek aangepast en
het herverwerken erna was al een taak, dus de dialoog meldde succes voordat het werk
begon. Nu zit alles in één taak die je kunt volgen.

Waar "Ok" je heen brengt hangt van het taaktype af
(`ON_COMPLETE_BY_TASK_TYPE` in `opi/web/task_progress.py`): terug naar `/projects` als het
project zelf weg is, naar de pagina zonder hash als de deployment weg is, en anders
gewoon herladen.

## Niet weg te klikken tijdens het lopen

Het voortgangsfragment (`partials/task_progress_fragment.html.j2`) draagt zolang de taak
loopt de klasse `edit-progress-view`. De afrondknop draagt `edit-progress-actions`.

Wat die twee klassen betekenen staat op één plek: `static/js/edit_modal.js`, naast de
blokkade zelf. Na elke htmx-swap leest dat bestand de open modal terug en zet
`window.isEditSubmitting` op "er staat een voortgangsweergave in en er zijn nog geen
afrondknoppen". Zolang die vlag aanstaat doen zowel Escape als
`handleEditBackdropClick()` niets.

Twee dingen zijn hierin bewust zo:

- **Het is een eigenschap van de gedeelde modal, niet van een pagina.** De regel stond tot
  RC-53 inline in `project-details.html.j2`, waar hij alleen daar gold; elke andere pagina
  die dezelfde modal opent zag er identiek uit en was toch onbeschermd. Nu geldt hij overal
  waar `.edit-section-modal` staat -- vandaag ook de domeingoedkeuringen in `admin/approvals`.
- **De staat wordt uit de DOM teruggelezen, niet uit het geswapte element.** Een
  voortgangsfragment vervangt zichzelf bij elke poll (`hx-swap="outerHTML"`), en htmx geeft
  bij zo'n swap het oude, al losgekoppelde element mee. Kijken naar dat element ziet de
  afronding dus juist *niet* op de poll die de modal moest vrijgeven.
- **Beide klassen tellen mee.** Het taakfragment laat `edit-progress-view` vallen als het
  klaar is, maar de modal-wizard houdt zijn voortgangsweergave als vaste omhulling en
  verwisselt alleen de binnenkant. Daar zijn de afrondknoppen het enige signaal dat het
  klaar is.

Dat geldt voor elke actie die dit fragment gebruikt, ook toekomstige: er is geen vlag per
actie of per pagina meer om te vergeten. Vastgelegd in
`tests/e2e/test_shared_modal_blockade.py`, dat de blokkade juist *niet* vanaf de
projectdetailpagina aanstuurt.

## Het fragment wordt één keer gerenderd

`render_progress_fragment()` (`opi/web/task_progress.py`) rendert het sjabloon en geeft
dat terug -- zonder het daarna nog door het filter `process_components` te halen. Dat
filter is bedoeld voor HTML die tijdens het verzoek is opgebouwd (formuliervelden); op
een sjabloonbestand is het overbodig, want de componentextensie heeft `<c-...>` al bij
het compileren vervangen. Het is bovendien gevaarlijk: het filter parseert de al
gerenderde HTML nóg een keer als Jinja-sjabloon. Autoescape escapet `< > & " '` maar niet
`{{`, dus een stapnaam of subtaaknaam met `{{ ... }}` erin zou dan worden uitgevoerd in
plaats van getoond -- code-uitvoering in de OPI-pod. Zet het filter hier dus niet terug.

Om dezelfde reden controleert elk endpoint zijn doel voordat het de taak aanmaakt:
`_require_deployment` / `_require_component` in `opi/web/router.py` en de catalogus-check
in `opi/web/router_attachments.py`. Een naam die het project niet heeft is een 404 bij de
klik, en komt zo nooit in de tekst van een taak terecht.

Het voortgangsfragment is verder geschermd op het project waar het onder hangt: de taak
moet van dat project zijn, en de gebruiker moet er toegang toe hebben of degene zijn die
de taak startte (`created_by`). Dat laatste is nodig omdat het project bij verwijderen al
weg is voordat de taak klaar meldt.

## Een nieuwe bevestiging toevoegen

- Hoort de actie bij een dienst? Geef die een `DeploymentAction` met `confirm_message` --
  zie `instructions/services.md`. Hier hoeft niets bij.
- Hoort de actie bij het project zelf? Voeg een sleutel toe in
  `opi/web/project_actions.py` (bericht, knop, endpoint, toetsing van `target`) en roep
  vanaf de pagina `openServiceModal(...)` aan met die sleutel. Het endpoint moet
  antwoorden met `create_task_and_render_progress(...)`.

Let op: zet `hx-*`-attributen met JSON (zoals `hx-headers`) op een omhullende `div`, niet
op een `<c-button>`. ROOS zet attribuutwaarden opnieuw uit in dubbele quotes en breekt de
JSON; htmx erft ze van een ancestor.

## Tests

- `tests/test_project_actions.py` -- welke sleutel welk endpoint oplevert, en dat een
  vreemd of ontbrekend `target` niets oplevert.
- `tests/e2e/test_detail_confirmations.py` -- per actie: opent de dialoog, post naar
  precies dat endpoint, en een lopende actie is niet weg te klikken.
