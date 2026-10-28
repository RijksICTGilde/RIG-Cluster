# Omgevingsvariabelen alleen achter een step-up

De `user-env-vars` van een component (en hun overschrijvingen per deployment) kunnen
geheimen bevatten. Sinds RC-388 staan die waarden **nergens meer in de HTML van het
portaal**, ook niet meer als naam of aantal. Wie de waarden wil zien
of wijzigen opent een eigen venster, en dat vraagt **altijd een verse step-up** - ook
voor een deployment die in ontwikkeling staat. De OTAP-markering versoepelt dit dus niet.

## Wat je waar ziet

- **Componenten-tabblad**: per componentkaart alleen de knop "Omgevingsvariabelen bewerken",
  naast Bewerken en Verwijderen (alleen voor wie mag bewerken). Geen namen, geen aantal en
  geen uitklaprij: ook dit venster is de enige weg om ze te zien of te zetten.
- **Deployments-tabblad**: per deployment-component alleen de knop "Omgevingsvariabelen
  bewerken" voor die scope. Geen namen en geen aantal overschrijvingen.
- **Het venster**: na een verse step-up toont het de echte waarden als `KEY=value`-regels
  in dezelfde editor als de wizard kende. Leeg opslaan haalt de variabelen van die scope
  weg. Na het opslaan wordt het project opnieuw verwerkt.
- **Wizard en component-modals**: het veld staat er niet meer in. Opslaan langs die
  wegen laat de opgeslagen waarde byte-gelijk staan (geen placeholder, geen
  terugzetstap: het veld wordt simpelweg niet aangeroerd). Één uitzondering: de
  **aanmaakwizard** vraagt de initiële variabelen wél, zonder step-up - op dat moment
  bestaat er nog geen opgeslagen waarde om terug te lezen. Alles daarna loopt via het
  venster.

## Waarom zo

Een eerder ontwerp (RC-337) hield het veld in de formulieren met een write-only
terugzetstap. Elke schrijfweg - modal, bewerkwizard, bewerkvenster, de herrender na een
validatiefout - moest die stap goed doen, en elke review vond een weg waarlangs een
geheim toch verdween of lekte. Het besluit van 21-10-2026: env-vars horen niet op de
weergavepagina en niet in een algemeen formulier, maar achter een step-up.

Dat het veld uit de formulieren is, staat als declaratie op de dienst:
`form_exempt_layers` in `opi/services/catalog/user_env_vars/__init__.py`. De algemene
regel "elke configlaag heeft een formulier of een reden" (tests/test_service_config_layers.py)
houdt dat vast.

## Hoe de techniek loopt

- De step-up is die van RC-332: `StepUpActie.ENV_VARS_BEHEREN` met
  `@requires_step_up(vraagt_altijd_step_up, ...)` op de GET én de POST van
  `opi/web/router_env_vars.py`. De opslag toetst de stempel dus opnieuw, ook als het
  venster al openstond. Het venster van vijf minuten schuift niet mee.
- De herhaalstap van de step-up-dialoog kent sinds RC-388 ook een GET als methode: het
  verborgen veld `vervolg_methode` reist mee in `bg/_step-up.html.j2`,
  `router_step_up.py` normaliseert het naar GET/POST en `bg/_step-up-herhalen.html.j2`
  herhaalt met die methode. Een leesvenster herhaalt dus geen POST.
- De knoppen op de kaarten zijn kaal htmx (`hx-get` naar het venster, doel
  `#edit-section-inner`). De inhoud die terugkomt opent de gedeelde modal zelf: de haak
  in `static/js/edit_modal.js` kijkt naar `data-step-up` (de dialoog) of
  `data-modal-fragment` (het venster, dat zijn titel meedraagt in `data-modal-title`).
- Versleuteling blijft zoals die was: dezelfde `KeyValueConverter` en
  `KeyValueValidator` als de editable van de dienst, dezelfde `read_user_env_vars` als
  de lees-API, en `keep_existing_ciphertext_if_unchanged` zodat een ongewijzigde inhoud
  geen nieuwe commit oplevert.

## Grens en vervolg

- De API verandert niet: de values-endpoints
  (`/api/v2/projects/{p}/services/user-env-vars/values/...`) kunnen waarden zetten met
  het projecttoken en de leesroutes maskeren ze met `***` (zie
  [component-values-api.md](component-values-api.md)). Of die schrijfweg ook een step-up
  moet dragen is een open punt.
- De voorwaarde is een declaratie op de handeling (het `actie`-argument), zodat
  "alleen bepaalde rollen" later een wijziging van die declaratie is en geen ingreep in
  generieke code. Nu geldt: wie het component mag bewerken (admin/owner) plus een verse
  step-up.
- `aliases` volgen hetzelfde patroon zodra dit bewezen is; dat is bewust buiten deze
  wijziging gehouden.
