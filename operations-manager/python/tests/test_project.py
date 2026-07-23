"""Tests for Project -- the single generic read/query/mutate entry point.

Covers the two generic method families: path/reference access, and service-generic
access ("give me X of service Y", parameterised by name). Also asserts the core
invariant that mutation preserves list order.
"""

import pytest
from opi.services.project import Project
from opi.services.services_enums import ServiceType
from pydantic import ValidationError


def _project() -> dict:
    return {
        "name": "demo",
        "services": [
            "publish-on-web",
            {"keycloak": {"config": {"template": "sso-support"}}},
            {"namespace-postgresql-database": {"config": {"instances": 2, "storage": "5Gi"}}},
        ],
        "components": [
            {"name": "api", "ports": {"inbound": [8000]}},
            {"name": "web", "ports": {"inbound": [3000]}},
        ],
        "deployments": [
            {"name": "prod", "components": [{"reference": "api"}, {"reference": "web"}]},
        ],
    }


class TestPathAccess:
    def test_get_plain_path(self) -> None:
        assert Project(_project()).get("name") == "demo"

    def test_get_field_match_filter(self) -> None:
        v = Project(_project())
        assert v.get("components{name=api}/ports/inbound[0]") == 8000

    def test_get_service_config_path(self) -> None:
        v = Project(_project())
        assert v.get("services/keycloak/config/template") == "sso-support"

    def test_get_missing_returns_default(self) -> None:
        assert Project(_project()).get("nope/x", default="d") == "d"

    def test_exists(self) -> None:
        v = Project(_project())
        assert v.exists("services/keycloak/config/template")
        assert not v.exists("services/keycloak/config/missing")

    def test_set_is_chainable_and_mutates_underlying_dict(self) -> None:
        data = _project()
        v = Project(data)
        v.set("services/keycloak/config/template", "sso-only").set("name", "renamed")
        assert data["name"] == "renamed"
        assert v.get("services/keycloak/config/template") == "sso-only"

    def test_delete(self) -> None:
        v = Project(_project())
        v.delete("services/keycloak/config/template")
        assert v.get("services/keycloak/config/template") is None

    def test_data_returns_wrapped_dict(self) -> None:
        data = _project()
        assert Project(data).data is data


class TestReferenceLookup:
    def test_find_by_name(self) -> None:
        v = Project(_project())
        assert v.find("components", name="web")["ports"]["inbound"] == [3000]

    def test_find_missing_returns_none(self) -> None:
        assert Project(_project()).find("components", name="ghost") is None

    def test_find_all(self) -> None:
        v = Project(_project())
        assert len(v.find_all("components")) == 2

    def test_locate_over_nested_list_by_reference(self) -> None:
        v = Project(_project())
        deployment = v.find("deployments", name="prod")
        assert Project.locate(deployment["components"], reference="web") == {"reference": "web"}

    def test_locate_multiple_fields(self) -> None:
        items = [{"a": 1, "b": 2}, {"a": 1, "b": 3}]
        assert Project.locate(items, a=1, b=3) == {"a": 1, "b": 3}


class TestOrderPreservation:
    def test_set_appends_without_reordering(self) -> None:
        data = {"services": ["publish-on-web", {"keycloak": {"config": {}}}]}
        v = Project(data)
        # Setting a new service config find-or-creates at the end, never reorders.
        v.set("services/redis/config/x", 1)
        names = [s if isinstance(s, str) else next(iter(s)) for s in data["services"]]
        assert names[:2] == ["publish-on-web", "keycloak"]
        assert names[-1] == "redis"


class TestFormAgnosticServiceAccess:
    """RC-5 A2.1: service config read/write works on the new {name/reference, config}
    record and the legacy name-as-key form alike."""

    def test_read_new_record_form(self):
        data = {"services": [{"name": "keycloak", "config": {"template": "sso-only"}}]}
        v = Project(data)
        assert v.uses_service("keycloak")
        assert v.service_config("keycloak") == {"template": "sso-only"}
        assert v.get("services/keycloak/config/template") == "sso-only"

    def test_read_reference_record_form(self):
        data = {"services": [{"reference": "persistent-storage", "config": [{"name": "data"}]}]}
        assert Project(data).service_config("persistent-storage") == [{"name": "data"}]

    def test_read_legacy_form(self):
        data = {"services": [{"keycloak": {"config": {"template": "x"}}}]}
        assert Project(data).get("services/keycloak/config/template") == "x"

    def test_write_into_existing_record_form(self):
        data = {"services": [{"name": "keycloak", "config": {"template": "sso-only"}}]}
        Project(data).set("services/keycloak/config/restrict-access/enabled", True)
        # written into the record's own config, template preserved, no name-as-key key
        entry = data["services"][0]
        assert entry["name"] == "keycloak"
        assert entry["config"]["template"] == "sso-only"
        assert entry["config"]["restrict-access"]["enabled"] is True

    def test_delete_in_record_form(self):
        data = {"services": [{"name": "keycloak", "config": {"template": "x", "banner": "b"}}]}
        Project(data).delete("services/keycloak/config/banner")
        assert data["services"][0]["config"] == {"template": "x"}


class TestGetSummary:
    """Project (aggregate root) produces its lightweight typed projection."""

    def test_produces_summary(self) -> None:
        data = {
            "name": "demo",
            "config": {"api-key": "stored-key"},
            "users": [{"email": "a@b.nl", "role": "admin"}, {"email": "c@d.nl", "role": "developer"}],
        }
        summary = Project(data).get_summary("demo.yaml")
        assert summary is not None
        assert summary.name == "demo"
        assert summary.filename == "demo.yaml"
        assert [u.email for u in summary.users] == ["a@b.nl", "c@d.nl"]

    def test_api_key_returned_as_stored_no_crypto(self) -> None:
        # Plaintext resolution is the caller's concern; get_summary stays pure.
        data = {"name": "demo", "config": {"api-key": "ENC[age-ciphertext]"}}
        assert Project(data).get_summary("f.yaml").api_key == "ENC[age-ciphertext]"

    def test_none_without_name(self) -> None:
        assert Project({"config": {"api-key": "k"}}).get_summary("f.yaml") is None

    def test_none_without_api_key(self) -> None:
        assert Project({"name": "demo"}).get_summary("f.yaml") is None

    def test_users_optional(self) -> None:
        summary = Project({"name": "demo", "config": {"api-key": "k"}}).get_summary("f.yaml")
        assert summary.users is None


class TestServiceGenericAccess:
    def test_service_entry_string_form(self) -> None:
        assert Project(_project()).service_entry("publish-on-web") == "publish-on-web"

    def test_service_entry_dict_form(self) -> None:
        entry = Project(_project()).service_entry("keycloak")
        assert entry == {"keycloak": {"config": {"template": "sso-support"}}}

    def test_service_entry_absent(self) -> None:
        assert Project(_project()).service_entry("redis") is None

    def test_uses_service(self) -> None:
        v = Project(_project())
        assert v.uses_service("publish-on-web")
        assert not v.uses_service("redis")

    def test_service_config_raw(self) -> None:
        assert Project(_project()).service_config("namespace-postgresql-database") == {
            "instances": 2,
            "storage": "5Gi",
        }

    def test_service_config_model_delegates_to_provider(self) -> None:
        model = Project(_project()).service_config_model("keycloak")
        assert model is not None
        assert model.template == "sso-support"

    def test_service_config_model_validates_and_defaults(self) -> None:
        model = Project(_project()).service_config_model("namespace-postgresql-database")
        assert model.instances == 2
        assert model.storage == "5Gi"
        # provider default fills the rest
        assert model.image == "ghcr.io/cloudnative-pg/postgresql:17"

    def test_service_config_model_absent_service_returns_none(self) -> None:
        assert Project(_project()).service_config_model("redis") is None

    def test_service_config_model_unknown_name_returns_none(self) -> None:
        data = {"services": [{"not-a-service": {"config": {}}}]}
        assert Project(data).service_config_model("not-a-service") is None

    def test_service_config_model_service_without_config_raises(self) -> None:
        data = {"services": ["publish-on-web"]}
        with pytest.raises(TypeError):
            Project(data).service_config_model("publish-on-web")

    def test_service_config_model_bad_value_fails_closed(self) -> None:
        data = {"services": [{"namespace-postgresql-database": {"config": {"instances": -1}}}]}
        with pytest.raises(ValidationError):
            Project(data).service_config_model("namespace-postgresql-database")

    def test_service_type_coverage_is_generic(self) -> None:
        # Sanity: the method dispatches by name for every configurable service.
        v = Project(_project())
        for name in ("keycloak", "namespace-postgresql-database"):
            assert ServiceType(name)  # name is a valid service
            assert v.service_config_model(name) is not None
