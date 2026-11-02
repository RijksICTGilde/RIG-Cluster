"""RC-358: docker-compose.dev.yaml moet OPI lokaal zonder TLS op de eigen Postgres laten
verbinden, met instellingen die Settings ook echt leest.

RC-275 zette DATABASE_SSLMODE met standaard require, maar de compose zette tot deze
wijziging DATABASE_URL, een naam die Settings niet kent. Die mismatch viel alleen op bij
het starten van de container, niet bij het lezen van het bestand.
"""

from __future__ import annotations

from pathlib import Path

from opi.core.config import Settings
from ruamel.yaml import YAML

COMPOSE_FILE = Path(__file__).parent.parent.parent.parent / "docker-compose.dev.yaml"


def _operations_manager_environment() -> dict[str, str]:
    yaml = YAML()
    with open(COMPOSE_FILE) as f:
        data = yaml.load(f)

    env_list = data["services"]["operations-manager"]["environment"]
    return dict(item.split("=", 1) for item in env_list)


def test_database_sslmode_is_disable_for_local_dev() -> None:
    env = _operations_manager_environment()

    assert env["DATABASE_SSLMODE"] == "disable"


def test_database_settings_match_known_settings_fields() -> None:
    env = _operations_manager_environment()
    known_fields = Settings.model_fields

    database_vars = {key: value for key, value in env.items() if key.startswith("DATABASE_")}

    assert database_vars, "geen DATABASE_-variabelen gevonden bij operations-manager"
    for key in database_vars:
        assert key in known_fields, f"{key} staat in docker-compose.dev.yaml maar niet in Settings"


def test_database_host_points_at_the_compose_postgres_service() -> None:
    env = _operations_manager_environment()

    assert env["DATABASE_HOST"] == "postgres"
