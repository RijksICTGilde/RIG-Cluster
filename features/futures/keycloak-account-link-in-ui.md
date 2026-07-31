# Account-link van de Keycloak-service zichtbaar maken in de UI

Status: ontwerpnotitie (2026-07-31). Niet gebouwd. Aanleiding: de vraag of de Keycloak-service in de UI een veld heeft voor het automatisch aanmaken van gebruikers. Dat heeft hij niet, en het dichtstbijzijnde dat wél bestaat (`account-link`) is alleen in het projectbestand te zetten.

## De vraag en het eerlijke antwoord

Er is geen veld voor "gebruikers automatisch aanmaken", en dat kan er ook niet komen zoals de vraag klinkt: het aanmaken van een gebruikersrecord bij een eerste SSO-login is standaardgedrag van Keycloak zelf (first broker login) voor elke brokered IdP. Dat staat altijd aan en is nergens in ZAD configureerbaar, niet in de UI en niet in YAML.

Wat wél een keuze is, is wat er gebeurt als er al een lokaal account bestaat met hetzelfde e-mailadres als de identiteit die via SSO binnenkomt. Dat is `account-link`, beschreven in `features/keycloak-auto-link.md`. Het gaat dus over koppelen, niet over aanmaken. In de praktijk is dat waarschijnlijk waar de vraag vandaan komt: je maakt gebruikers vooruit aan in het realm (met de juiste rol, bijvoorbeeld `allowed-user` voor de authorization wall) en je wil dat ze bij hun eerste login zonder handmatige stap op dat account uitkomen.

## Wat de UI nu toont

De Keycloak-sectie (`config_form_section()` in `operations-manager/python/opi/services/catalog/keycloak/__init__.py`) heeft drie fieldsets en zes velden:

| Fieldset | Veld | Widget |
|---|---|---|
| Template | Keycloak template | select |
| Toegangsbeperking | Toegang beperken | checkbox |
| Toegangsbeperking | Realm rol | text |
| Toegangsbeperking | Foutmelding | text |
| Extra Keycloak clients | Client naam | text (sequence) |
| Extra Keycloak clients | Redirect URI's | text (sequence) |

`config_editables()` levert dezelfde set, dus ook via de modal-edit weg zit er geen account-link-veld.

Een detail dat bij het bouwen opvalt: `KEYCLOAK_REDIRECT_URIS` staat wel in `editables=[...]` van de FormSection maar niet in `layout=[...]`, dus "Extra redirect URI's" rendert niet in de sectie. Dat is een losstaand bugje, niet iets dat deze wijziging hoeft op te lossen, maar wel iets om niet per ongeluk te kopiëren.

## Wat er al klaarstaat

Alles behalve de UI-laag:

- `KeycloakConfig.account_link: AccountLink | None` met alias `account-link`, waarden `automatic` | `confirm` | `verify` (`catalog/keycloak/config_model.py`).
- Het gegenereerde JSON-schema `catalog/keycloak/keycloak.v1.0.json` kent `AccountLink` al, dus schemawerk is er niet.
- De API accepteert het al: `config_api_fields()` geeft de volledige `config_model_field_names()` terug. De docstring van de service erkent dat expliciet ("Its API surface is broader than the UI section").
- De lezer valideert al en faalt hard op een onbekende waarde (`KeycloakManager._get_keycloak_service_config`), en zet het door naar `KeycloakYamlHandler`, die bij `automatic` of `confirm` de custom first-broker-login flow installeert met `idp-auto-link`.

Ontbreekt: een `Editable`, een `EditableVisualizer`, een options-provider en een regel in de layout.

## Voorstel

1. **Options-provider** in `opi/forms/visualizers/providers.py`, in de stijl van `KeycloakTemplateOptionsProvider`, en registreren in de dict onderaan dat bestand. Vier opties, waarvan de eerste leeg is:
   - `""` → "Standaard Keycloak-flow (bevestigen en verifiëren via e-mail)"
   - `automatic` → "Automatisch koppelen aan een bestaand account met hetzelfde e-mailadres"
   - `confirm` → "Koppelen na één bevestigingsscherm"
   - `verify` → "Expliciet de standaardflow (gelijk aan leeg laten)"

   Of `verify` als aparte optie moet blijven bestaan is een keuze: hij doet hetzelfde als weglaten, dus vier opties waarvan er twee identiek gedrag geven is verwarrend. Voorkeur: alleen leeg, `automatic` en `confirm` tonen, en `verify` blijven accepteren in YAML voor bestaande bestanden.

2. **Editable** in `catalog/keycloak/editables.py`, met `yaml_path="services/keycloak/config/account-link"` (koppelteken, zoals de restrict-access-velden), `virtualize=_SVC_VIRT`, `converter=EmptyToNoneConverter()` en `remove_when_none=True`, zodat leeg laten de sleutel echt uit het projectbestand houdt in plaats van er `null` in te schrijven.

3. **Visualizer** in `catalog/keycloak/visualizers.py`, `WidgetType.SELECT`, met een `help_text` die het verschil tussen koppelen en aanmaken benoemt, want dat is precies de verwarring die deze notitie veroorzaakte.

4. **Opnemen** in `config_editables()` en in `config_form_section()`: een eigen fieldset "Gebruikers koppelen" onder Toegangsbeperking, of als vierde veld binnen Toegangsbeperking. Voorkeur voor een eigen fieldset, want het staat los van de rolcontrole.

5. **Test** in de stijl van de bestaande service-configtests: veld ingevuld op `automatic` levert `account-link: automatic` in het projectbestand, veld leeg levert géén sleutel.

## Beveiligingsafweging die in de help-tekst hoort

`automatic` koppelt een SSO-identiteit aan een bestaand lokaal account op basis van het e-mailadres, zonder e-mailverificatie of wachtwoord. Dat is alleen veilig omdat de IdP het e-mailadres autoritatief aanlevert en de gebruiker het zelf niet kan wijzigen. Die tweede voorwaarde was tot voor kort niet overal waar: realms die vóór de identity-field-lock zijn aangemaakt hadden een zelf-wijzigbaar e-mailadres in de account console. Zie de vergrendeling in `KeycloakConnector.lock_identity_fields()`, en het reconcile-gat dat daarbij hoort. Zolang die twee niet allebei kloppen, is `automatic` aanzetten geen goed idee.

Er is bovendien een tweede, ernstiger gat in dezelfde hoek: een gebruiker kan in de account console zelf een wachtwoord zetten en de IdP-koppeling verwijderen, en daarmee SSO Rijk helemaal omzeilen. Zie [keycloak-sso-bypass-voorkomen.md](keycloak-sso-bypass-voorkomen.md). Zolang dat openstaat, is `automatic` aanbieden in de UI niet verstandig.

Praktisch gevolg voor de UI: het veld mag geen kale keuzelijst zijn. Er hoort bij te staan dat `automatic` alleen verantwoord is als de identiteit uit een vertrouwde IdP komt.

## Wat expliciet buiten scope blijft

- Gebruikers aanmaken of beheren vanuit de ZAD-UI. Dat is realmbeheer en hoort in de Keycloak admin console, niet in het projectbestand.
- De platform-realm. Auto-link is per ontwerp alleen voor projectrealms; de platform-realm houdt altijd de stock flow.
- Het niet-renderende veld "Extra redirect URI's" hierboven. Los bugje, losse fix.
