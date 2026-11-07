# Een nieuwe Keycloak-actie, en waar zijn toets komt

Twee regels, en dan weet je het:

1. **Een nieuwe methode op `KeycloakConnector`** krijgt zijn toets in
   `tests/integration/test_keycloak_admin_api.py`: aanroepen, en terugleren wat Keycloak
   opsloeg. `tests/test_keycloak_admin_api_coverage.py` faalt in de standaardrun zodra `opi/`
   een methode aanroept die daar niet staat.
2. **Een nieuwe STAP of een nieuwe ORDE in `opi/manager/keycloak_manager.py` of
   `opi/handlers/keycloak_yaml_handler.py`** krijgt zijn toets in
   `tests/integration/test_keycloak_scenarios.py`, over meer dan een deployment in hetzelfde
   realm. Daar hoort ook een directe `.admin.*`-aanroep: die moet in `DIRECT_ADMIN_CALLS` van
   de dekkingstoets de toets noemen die hem draait, anders faalt die toets.

Draaien doe je ze met `task test-keycloak` (de hele groep `keycloak_live`, tegen de image die
productie draait). De standaardrun slaat ze over, dus een groene `uv run pytest` zegt hier
niets; de CI-job `keycloak-live` draait ze wel. Achtergrond en wat de invariant na elke stap
eist: `features/keycloak-eigen-image.md`.
