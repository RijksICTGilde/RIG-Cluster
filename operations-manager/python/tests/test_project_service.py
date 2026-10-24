"""Tests for opi.services.project_service module.

Focuses on non-trivial logic: data parsing/validation in load_project_from_data,
case-insensitive email matching, singleton behavior, and defensive copy semantics.
"""

import pytest
from opi.services.project_authorization import get_project_users
from opi.services.project_service import (
    ProjectService,
    ProjectUser,
    get_project_service,
)


@pytest.fixture(autouse=True)
def reset_project_service_singleton():
    """Reset ProjectService singleton between tests."""
    ProjectService._instance = None
    ProjectService._initialized = False
    yield
    ProjectService._instance = None
    ProjectService._initialized = False


@pytest.fixture
def service():
    return ProjectService()


class TestSingleton:
    """Singleton pattern matters - double-init would wipe state."""

    def test_second_init_preserves_data(self):
        s1 = ProjectService()
        s1.register("proj", "key", "f.yaml")
        s2 = ProjectService()
        assert s2.get_project("proj") is not None, "Second __init__ must not reset state"

    def test_get_project_service_returns_same_instance(self):
        s1 = get_project_service()
        s1.register("proj", "key", "f.yaml")
        s2 = get_project_service()
        assert s2.get_project("proj") is not None


class TestGetAllProjectsReturnsCopy:
    """get_all_projects must return a copy - callers mutating the dict would corrupt state."""

    def test_mutation_does_not_affect_internal_state(self, service):
        service.register("a", "k1", "a.yaml")
        leaked = service.get_all_projects()
        leaked.pop("a")
        assert service.get_project("a") is not None


class TestLoadProjectFromData:
    """load_project_from_data has real parsing logic: field extraction, validation, user filtering."""

    def test_extracts_api_key_from_nested_config(self, service):
        data = {
            "name": "myproject",
            "config": {"api-key": "secret123"},
        }
        assert service.load_project_from_data(data, "proj.yaml") is True
        assert service.get_project("myproject").api_key == "secret123"

    def test_parses_users_with_email_and_role(self, service):
        data = {
            "name": "proj",
            "config": {"api-key": "k"},
            "users": [
                {"email": "a@b.com", "role": "admin"},
                {"email": "c@d.com", "role": "viewer"},
            ],
        }
        service.load_project_from_data(data, "f.yaml")
        users = get_project_users("proj")
        assert len(users) == 2
        assert users[0].email == "a@b.com"
        assert users[1].role == "viewer"

    def test_rejects_missing_name(self, service):
        assert service.load_project_from_data({"config": {"api-key": "k"}}, "f.yaml") is False

    def test_laadt_een_project_zonder_projectsleutel(self, service):
        """Sinds schemaversie 2.12 heeft een project geen ``config.api-key`` meer: zijn
        bewijsmiddelen zijn tokens bij de leden. Een bestand zonder sleutel weigeren zou
        elk gemigreerd project uit de cache houden."""
        assert service.load_project_from_data({"name": "proj", "config": {}}, "f.yaml") is True

    def test_laadt_een_project_zonder_configblok(self, service):
        assert service.load_project_from_data({"name": "proj"}, "f.yaml") is True

    def test_skips_malformed_users(self, service):
        """Users missing required fields or not dicts should be silently dropped."""
        data = {
            "name": "proj",
            "config": {"api-key": "k"},
            "users": [
                {"email": "valid@x.com", "role": "admin"},
                {"bad": "entry"},  # missing email/role
                "not-a-dict",  # wrong type entirely
                {"email": "no-role@x.com"},  # missing role
            ],
        }
        service.load_project_from_data(data, "f.yaml")
        users = get_project_users("proj")
        assert len(users) == 1
        assert users[0].email == "valid@x.com"

    def test_empty_users_list_stores_none(self, service):
        """Empty users list should become None (the register call passes None for empty)."""
        data = {"name": "proj", "config": {"api-key": "k"}, "users": []}
        service.load_project_from_data(data, "f.yaml")
        assert service.get_project("proj").users is None

    def test_preserves_full_project_data(self, service):
        """load_project_from_data must pass the full data dict through to register() so Project.data is set."""
        data = {
            "name": "proj",
            "config": {"api-key": "k", "extra": "value"},
            "deployments": [{"cluster": "local", "namespace": "ns"}],
        }
        service.load_project_from_data(data, "f.yaml")
        project = service.get_project("proj")
        assert project.data is not None, "Project.data should contain the original project data"
        assert project.data["deployments"][0]["cluster"] == "local"


AGE_API_KEY = "-----BEGIN AGE ENCRYPTED FILE-----\nciphertext-api-key\n-----END AGE ENCRYPTED FILE-----"
AGE_PRIVATE_KEY = "-----BEGIN AGE ENCRYPTED FILE-----\nciphertext-private-key\n-----END AGE ENCRYPTED FILE-----"


def _encrypted_project_data() -> dict:
    return {
        "name": "proj",
        "config": {"api-key": AGE_API_KEY, "age-private-key": AGE_PRIVATE_KEY},
    }


class TestApiKeyDecryptionOnLoad:
    """load_project_from_data must register the *plaintext* API key.

    Project files store config.api-key AGE-encrypted. Portal saves re-register
    via load_project_from_data; registering the raw ciphertext poisons the
    in-memory key and 401s every API call for the project until the next
    process run (the wies pr-394 CI failure of 2026-06-11).
    """

    def _patch_decrypt(self, monkeypatch):
        """Route the two-step chain: unwrap the project key, then open the api key with it."""

        def fake_unwrap(wrapped: str) -> str:
            if wrapped == AGE_PRIVATE_KEY:
                return "AGE-SECRET-KEY-PROJECT"
            raise ValueError("not the project key")

        def fake_decrypt(content: str, private_key: str) -> str | None:
            if content == AGE_API_KEY and private_key == "AGE-SECRET-KEY-PROJECT":
                return "plain-api-key"
            return None

        import opi.services.project_service as ps

        monkeypatch.setattr(ps, "unwrap_project_key_sync", fake_unwrap)
        monkeypatch.setattr(ps, "decrypt_age_content_sync", fake_decrypt)

    def test_encrypted_api_key_is_decrypted_before_registration(self, service, monkeypatch):
        self._patch_decrypt(monkeypatch)

        assert service.load_project_from_data(_encrypted_project_data(), "proj.yaml") is True
        assert service.get_project("proj").api_key == "plain-api-key"

    def test_decrypt_failure_keeps_previously_registered_key(self, service, monkeypatch):
        import opi.services.project_service as ps

        service.register("proj", "plain-api-key", "proj.yaml")
        monkeypatch.setattr(ps, "unwrap_project_key_sync", lambda *_: "AGE-SECRET-KEY-PROJECT")
        monkeypatch.setattr(ps, "decrypt_age_content_sync", lambda *_: None)

        assert service.load_project_from_data(_encrypted_project_data(), "proj.yaml") is True
        assert service.get_project("proj").api_key == "plain-api-key", (
            "A failed decrypt must not poison the in-memory key"
        )

    def test_plaintext_api_key_passes_through(self, service):
        data = {"name": "proj", "config": {"api-key": "plain-api-key"}}
        assert service.load_project_from_data(data, "proj.yaml") is True
        assert service.get_project("proj").api_key == "plain-api-key"

    def test_decrypt_failure_without_previous_registers_nothing(self, service, monkeypatch):
        """Een sleutel die niet open gaat legitimeert niemand, ook niet als zijn ciphertext.

        De ciphertext teruggeven maakte hem zijn eigen wachtwoord: ``herken_principaal``
        vergelijkt de aangeboden sleutel met de geregistreerde waarde.
        """
        import opi.services.project_service as ps

        monkeypatch.setattr(ps, "unwrap_project_key_sync", lambda *_: "AGE-SECRET-KEY-PROJECT")
        monkeypatch.setattr(ps, "decrypt_age_content_sync", lambda *_: None)

        assert service.load_project_from_data(_encrypted_project_data(), "proj.yaml") is True
        assert service.get_project("proj").api_key is None

    def test_zonder_age_private_key_in_config_blijft_de_vorige_sleutel_staan(self, service, monkeypatch):
        """``age-private-key`` kan ontbreken (een bestand midden in de projectsleutel-
        migratie); zonder sleutel om mee te ontsleutelen valt het terug op het vorige
        geregistreerde adres, net als bij een mislukte ontsleuteling."""
        import opi.services.project_service as ps

        service.register("proj", "plain-api-key", "proj.yaml")
        monkeypatch.setattr(
            ps, "unwrap_project_key_sync", lambda *_: (_ for _ in ()).throw(AssertionError("niet aan te roepen"))
        )

        data = {"name": "proj", "config": {"api-key": AGE_API_KEY}}
        assert service.load_project_from_data(data, "proj.yaml") is True
        assert service.get_project("proj").api_key == "plain-api-key"

    def test_een_niet_uit_te_pakken_projectsleutel_valt_terug_op_de_vorige(self, service, monkeypatch):
        """``unwrap_project_key_sync`` faalt met een verkeerde KEK; de eerder geregistreerde
        plaintext blijft staan in plaats van de pijplijn stuk te maken op een 401."""
        import opi.services.project_service as ps

        service.register("proj", "plain-api-key", "proj.yaml")
        monkeypatch.setattr(
            ps, "unwrap_project_key_sync", lambda *_: (_ for _ in ()).throw(ValueError("verkeerde kek"))
        )

        assert service.load_project_from_data(_encrypted_project_data(), "proj.yaml") is True
        assert service.get_project("proj").api_key == "plain-api-key"


class TestRegisterSyncsAllowlist:
    """register() must add member emails to the global access allowlist.

    The team-edit modal saves via load_project_from_data -> register only.
    Before this sync, a newly added member was redirected to /permission-denied
    until the next periodic Git refresh (or app restart) rebuilt the allowlist.
    """

    @pytest.fixture(autouse=True)
    def reset_user_service_singleton(self):
        import opi.services.user_service as us

        us._user_service = None
        yield
        us._user_service = None

    def test_register_adds_member_emails_to_allowlist(self, service):
        from opi.services.user_service import get_user_service

        users = [ProjectUser(email="New.Member@Example.com", role="admin")]
        service.register("proj", "k", "f.yaml", users=users)
        assert get_user_service().is_email_allowed("new.member@example.com") is True

    def test_load_project_from_data_adds_member_emails_to_allowlist(self, service):
        from opi.services.user_service import get_user_service

        data = {
            "name": "proj",
            "config": {"api-key": "k"},
            "users": [{"email": "member@example.com", "role": "admin"}],
        }
        service.load_project_from_data(data, "f.yaml")
        assert get_user_service().is_email_allowed("member@example.com") is True

    def test_register_without_users_does_not_crash(self, service):
        from opi.services.user_service import get_user_service

        service.register("proj", "k", "f.yaml")
        assert get_user_service().is_email_allowed("anybody@example.com") is False

    def test_register_accepts_raw_dicts_as_users(self, service):
        """De e2e-testserver geeft leden als ruwe dicts door, geen ``ProjectUser``.

        ``register`` zette ze voorheen rechtstreeks in ``ProjectSummary.users`` en viel
        later om op ``lid.type`` (een attribuut dat een dict niet heeft); nu normaliseert
        hij ze een keer naar ``ProjectUser``.
        """
        from opi.services.user_service import get_user_service

        service.register("proj", "k", "f.yaml", users=[{"email": "dict.member@example.com", "role": "admin"}])

        project = service.get_project("proj")
        assert project.users[0].email == "dict.member@example.com"
        assert get_user_service().is_email_allowed("dict.member@example.com") is True
