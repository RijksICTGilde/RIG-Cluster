# Een aanvraag die geen domein is: de beheerpagina toont de mailaanvraag als een leeg domein

RC-143, uitgevoerd in PR #139 op `fixes-na-release-augustus`. Dit is het goedgekeurde plan
zoals het aan de implementatie is meegegeven.

---

Op `/admin/approvals` staat de goedkeuringsaanvraag van de dienst "E-mail versturen" wel in de lijst, maar hij is niet te herkennen. De pagina is nog helemaal op domeinen gebouwd: het woord "Domeinbeheer" bovenaan, "Domeinen" in het hoofdmenu, een tabel met de kolommen Type, Domein en Naam, en een badge die hardgecodeerd "Domein" of "Subdomein" zegt. Een dienstaanvraag heeft geen domein, dus die rij komt als een lege regel op het scherm. Deze taak maakt de pagina dienstneutraal en geeft haar een vorm die past bij wat er echt op staat.

## Gemeten, 20 augustus 2026, tegen de sandbox

Het project `ai1-uit` (`projects/ai1-uit.yaml` in de zad-projects-repo) heeft de dienst aangezet en de aanvraag staat er correct in:

```yaml
- name: send-email
  config:
    from-name: Robbert Uittenbroek
    messages-per-day: 100
    approval:
      status: requested
      history: []
```

`collect_approval_items()` op dat projectbestand geeft het item ook netjes terug, met het opschrift van de dienst erbij:

```json
{"type": "send-email", "domain": "", "name": "ai1-uit", "current_status": "requested",
 "status": "skip", "history": [], "service": "send-email", "label": "E-mail versturen"}
```

Het sjabloon gerenderd met precies dat item levert deze rij op:

```html
<nldd-tag text="Domein"></nldd-tag>
<nldd-cell data-lotc-component="td"></nldd-cell>   <!-- de kolom Domein, leeg -->
```

Dus: er ontbreekt geen aanvraag en er ontbreekt geen mechanisme. Wat ontbreekt is de weergave.

## Waarom hier geen nieuw mechanisme voor nodig is

De catalogusweg werkt al. `approval_services()` levert elke dienst die minstens één `ApprovalSpec` declareert, `collect_approval_items()` loopt daaroverheen en tagt elk item met `service`, `type` en `label` (`opi/services/approvals.py:50`). Een derde dienst met goedkeuring verschijnt daarmee vanzelf, zonder routewijziging. De modal in `wizard/partials/approval_items.html.j2` doet het ook al goed: die toont `item.label`.

Alleen de LIJST is blijven staan in de domeinvorm, in `opi/templates_lotc/bg/admin-approvals.html.j2:127-141`:

```jinja
<c-th>Type</c-th><c-th>Domein</c-th><c-th>Naam</c-th>
...
<c-tag label="{{ 'Subdomein' if item.type == 'subdomain' else 'Domein' }}" />
<c-td>{{ item.domain }}</c-td>
```

Dat is dezelfde fout die op de item-kant al eens is gerepareerd (het commentaar bij `collect_approval_items` noemt RC-114 en `item.label`), maar de tabel is toen niet meegegaan.

## Wat er moet gebeuren

**1. Hernoemen: dit gaat over aanvragen, niet over domeinen.**

- `opi/web/menu.py:67`: menu-item "Domeinen" wordt "Aanvragen".
- `bg/admin-approvals.html.j2`: paginatitel en `page_head()` worden "Aanvragen", en de zin "Beheer domein- en subdomeinaanvragen voor alle projecten." vervalt zonder vervanging. De kop zegt het al.
- De ongebruikte tweeling `admin/approvals.html.j2` plus `admin/approvals/_kop.html.j2` gaat mee, anders lopen de twee uit elkaar. Kijk of die tweeling nog een lezer heeft; zo niet, meld dat in de PR maar verwijder hem niet in deze taak.
- De titel van het venster ("Domeingoedkeuring - <project>", gezet in de JS onderaan hetzelfde sjabloon en nog eens in `admin/approvals/_modal.html.j2`), de flowtitel `Domein- en subdomeingoedkeuring` (`opi/forms/visualizers/flows.py:626`), de sectietitel en omschrijving (`opi/forms/visualizers/wizard_sections.py:974-983`) en de weigering "Er zijn geen domein- of subdomeinaanvragen voor dit project" (`opi/web/router_approvals.py:284`) worden alle vier dienstneutraal.

**2. Een item zegt waar het over gaat.**

`ApprovalItem` krijgt er één sleutel bij, `subject`: wat er wordt gevraagd, geschreven door de dienst die het weet. De notice-kant heeft dat veld al (zie de contractbeschrijving in `opi/services/catalog/approval.py`), de item-kant niet, en dat is precies het gat waar de lege cel doorheen valt.

- publish-on-web: `example.nl` voor een domein, `foo.example.nl` voor een subdomein (dus samengesteld, niet de twee losse velden die er nu staan).
- send-email: `Gebruik van de dienst`.

Laat `collect_approval_items()` terugvallen op de bestaande velden (`domain`, anders `name`) als een spec geen `subject` zet, zodat een modalsessie die nog van voor deze wijziging loopt niet breekt. Datzelfde is eerder met `label` gedaan.

**3. De pagina moet er ook naar uitzien.**

Dit is geen kolom hernoemen. Een domeinaanvraag en een dienstaanvraag zijn verschillende dingen en de pagina hoort dat te laten zien. Richtinggevend:

- Per project blijft er één `panel()` met de projectnaam en de knop Beheren rechts in de kop. Dat werkt en dat blijft.
- Binnen dat panel wordt er gegroepeerd per DIENST, met een kopregel die de dienst noemt met haar eigen icoon en naam uit `ServiceDefinition` (`definition.name`, `definition.icon`, en `definition.color` is er ook). Dus "Publiceren op het web" met de wereldbol, "E-mail versturen" met de envelop. Die gegevens staan er al; haal ze op via de registry en niet via een lijstje in het sjabloon.
- Binnen een groep één regel per aanvraag: de `subject` als tekst, de status als `c-tag` (aangevraagd warning, goedgekeurd success, afgewezen error, zoals nu), en de laatste wijziging met datum en wie.
- De soort-tag ("Domein", "Subdomein") staat vóór de subject, maar alleen als hij iets toevoegt: bij publish-on-web onderscheidt hij domein van subdomein, bij een dienst met één soort herhaalt hij alleen de groepskop. De regel daarvoor is generiek te schrijven: toon `item.label` alleen als hij verschilt van de naam van de dienst.

De vorm van de regels mag een `c-table` blijven of iets anders worden; kies wat er goed uitziet met twee groepen onder elkaar. Raadpleeg de componentreferentie VOORDAT je markup schrijft, en let op de vallen die hier al eens geld hebben gekost: `c-table` heeft `columns` nodig anders stapelen de cellen, `c-card` vereist `background`, twee blokken zonder stack ertussen raken elkaar, en een `c-stack` IN een tabelcel breekt de kolombreedte in Firefox (dat staat uitgeschreven in het sjabloon zelf, regel 152 en verder; laat die waarschuwing staan).

**4. De booleaanse vorm hoort één keer te bestaan.**

"Ja, dit project mag deze dienst gebruiken" komt vaker terug dan alleen bij mail. Nu is die vorm met de hand geschreven in `opi/services/catalog/send_email/__init__.py:78-185`: het lezen van de status, het item, het vastleggen van het oordeel, de mededeling aan de aanvrager en `ensure_approval_requests`, samen ongeveer negentig regels. De tweede dienst die dit nodig heeft, kopieert dat.

Maak er één declaratie van in `opi/services/catalog/approval.py` (werknaam `service_use_approval()`, dat is een voorstel en geen vastgelegde naam), die de hele vorm levert voor state op `services/[dienst]/config/approval` met `status` en `history`. send-email is de eerste en voorlopig enige gebruiker en moet er daarna kaal uitzien: de dienst zegt dát ze goedkeuring vereist plus de zin die de aanvrager te lezen krijgt, en verder niets.

Twee dingen die daarbij niet mogen sneuvelen, want ze zijn de reden dat de mailkant zorgvuldig is opgeschreven:

- `is_approved()` blijft de enige poort. Alles wat de dienst doet (account, netwerkbeleid, envFrom-secret, secretbestand) hangt eraan, zodat die vier het nooit oneens kunnen zijn.
- De mededeling per deployment blijft bestaan, inclusief het gevolg ("geen SMTP-account, geen netwerktoegang naar de relay, geen SMTP_-variabelen"). Een dienst die aanstaat en stil niets doet is precies de storing die hier is uitgeroeid.

## Valkuilen

**Er werkt een andere sessie in dezelfde hoek.** Op het moment van schrijven staan `opi/forms/visualizers/providers.py`, `opi/services/help_text.py`, `opi/services/catalog/send_email/editables.py` en `widgets/service_cards.html.j2` niet-gecommit in de werkmap: dat is de wizardkant van send-email (onder andere een pil "Vereist goedkeuring door een beheerder" op de dienstkaart, gevoed door `approval_specs()`). Die kant en deze kant raken elkaar niet, maar hergebruik wat daar ontstaat in plaats van er een tweede waarheid naast te zetten, en blijf van die bestanden af.

**Het statusfilter telt items, niet projecten.** `filter_op_status()` gooit een project weg zodra er van dat project niets overblijft, en `approvals_totaal` telt de ongefilterde items voor de zin "x van y aanvragen". Met een groepering per dienst erbij moeten die twee getallen nog steeds over ITEMS gaan en niet over groepen, anders klopt de teller niet meer met de lijst. `tests/test_approvals_statusfilter.py` bewaakt de kop van dat filter; breid het uit in plaats van het te omzeilen.

**Het venster leest de items uit de wizardstate, niet uit het sjabloon.** De verborgen velden in `approval_items.html.j2` dragen `service`, `type`, `label`, `domain`, `name` en `current_status` terug naar de POST. Als `subject` alleen voor de weergave is, hoeft hij niet mee; zorg dan wel dat `apply_approval_verdicts` niet op een veld gaat leunen dat niet terugkomt. De routering gebeurt op `service` plus `type`, en dat moet zo blijven.

**Namen die niet meer kloppen.** De browsertest heet `tests/e2e/test_lotc_domeinbeheer.py` en er wijzen docstrings in minstens vier unittests naar. Hernoemen mag en is netter, maar dan alle verwijzingen mee, in één commit.

## Wat hier buiten valt

- Nieuwe diensten met goedkeuring aanzetten. Deze taak maakt de weg vrij en zet er niets nieuws op.
- De wizardkant van send-email (het aantal berichten per dag, de dienstkaart). Dat loopt elders.
- De goedkeuringen zelf verlenen op de sandbox. De pagina moet kloppen; wie wat goedkeurt is een aparte beslissing.

## Verifieerbaar

- Op `/admin/approvals` staat voor `ai1-uit` een herkenbare regel voor E-mail versturen, met de envelop, de dienstnaam en de status Aangevraagd, en zonder lege cel. Zet er een screenshot bij in de PR, samen met een screenshot van een project met domeinaanvragen zodat het verschil zichtbaar is.
- Het hoofdmenu zegt "Aanvragen" en de pagina heeft geen introzin meer.
- Een gerichte test rendert het sjabloon met één domeinitem en één dienstitem en legt vast dat er nergens meer een hardgecodeerd "Domein" op een dienstaanvraag valt.
- `collect_approval_items()` op `ai1-uit` geeft voor elk item een gevulde `label` en `subject`.
- Het statusfilter blijft kloppen: `?status=requested` toont de mailaanvraag, `?status=approved` toont hem niet, en de teller "x van y" telt items.
- Goedkeuren via het venster schrijft nog steeds naar `services/[send-email]/config/approval` met een history-regel, en de bestaande domeingoedkeuring is onveranderd.
- `uv run pytest tests/ -q` groen, plus `ruff check .`, `ruff format .` en `pyright`.
