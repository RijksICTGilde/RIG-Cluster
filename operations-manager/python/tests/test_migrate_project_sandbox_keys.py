"""Tests voor de eigen projectsleutel per gemigreerd project (RC-288).

Vroeger: project hield zijn key, private helft opnieuw versleuteld voor sandbox. Risico:
waarden die later in productie bijkomen openen ook op sandbox. Nu: conversie maakt eigen
sleutelpaar. Sleutels per test via generate_sops_key_pair.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from opi.core.project_schema import find_plaintext_secret_violations, validate_project_schema
from opi.manager.project_validation import find_plaintext_service_config_violations, validate_project_structure
from opi.services.project_leden import hash_van
from opi.services.schema_migration import LATEST_SCHEMA_VERSION_VALUE
from opi.utils.age import (
    decrypt_age_block_to_bytes_sync,
    decrypt_age_content_sync,
    encrypt_age_content_sync,
    encrypt_file_to_age_block_sync,
    is_age_encrypted,
)
from opi.utils.sops import generate_sops_key_pair

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from migrate_project_to_sandbox import (  # noqa: E402
    AGE_FIELD_HANDLING,
    SANDBOX_PLACEHOLDER,
    migrate_project,
    replace_remaining_secrets,
)

_SCHEMA = Path(__file__).resolve().parent.parent / "opi" / "schemas" / "project_v2.json"

ALIASES = "POSTGRES_HOST=$DATABASE_SERVER_HOST\nPOSTGRES_PORT=$DATABASE_SERVER_PORT\n"


class Keys:
    """De drie sleutelparen die in dit verhaal meedoen."""

    def __init__(self) -> None:
        self.prod_private, self.prod_public = generate_sops_key_pair()
        self.sandbox_private, self.sandbox_public = generate_sops_key_pair()
        self.project_private, self.project_public = generate_sops_key_pair()


@pytest.fixture
def keys() -> Keys:
    return Keys()


@pytest.fixture
def production_project(keys: Keys) -> dict[str, Any]:
    """Een productiebestand met alles wat met de projectsleutel versleuteld is."""
    project = keys.project_public
    return {
        "schema-version": LATEST_SCHEMA_VERSION_VALUE,
        "name": "demo",
        "clusters": ["odcn-production"],
        "users": [{"email": "team@rijksoverheid.nl", "role": "admin"}],
        "services": [
            "publish-on-web",
            {
                "attachments": {
                    "data": [
                        {
                            "id": "server-cert",
                            "filename": "server.pem",
                            "content": encrypt_file_to_age_block_sync(b"-----BEGIN CERTIFICATE-----", project),
                        }
                    ]
                }
            },
            {
                "name": "image-registries",
                "config": {
                    "registries": [
                        {
                            "name": "github-registry",
                            "upstream": "ghcr.io",
                            "username": "someuser",
                            "password": encrypt_age_content_sync("ghp-productietoken", project),
                        }
                    ]
                },
            },
        ],
        "repositories": [
            {
                "name": "main-repo",
                "url": "https://github.com/org/repo.git",
                "username": "git",
                "password": encrypt_age_content_sync("productie-repo-wachtwoord", keys.prod_public),
                "branch": "main",
                "path": ".",
            }
        ],
        "components": [
            {
                "name": "api",
                "ports": {"inbound": [8000], "outbound": [443]},
                "aliases": encrypt_age_content_sync(ALIASES, project),
                "user-env-vars": encrypt_age_content_sync("DJANGO_SECRET_KEY=s3cr3t\n", project),
                "services": ["publish-on-web"],
            }
        ],
        "deployments": [
            {
                "name": "deployment-1",
                "cluster": "odcn-production",
                "namespace": "demo",
                "repository": "main-repo",
                "configuration": encrypt_age_content_sync("DEPLOY_TOKEN=abc\n", project),
                "sleep": {"state": "sleeping", "wake-token": encrypt_age_content_sync("wektoken", project)},
                "components": [
                    {
                        "reference": "api",
                        "image": "ghcr.io/org/api:1",
                        "user-env-vars": encrypt_age_content_sync("API_TOKEN=t0ken\n", project),
                    }
                ],
            }
        ],
        "config": {
            "age-public-key": project,
            "age-private-key": encrypt_age_content_sync(keys.project_private, keys.prod_public),
            "api-key": encrypt_age_content_sync("0123456789abcdef0123456789abcdef", project),
            "keycloak": [{"host": "https://keycloak.rijksapp.nl", "realm": "demo"}],
        },
    }


@pytest.fixture
def migrated(production_project: dict[str, Any], keys: Keys) -> dict[str, Any]:
    return migrate_project(production_project, keys.prod_private, keys.sandbox_public)


def age_blocks(node: Any, path: str = "") -> list[tuple[str, str]]:
    """Elk AGE-blok in het document, als (pad, waarde)."""
    if isinstance(node, dict):
        return [pair for key, value in node.items() for pair in age_blocks(value, f"{path}/{key}")]
    if isinstance(node, list):
        return [pair for item in node for pair in age_blocks(item, f"{path}[]")]
    if isinstance(node, str) and is_age_encrypted(node):
        return [(path, node)]
    return []


class TestNewProjectKey:
    def test_the_project_gets_a_key_of_its_own(self, production_project: dict, migrated: dict, keys: Keys) -> None:
        assert migrated["config"]["age-public-key"] != keys.project_public
        assert migrated["config"]["age-private-key"] != production_project["config"]["age-private-key"]

    def test_the_new_private_key_opens_with_the_sandbox_key(self, migrated: dict, keys: Keys) -> None:
        opened = decrypt_age_content_sync(migrated["config"]["age-private-key"], keys.sandbox_private)

        assert opened is not None
        assert opened.startswith("AGE-SECRET-KEY-")
        assert opened != keys.project_private

    def test_the_stored_pair_belongs_together(self, migrated: dict, keys: Keys) -> None:
        """De publieke helft in het bestand moet de private helft eronder zijn."""
        private = decrypt_age_content_sync(migrated["config"]["age-private-key"], keys.sandbox_private)
        assert private is not None

        block = encrypt_age_content_sync("rondje", migrated["config"]["age-public-key"])

        assert decrypt_age_content_sync(block, private) == "rondje"


class TestEverythingOnTheNewKey:
    """De meting die de scheiding aantoont: niets in de uitvoer opent nog met de oude sleutel."""

    def _new_private_key(self, migrated: dict, keys: Keys) -> str:
        private = decrypt_age_content_sync(migrated["config"]["age-private-key"], keys.sandbox_private)
        assert private is not None
        return private

    def test_no_block_opens_with_the_old_project_key(self, migrated: dict, keys: Keys) -> None:
        for path, block in age_blocks(migrated):
            assert decrypt_age_content_sync(block, keys.project_private) is None, path

    def test_every_block_opens_with_the_new_project_key(self, migrated: dict, keys: Keys) -> None:
        new_private = self._new_private_key(migrated, keys)

        for path, block in age_blocks(migrated):
            if path == "/config/age-private-key":
                # Die staat op de clustersleutel van de sandbox, niet op de projectsleutel.
                continue
            assert decrypt_age_content_sync(block, new_private) is not None, path

    def test_de_projectsleutel_wordt_een_vers_token(self, migrated: dict) -> None:
        """De productiesleutel gaat niet mee: een token dat daar werkt hoort hier niet te werken."""
        assert "api-key" not in migrated["config"]

        account = next(lid for lid in migrated["users"] if lid.get("name") == "project-api")
        hash_hex = account["tokens"][0]["hash"]
        assert len(hash_hex) == 64
        assert hash_hex != hash_van("0123456789abcdef0123456789abcdef")

    def test_een_bestaand_token_wordt_opnieuw_gemunt(self, production_project: dict, keys: Keys) -> None:
        production_project["users"] = [
            {
                "email": "lid@rijksoverheid.nl",
                "role": "admin",
                "tokens": [{"name": "cli", "expires": "2030-01-01T00:00:00+00:00", "hash": "a" * 64}],
            }
        ]
        omgezet = migrate_project(production_project, keys.prod_private, keys.sandbox_public)

        hashes = [t["hash"] for lid in omgezet["users"] for t in lid.get("tokens") or []]
        assert "a" * 64 not in hashes

    def test_user_env_var_values_are_a_placeholder_with_the_keys_kept(self, migrated: dict, keys: Keys) -> None:
        new_private = self._new_private_key(migrated, keys)

        component = decrypt_age_content_sync(migrated["components"][0]["user-env-vars"], new_private)
        deployed = decrypt_age_content_sync(migrated["deployments"][0]["components"][0]["user-env-vars"], new_private)

        # De uitpakker haalt de laatste witregel eraf; de sleutelnaam is wat moet blijven.
        assert component == f"DJANGO_SECRET_KEY={SANDBOX_PLACEHOLDER}"
        assert deployed == f"API_TOKEN={SANDBOX_PLACEHOLDER}"

    def test_aliases_keep_their_content(self, migrated: dict, keys: Keys) -> None:
        """Een alias verwijst naar een platformvariabele en is geen geheim, dus de inhoud blijft."""
        new_private = self._new_private_key(migrated, keys)

        assert decrypt_age_content_sync(migrated["components"][0]["aliases"], new_private) == ALIASES.strip()

    def test_the_legacy_alias_mapping_keeps_its_values(self, production_project: dict, keys: Keys) -> None:
        """Voor RC-106 was aliases een mapping met elke waarde apart versleuteld.

        Die vorm staat nog in oude productiebestanden. Zonder eigen behandeling zet het
        vangnet ze op een placeholder, en dat is precies de kapotte alias uit de RC-22-ronde.
        """
        production_project["components"][0]["aliases"] = {
            "POSTGRES_HOST": encrypt_age_content_sync("$DATABASE_SERVER_HOST", keys.project_public)
        }

        migrated = migrate_project(production_project, keys.prod_private, keys.sandbox_public)
        new_private = self._new_private_key(migrated, keys)

        alias = migrated["components"][0]["aliases"]["POSTGRES_HOST"]
        assert decrypt_age_content_sync(alias, new_private) == "$DATABASE_SERVER_HOST"

    def test_a_registry_password_becomes_a_placeholder(self, migrated: dict, keys: Keys) -> None:
        """Niet in het schema als ``age-encrypted``, wel een productiecredential."""
        new_private = self._new_private_key(migrated, keys)
        registries = migrated["services"][2]["config"]["registries"]

        assert decrypt_age_content_sync(registries[0]["password"], new_private) == SANDBOX_PLACEHOLDER


class TestRekeyAliasesUnreadable:
    """Een alias die met geen van beide projectsleutels opent: blijft staan, wordt gemeld."""

    def test_a_string_alias_block_is_left_alone_and_reported(
        self, production_project: dict, keys: Keys, caplog: pytest.LogCaptureFixture
    ) -> None:
        _, other_public = generate_sops_key_pair()
        unreadable = encrypt_age_content_sync(ALIASES, other_public)
        production_project["components"][0]["aliases"] = unreadable

        with caplog.at_level("WARNING"):
            migrated = migrate_project(production_project, keys.prod_private, keys.sandbox_public)

        assert migrated["components"][0]["aliases"] == unreadable
        assert "aliases niet te openen" in caplog.text

    def test_a_legacy_mapping_value_is_left_alone_and_reported(
        self, production_project: dict, keys: Keys, caplog: pytest.LogCaptureFixture
    ) -> None:
        _, other_public = generate_sops_key_pair()
        unreadable = encrypt_age_content_sync("$DATABASE_SERVER_HOST", other_public)
        production_project["components"][0]["aliases"] = {"POSTGRES_HOST": unreadable}

        with caplog.at_level("WARNING"):
            migrated = migrate_project(production_project, keys.prod_private, keys.sandbox_public)

        assert migrated["components"][0]["aliases"]["POSTGRES_HOST"] == unreadable
        assert "alias 'POSTGRES_HOST' niet te openen" in caplog.text


class TestAttachmentsAndWakeToken:
    def test_the_attachment_content_becomes_a_placeholder(self, migrated: dict, keys: Keys) -> None:
        private = decrypt_age_content_sync(migrated["config"]["age-private-key"], keys.sandbox_private)
        assert private is not None
        entry = migrated["services"][1]["attachments"]["data"][0]

        # Door dezelfde uitpakker als OPI gebruikt: de platte tekst is base64 van bytes.
        assert decrypt_age_block_to_bytes_sync(entry["content"], private) == SANDBOX_PLACEHOLDER.encode()

    def test_the_attachment_keeps_its_metadata(self, migrated: dict) -> None:
        entry = migrated["services"][1]["attachments"]["data"][0]

        assert entry["id"] == "server-cert"
        assert entry["filename"] == "server.pem"

    def test_the_wake_token_is_minted_again(self, migrated: dict, keys: Keys) -> None:
        private = decrypt_age_content_sync(migrated["config"]["age-private-key"], keys.sandbox_private)
        assert private is not None
        sleep = migrated["deployments"][0]["sleep"]

        token = decrypt_age_content_sync(sleep["wake-token"], private)

        assert token is not None
        assert token != "wektoken"
        assert sleep["state"] == "sleeping"


class TestReplaceRemainingSecrets:
    """Het vangnet apart gemeten: welke waarde wordt vervangen, en welke gemeld."""

    def test_a_value_on_the_old_key_is_replaced(self, keys: Keys) -> None:
        project = {"a": encrypt_age_content_sync("geheim", keys.project_public)}

        replaced = replace_remaining_secrets(
            project, keys.project_private, keys.sandbox_private, keys.sandbox_public, "demo"
        )

        assert replaced == ["/a"]
        assert decrypt_age_content_sync(project["a"], keys.sandbox_private) == SANDBOX_PLACEHOLDER

    def test_a_value_already_on_the_new_key_is_left_alone(self, keys: Keys) -> None:
        stays = encrypt_age_content_sync("al omgezet", keys.sandbox_public)
        project = {"a": stays}

        replaced = replace_remaining_secrets(
            project, keys.project_private, keys.sandbox_private, keys.sandbox_public, "demo"
        )

        assert replaced == []
        assert project["a"] == stays

    def test_a_value_on_neither_key_is_reported_and_left_alone(
        self, keys: Keys, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Een waarde op de clustersleutel van productie: niet stil vervangen, wel melden."""
        other = encrypt_age_content_sync("van een andere sleutel", keys.prod_public)
        project = {"a": other}

        with caplog.at_level("WARNING"):
            replaced = replace_remaining_secrets(
                project, keys.project_private, keys.sandbox_private, keys.sandbox_public, "demo"
            )

        assert replaced == []
        assert project["a"] == other
        assert "/a" in caplog.text


def age_encrypted_fields(schema: dict[str, Any]) -> set[str]:
    """Elk veld in het schema dat naar ``age-encrypted`` verwijst, als ``<def>.<veldpad>``.

    De naam van een veld is het ``$defs``-blok waar het in staat plus het pad eronder, want
    dat is hoe het schema het beschrijft; een documentpad zou per schemaversie verschuiven.
    """
    found: set[str] = set()

    def walk(node: Any, trail: list[str]) -> None:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.rsplit("/", 1)[-1] in ("age-encrypted", "age-encrypted-or-plain"):
                # Het veldpad zonder de schematrefwoorden ertussen: properties, oneOf, index.
                parts = [p for p in trail if p not in ("properties", "oneOf") and not p.isdigit()]
                found.add(f"{parts[0]}.{'.'.join(parts[1:])}" if len(parts) > 1 else parts[0])
            for key, value in node.items():
                walk(value, [*trail, key])
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, [*trail, str(index)])

    for name, definition in schema.get("$defs", {}).items():
        # De definitie van de vorm zelf is geen veld.
        if name in ("age-encrypted", "age-encrypted-or-plain"):
            continue
        walk(definition, [name])
    return found


class TestAgeFieldGuard:
    """De guard: een nieuw versleuteld veld in het schema moet een beslissing afdwingen."""

    def _schema(self) -> dict[str, Any]:
        return json.loads(_SCHEMA.read_text())

    def test_every_encrypted_field_in_the_schema_is_handled(self) -> None:
        assert age_encrypted_fields(self._schema()) == set(AGE_FIELD_HANDLING)

    def test_a_new_encrypted_field_is_not_handled(self) -> None:
        schema = self._schema()
        schema["$defs"]["component"]["properties"]["nieuw-geheim"] = {"$ref": "#/$defs/age-encrypted"}

        assert age_encrypted_fields(schema) - set(AGE_FIELD_HANDLING) == {"component.nieuw-geheim"}


class TestProjectWithoutAProjectKey:
    def test_a_file_without_age_private_key_is_left_on_its_own_key(self, production_project: dict, keys: Keys) -> None:
        """Zonder leesbare projectsleutel valt er niets om te zetten; de conversie loopt door."""
        del production_project["config"]["age-private-key"]

        migrated = migrate_project(production_project, keys.prod_private, keys.sandbox_public)

        assert migrated["config"]["age-public-key"] == keys.project_public
        assert "age-private-key" not in migrated["config"]


class TestTheConvertedFilePassesOpiValidation:
    """Laag 1 van de upgrade-veiligheidstest, op de uitvoer: de validatieketen die OPI voor
    elke schrijfactie draait. Een placeholder op de verkeerde plek valt hier om zonder dat er
    een cluster bij hoeft, en dat is precies de klasse fouten die deze conversie kan maken.
    """

    def test_the_schema_accepts_it(self, migrated: dict) -> None:
        validate_project_schema(migrated)

    def test_no_field_that_must_be_encrypted_holds_plain_text(self, migrated: dict) -> None:
        # Twee helften: de velden die project_v2.json beschrijft, en de velden in een
        # service-config, die het model van die service beschrijft.
        assert find_plaintext_secret_violations(migrated) == []
        assert find_plaintext_service_config_violations(migrated) == []

    async def test_the_structure_holds(self, migrated: dict) -> None:
        await validate_project_structure(migrated)
