# Test: create-project wizard end-to-end tegen de sandbox

Test de "nieuw project aanmaken"-wizard end-to-end tegen de sandbox.

Schrijf/uitbreid een Playwright E2E-test die alleen de create-project wizard doorloopt (`/forms/wizard/create-project`), maar volledig: alle services aangevinkt (publish-on-web, keycloak, postgresql, auth-wall) en per stap alle velden ingevuld. Test ook heen-en-terug navigeren tussen stappen en controleer dat ingevulde waarden behouden blijven. Na submit: controleer de gegenereerde project-YAML in de Forgejo `zad-projects` repo (via `ForgejoClient`) én controleer dat het project in de sandbox verschijnt.

Lees eerst `workflow/sandbox.md` (de sandbox is een lokale Kind-cluster op `https://zad.sandbox.rijksapp.dev`), `features/e2e-sandbox-tests.md`, en gebruik het patroon uit `operations-manager/python/tests/e2e/test_sandbox_flows.py` plus de helpers `WizardHelper` en `ForgejoClient`. Draai met `task test-e2e-sandbox` (marker `e2e and sandbox`); zonder draaiende sandbox skippen de tests.

**Belangrijk - htmx:** de wizard werkt met htmx en haalt tussentijds UI-delen op (stappen, presets, config-blokken verschijnen na een swap). Wacht expliciet tot elk opgehaald deel geladen is (bv. `wait_for_step` / wachten op de verwachte velden) vóór je verder klikt of invult, anders loopt de flow mis. Vertrouw niet op vaste sleeps.
