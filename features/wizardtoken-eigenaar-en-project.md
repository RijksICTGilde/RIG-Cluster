# Een wizardtoken hoort bij één gebruiker en één project

## Wat het is

De modal-wizard bewaart zijn state op schijf onder een `_wizard_token` dat door de
HTMX-keten meereist. Die state is nu gebonden aan de gebruiker die hem begon en aan het
project waarvoor hij is aangemaakt. Dezelfde voortgangsroute van de modal toetst, net als
zijn zusterroutes, dat de taak van het project in het pad is en van deze gebruiker.

Niets verandert aan wie wat mag. Wat verandert is dat een gedeeld, gekopieerd of
afgekeken token niets meer opent voor een ander.

## Waarom

Een token was zijn eigen toegangsbewijs. `get_modal_state_by_token` laadde elke geldige
token, en geen enkele route vergeleek `state.project_name` met het project in het pad. Wie
een token in handen kreeg, kon er de wizardsessie van een ander mee doorlopen, inclusief
de projectgegevens die de state draagt. Hetzelfde gold voor
`GET /projects/{project}/modal-wizard/progress/{task_id}`: het taak-id alleen was genoeg
om de stappen van elke taak te lezen, onder welk project in het pad dan ook.

BIO2 5.15 en 8.03: een id of token is geen toegangsbewijs, de toegang hoort aan gebruiker
en project te hangen.

## Hoe het werkt

**De state draagt zijn eigenaar.** `WizardState.owner_email` wordt gezet door
`init_modal_state_tokenized` en is het enige e-mailadres waarvoor het token opent. Een
state zonder eigenaar, geschreven voor deze wijziging, geldt als verlopen: de gebruiker
krijgt de melding over een verlopen sessie en begint de modal opnieuw.

**Twee laders, één controle.** In `opi/forms/wizard/session.py`:

- `load_modal_state_for_user(request, token)` geeft de state alleen aan zijn eigenaar;
- `load_modal_state_for(request, token, project_name)` doet dat en eist daarbij dat
  `state.project_name` het project in het pad is.

`get_modal_state_by_token` wordt alleen nog binnen `session.py` aangeroepen. Elke route met
een project in zijn pad (`router_detail_edit.py`, `router_approvals.py`) gebruikt
`load_modal_state_for`; een state die niet opent levert dezelfde 400 met de melding over een
verlopen sessie als voorheen.

**De bijlageroutes.** `/forms/wizard/{flow_id}/attachments/...` heeft geen project in zijn
pad. Daar bindt de flow het verzoek aan de state: een cookie-state van een andere flow is
niet de sessie waarin deze upload thuishoort, en valt door naar het token in plaats van
voorrang te krijgen. Op de modal-state wordt de flow niet getoetst, omdat de
uploadpartial `create-project` in zijn URL zet zodra de modal hem rendert zonder flow; daar
doet het token zelf het werk, via de eigenaarscontrole.

**De voortgangsroute.** `modal_wizard_progress_html` gebruikt `_require_task_of_project`
uit `opi/web/router.py`, dezelfde controle als `task_progress_fragment`: de taak moet van
het project in het pad zijn (anders 404) en de gebruiker moet er toegang tot hebben
(anders 403). Een taak die niet bestaat en een taak van een ander project geven hetzelfde
antwoord, zodat het antwoord niet verraadt welk van de twee het was.

## Tests

```bash
cd operations-manager/python
uv run pytest tests/test_wizardtoken_eigenaar_en_project.py tests/test_progress_fragment_injection.py -q
```

`test_wizardtoken_eigenaar_en_project.py` toetst de twee laders, de routes die een token
aannemen, de flowbinding van de bijlageroutes, en met een AST-toets dat
`get_modal_state_by_token` buiten `session.py` niet wordt aangeroepen.
`test_progress_fragment_injection.py` toetst de voortgangsroutes, nu ook die van de modal.

## Afhankelijkheden

- `opi/forms/wizard/state.py`: het veld `owner_email`, meegeschreven in `to_dict` en gelezen
  in `from_dict`.
- `opi/web/router.py`: `_require_task_of_project` en `_require_task_access`.
