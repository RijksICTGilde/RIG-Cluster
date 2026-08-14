"""Tests for the unified service-config API surface (RC-12 follow-up).

Two layers are exercised:

* the pure data-manipulation core on ``ServiceAdapter`` (``set_service_config`` /
  ``remove_service_config``) -- no I/O, fast, deterministic; and
* the round-trip guarantee that config written through the core is accepted by the
  same validation chokepoint the save path runs (``validate_service_configs``), and
  that a config the service's model rejects is refused.

The core is where a config block lands in the project YAML at the right target
(project / component / deployment / deployment-component). Everything above it (the
async task, the endpoint) is a thin wrapper over this core, so proving the core here
keeps the endpoint tests small.
"""

from __future__ import annotations

import pytest
from opi.core.project_schema import ProjectIntegrityError
from opi.manager.project_validation import validate_service_configs
from opi.services.catalog.base import ConfigLayer
from opi.services.services import (
    ServiceAdapter,
    ServiceValidationError,
    service_entry_config,
    service_entry_name,
)
from opi.services.services_enums import ServiceType


def _project() -> dict:
    """A minimal but real-shaped project with one component and one deployment."""
    return {
        "schema-version": 2,
        "name": "demo",
        "services": ["publish-on-web"],
        "components": [
            {"name": "backend", "type": "single", "services": ["publish-on-web"]},
        ],
        "deployments": [
            {
                "name": "deployment-1",
                "cluster": "local",
                "namespace": "demo",
                "components": [{"reference": "backend", "services": []}],
            }
        ],
    }


class TestSetServiceConfigProjectLayer:
    def test_appends_name_config_record_when_absent(self) -> None:
        data = _project()
        ServiceAdapter.set_service_config(
            data,
            ServiceType.AUTHORIZATION_WALL.value,
            ConfigLayer.PROJECT,
            {"banner": "Toegang beperkt"},
        )
        entry = next(e for e in data["services"] if service_entry_name(e) == "authorization-wall")
        assert entry == {"name": "authorization-wall", "config": {"banner": "Toegang beperkt"}}

    def test_promotes_bare_string_in_place_without_duplicating(self) -> None:
        data = _project()
        data["services"].append("keycloak")  # bare string selection
        ServiceAdapter.set_service_config(
            data,
            ServiceType.KEYCLOAK.value,
            ConfigLayer.PROJECT,
            {"template": "algoritmeregister"},
        )
        keycloak_entries = [e for e in data["services"] if service_entry_name(e) == "keycloak"]
        assert len(keycloak_entries) == 1
        assert service_entry_config(keycloak_entries[0]) == {"template": "algoritmeregister"}

    def test_replaces_existing_config_not_merges(self) -> None:
        data = _project()
        ServiceAdapter.set_service_config(
            data, ServiceType.KEYCLOAK.value, ConfigLayer.PROJECT, {"template": "old", "variables": {"a": 1}}
        )
        ServiceAdapter.set_service_config(data, ServiceType.KEYCLOAK.value, ConfigLayer.PROJECT, {"template": "new"})
        entry = next(e for e in data["services"] if service_entry_name(e) == "keycloak")
        assert service_entry_config(entry) == {"template": "new"}

    def test_preserves_schema_version_sibling(self) -> None:
        data = _project()
        data["services"].append({"name": "keycloak", "config": {"template": "x"}, "schema-version": "1.0"})
        ServiceAdapter.set_service_config(data, ServiceType.KEYCLOAK.value, ConfigLayer.PROJECT, {"template": "y"})
        entry = next(e for e in data["services"] if service_entry_name(e) == "keycloak")
        assert entry.get("schema-version") == "1.0"
        assert service_entry_config(entry) == {"template": "y"}


class TestSetServiceConfigComponentLayer:
    def test_writes_reference_config_on_named_component(self) -> None:
        data = _project()
        ServiceAdapter.set_service_config(
            data,
            ServiceType.HEALTH_CHECK.value,
            ConfigLayer.COMPONENT,
            {"scheme": "http", "port": 8080, "liveness-path": "/healthz"},
            component_name="backend",
        )
        comp = next(c for c in data["components"] if c["name"] == "backend")
        entry = next(e for e in comp["services"] if service_entry_name(e) == "health-check")
        assert entry["reference"] == "health-check"
        assert service_entry_config(entry)["port"] == 8080

    def test_unknown_component_raises(self) -> None:
        data = _project()
        with pytest.raises(ServiceValidationError):
            ServiceAdapter.set_service_config(
                data, ServiceType.HEALTH_CHECK.value, ConfigLayer.COMPONENT, {"port": 1}, component_name="nope"
            )

    def test_missing_component_name_raises(self) -> None:
        data = _project()
        with pytest.raises(ServiceValidationError):
            ServiceAdapter.set_service_config(data, ServiceType.HEALTH_CHECK.value, ConfigLayer.COMPONENT, {"port": 1})


class TestImplicitProjectSelection:
    """Configuring a service on a component/deployment implicitly selects it at the
    project level, so a component config write does not require the caller to have
    added the service to the root services list first (structural check
    project_validation.py:288). No explicit project-level config is assumed."""

    def test_component_config_adds_bare_project_selection(self) -> None:
        data = _project()  # no health-check anywhere
        ServiceAdapter.set_service_config(
            data, ServiceType.HEALTH_CHECK.value, ConfigLayer.COMPONENT, {"port": 8080}, component_name="backend"
        )
        project_names = ServiceAdapter.extract_service_names_from_project_services(data["services"])
        assert "health-check" in project_names
        # added as a bare selection, not a config record at project level
        entry = next(e for e in data["services"] if service_entry_name(e) == "health-check")
        assert entry == "health-check"

    def test_deployment_config_adds_bare_project_selection(self) -> None:
        data = _project()
        ServiceAdapter.set_service_config(
            data, ServiceType.MINIO_STORAGE.value, ConfigLayer.DEPLOYMENT, {}, deployment_name="deployment-1"
        )
        assert "minio-storage" in ServiceAdapter.extract_service_names_from_project_services(data["services"])

    def test_existing_project_entry_is_not_duplicated_or_demoted(self) -> None:
        data = _project()
        data["services"].append({"name": "minio-storage", "config": {"existing": True}})
        ServiceAdapter.set_service_config(
            data, ServiceType.MINIO_STORAGE.value, ConfigLayer.DEPLOYMENT, {}, deployment_name="deployment-1"
        )
        minio_entries = [e for e in data["services"] if service_entry_name(e) == "minio-storage"]
        assert len(minio_entries) == 1
        assert service_entry_config(minio_entries[0]) == {"existing": True}  # project config preserved

    def test_project_config_does_not_add_a_second_entry(self) -> None:
        data = _project()
        ServiceAdapter.set_service_config(data, ServiceType.KEYCLOAK.value, ConfigLayer.PROJECT, {"template": "x"})
        assert len([e for e in data["services"] if service_entry_name(e) == "keycloak"]) == 1


class TestSetServiceConfigDeploymentLayers:
    def test_deployment_layer_writes_on_named_deployment(self) -> None:
        data = _project()
        ServiceAdapter.set_service_config(
            data,
            ServiceType.MINIO_STORAGE.value,
            ConfigLayer.DEPLOYMENT,
            {"foo": "bar"},
            deployment_name="deployment-1",
        )
        dep = next(d for d in data["deployments"] if d["name"] == "deployment-1")
        entry = next(e for e in dep["services"] if service_entry_name(e) == "minio-storage")
        assert entry["reference"] == "minio-storage"

    def test_deployment_component_layer_writes_on_nested_ref(self) -> None:
        data = _project()
        ServiceAdapter.set_service_config(
            data,
            ServiceType.PERSISTENT_STORAGE.value,
            ConfigLayer.DEPLOYMENT_COMPONENT,
            [{"name": "data", "action": "recreate"}],
            deployment_name="deployment-1",
            component_name="backend",
        )
        dep = next(d for d in data["deployments"] if d["name"] == "deployment-1")
        comp = next(c for c in dep["components"] if service_entry_name(c) == "backend")
        entry = next(e for e in comp["services"] if service_entry_name(e) == "persistent-storage")
        assert service_entry_config(entry) == [{"name": "data", "action": "recreate"}]


class TestUnknownService:
    def test_unknown_service_name_raises(self) -> None:
        data = _project()
        with pytest.raises(ServiceValidationError):
            ServiceAdapter.set_service_config(data, "not-a-service", ConfigLayer.PROJECT, {})


class TestRoundTripThroughValidationChokepoint:
    """Config written by the core must be accepted by the same chokepoint the save
    path runs (``validate_service_configs``), and a config the model rejects must be
    refused -- with the accepted-field list the endpoint surfaces to the client."""

    def test_valid_project_config_passes_validation(self) -> None:
        data = _project()
        ServiceAdapter.set_service_config(
            data,
            ServiceType.KEYCLOAK.value,
            ConfigLayer.PROJECT,
            {"template": "algoritmeregister", "additional_redirect_uris": ["http://localhost:8080/*"]},
        )
        validate_service_configs(data)  # must not raise

    def test_valid_component_config_passes_validation(self) -> None:
        data = _project()
        ServiceAdapter.set_service_config(
            data,
            ServiceType.HEALTH_CHECK.value,
            ConfigLayer.COMPONENT,
            {"scheme": "http", "port": 8080, "liveness-path": "/healthz"},
            component_name="backend",
        )
        validate_service_configs(data)  # must not raise

    def test_unknown_field_is_rejected(self) -> None:
        data = _project()
        ServiceAdapter.set_service_config(
            data, ServiceType.AUTHORIZATION_WALL.value, ConfigLayer.PROJECT, {"banner": "ok", "bogus": 1}
        )
        with pytest.raises(ProjectIntegrityError):
            validate_service_configs(data)

    def test_cross_domain_access_project_and_deployment_round_trip(self) -> None:
        # The service is API-configurable for free at both layers it declares; a config
        # written by the core is accepted by the same chokepoint the save path runs.
        data = _project()
        ServiceAdapter.set_service_config(
            data,
            ServiceType.CROSS_DOMAIN_ACCESS.value,
            ConfigLayer.PROJECT,
            {
                "outbound": [
                    {
                        "name": "naar-api",
                        "from": {"component": "backend"},
                        "to": {"project": "regelrecht", "deployment": "prod", "component": "api", "port": 8080},
                    }
                ]
            },
        )
        ServiceAdapter.set_service_config(
            data,
            ServiceType.CROSS_DOMAIN_ACCESS.value,
            ConfigLayer.DEPLOYMENT,
            {"outbound": [{"name": "naar-api", "to": {"deployment": "dev"}}]},
            deployment_name="deployment-1",
        )
        validate_service_configs(data)  # must not raise

    def test_out_of_range_enum_value_is_rejected(self) -> None:
        data = _project()
        ServiceAdapter.set_service_config(
            data,
            ServiceType.HEALTH_CHECK.value,
            ConfigLayer.COMPONENT,
            {"scheme": "ftp"},  # not in Literal['none','tcp','http','https']
            component_name="backend",
        )
        with pytest.raises(ProjectIntegrityError):
            validate_service_configs(data)


class TestPatchServiceConfigList:
    """The add/remove-by-key patch on list-shaped component configs (RC: vraag 18).

    Storage mounts and attachment couplings change one entry at a time, so a single
    volume or coupling is named instead of the whole list being resent -- the same
    race the ``services`` list had, one level deeper. Remove is by key on the model's
    own ``ITEM_KEY``; add upserts on that key.
    """

    def _backend(self, data: dict) -> dict:
        return next(c for c in data["components"] if c["name"] == "backend")

    def test_add_appends_a_new_entry_and_validates(self) -> None:
        data = _project()
        counts = ServiceAdapter.patch_service_config_list(
            data,
            ServiceType.PERSISTENT_STORAGE.value,
            ConfigLayer.COMPONENT,
            add=[{"name": "data1", "size": "1Gi", "mount-path": "/data1"}],
            remove=[],
            component_name="backend",
        )
        entry = next(e for e in self._backend(data)["services"] if service_entry_name(e) == "persistent-storage")
        assert service_entry_config(entry) == [{"name": "data1", "size": "1Gi", "mount-path": "/data1"}]
        assert counts == {"added": 1, "updated": 0, "removed": 0}
        validate_service_configs(data)  # the merged list passes the save chokepoint

    def test_add_replaces_the_entry_with_the_same_key(self) -> None:
        data = _project()
        self._backend(data)["services"].append(
            {
                "reference": "persistent-storage",
                "config": [
                    {"name": "data1", "size": "1Gi", "mount-path": "/data1"},
                    {"name": "data2", "size": "1Gi", "mount-path": "/data2"},
                ],
            }
        )
        counts = ServiceAdapter.patch_service_config_list(
            data,
            ServiceType.PERSISTENT_STORAGE.value,
            ConfigLayer.COMPONENT,
            add=[{"name": "data2", "size": "2Gi", "mount-path": "/data2"}],
            remove=[],
            component_name="backend",
        )
        entry = next(e for e in self._backend(data)["services"] if service_entry_name(e) == "persistent-storage")
        assert service_entry_config(entry) == [
            {"name": "data1", "size": "1Gi", "mount-path": "/data1"},
            {"name": "data2", "size": "2Gi", "mount-path": "/data2"},
        ]
        assert counts["updated"] == 1
        assert counts["added"] == 0
        validate_service_configs(data)

    def test_remove_drops_only_the_named_entries(self) -> None:
        data = _project()
        self._backend(data)["services"].append(
            {
                "reference": "temp-storage",
                "config": [
                    {"name": "data1", "size": "1Gi", "mount-path": "/data1"},
                    {"name": "data2", "size": "1Gi", "mount-path": "/data2"},
                ],
            }
        )
        counts = ServiceAdapter.patch_service_config_list(
            data,
            ServiceType.TEMP_STORAGE.value,
            ConfigLayer.COMPONENT,
            add=[],
            remove=["data2"],
            component_name="backend",
        )
        entry = next(e for e in self._backend(data)["services"] if service_entry_name(e) == "temp-storage")
        assert service_entry_config(entry) == [{"name": "data1", "size": "1Gi", "mount-path": "/data1"}]
        assert counts == {"added": 0, "updated": 0, "removed": 1}
        validate_service_configs(data)

    def test_remove_an_unknown_key_is_a_no_op(self) -> None:
        data = _project()
        self._backend(data)["services"].append(
            {"reference": "persistent-storage", "config": [{"name": "data1", "size": "1Gi", "mount-path": "/data1"}]}
        )
        counts = ServiceAdapter.patch_service_config_list(
            data,
            ServiceType.PERSISTENT_STORAGE.value,
            ConfigLayer.COMPONENT,
            add=[],
            remove=["no-such-mount"],
            component_name="backend",
        )
        entry = next(e for e in self._backend(data)["services"] if service_entry_name(e) == "persistent-storage")
        assert service_entry_config(entry) == [{"name": "data1", "size": "1Gi", "mount-path": "/data1"}]
        assert counts == {"added": 0, "updated": 0, "removed": 0}

    def test_remove_then_add_replaces_the_entry(self) -> None:
        data = _project()
        self._backend(data)["services"].append(
            {"reference": "persistent-storage", "config": [{"name": "data1", "size": "1Gi", "mount-path": "/data1"}]}
        )
        counts = ServiceAdapter.patch_service_config_list(
            data,
            ServiceType.PERSISTENT_STORAGE.value,
            ConfigLayer.COMPONENT,
            add=[{"name": "data1", "size": "4Gi", "mount-path": "/data1"}],
            remove=["data1"],
            component_name="backend",
        )
        entry = next(e for e in self._backend(data)["services"] if service_entry_name(e) == "persistent-storage")
        assert service_entry_config(entry) == [{"name": "data1", "size": "4Gi", "mount-path": "/data1"}]
        assert counts == {"added": 1, "updated": 0, "removed": 1}

    def test_invalid_entry_is_refused_before_anything_is_written(self) -> None:
        data = _project()
        with pytest.raises(ServiceValidationError):
            ServiceAdapter.patch_service_config_list(
                data,
                ServiceType.PERSISTENT_STORAGE.value,
                ConfigLayer.COMPONENT,
                add=[{"name": "data1", "size": "1Gi"}],  # mount-path missing
                remove=[],
                component_name="backend",
            )
        assert "persistent-storage" not in [service_entry_name(e) for e in self._backend(data)["services"]]
        # a traversal path is refused on the same guard, by the model's own validator
        with pytest.raises(ServiceValidationError):
            ServiceAdapter.patch_service_config_list(
                data,
                ServiceType.PERSISTENT_STORAGE.value,
                ConfigLayer.COMPONENT,
                add=[{"name": "data1", "size": "1Gi", "mount-path": "/../etc"}],
                remove=[],
                component_name="backend",
            )

    def test_attachments_patch_keys_on_the_reference(self) -> None:
        data = _project()
        data["services"].append("attachments")
        counts = ServiceAdapter.patch_service_config_list(
            data,
            ServiceType.ATTACHMENTS.value,
            ConfigLayer.COMPONENT,
            add=[{"reference": "rapport", "provide-as": "env-var", "env-name": "RAPPORT"}],
            remove=[],
            component_name="backend",
        )
        entry = next(e for e in self._backend(data)["services"] if service_entry_name(e) == "attachments")
        assert service_entry_config(entry) == [{"reference": "rapport", "provide-as": "env-var", "env-name": "RAPPORT"}]
        assert counts == {"added": 1, "updated": 0, "removed": 0}

    def test_a_service_without_a_list_model_has_nothing_to_patch(self) -> None:
        data = _project()
        with pytest.raises(ServiceValidationError):
            ServiceAdapter.patch_service_config_list(
                data,
                ServiceType.KEYCLOAK.value,
                ConfigLayer.PROJECT,
                add=[{"template": "x"}],
                remove=[],
            )

    def test_an_unknown_service_is_refused_as_validation(self) -> None:
        data = _project()
        with pytest.raises(ServiceValidationError):
            ServiceAdapter.patch_service_config_list(
                data,
                "not-a-service",
                ConfigLayer.COMPONENT,
                add=[],
                remove=["x"],
                component_name="backend",
            )


class TestPlatformManagedFieldsSurviveAWrite:
    """A user's write may not destroy what the platform wrote into the same block.

    ``keycloak.realms`` holds the host, the realm and the AGE-encrypted password of the
    realm-admin account, and nothing else holds that password. The generic PUT dumps with
    ``exclude_unset=True`` and ``set_service_config`` replaces the whole block, so a
    ``PUT {"template": "sso-only"}`` used to leave exactly that and take the realm admin
    with it -- after which the project wedges on the duplicate-admin guard and the only
    ways back are the git history or an administrator on the master realm.
    """

    def _keycloak_project(self) -> dict:
        data = _project()
        data["services"].append(
            {
                "name": "keycloak",
                "config": {
                    "template": "algoritmeregister",
                    "realms": [
                        {
                            "host": "https://keycloak.example",
                            "realm": "demo-odcn",
                            "username": "realm-admin",
                            "password": "AGE-VERSLEUTELD",
                        }
                    ],
                },
            }
        )
        return data

    def _keycloak_config(self, data: dict) -> dict:
        return service_entry_config(next(e for e in data["services"] if service_entry_name(e) == "keycloak"))

    def test_the_declaration_names_exactly_the_platform_written_field(self) -> None:
        from opi.services.catalog.keycloak.config_model import KeycloakConfig
        from opi.services.config_managed import platform_managed_keys

        assert platform_managed_keys(KeycloakConfig) == frozenset({"realms"})

    def test_a_write_without_realms_keeps_the_realm_admin(self) -> None:
        """What the generic PUT actually sends for a body of {"template": "sso-only"}."""
        from opi.services.catalog.keycloak.config_model import KeycloakConfig

        data = self._keycloak_project()
        body = KeycloakConfig.model_validate({"template": "sso-only"})
        sent = body.model_dump(by_alias=True, exclude_unset=True)
        assert sent == {"template": "sso-only"}  # the caller never mentions realms

        ServiceAdapter.set_service_config(data, ServiceType.KEYCLOAK.value, ConfigLayer.PROJECT, sent)

        config = self._keycloak_config(data)
        assert config["template"] == "sso-only"  # the setting the caller did send lands
        assert config["realms"] == [
            {
                "host": "https://keycloak.example",
                "realm": "demo-odcn",
                "username": "realm-admin",
                "password": "AGE-VERSLEUTELD",
            }
        ]
        validate_service_configs(data)

    def test_a_caller_cannot_replace_the_platform_field_either(self) -> None:
        """Stored wins. Refusing outright would break a replay that resends what is
        already there, and there is no request that may replace this."""
        data = self._keycloak_project()
        ServiceAdapter.set_service_config(
            data,
            ServiceType.KEYCLOAK.value,
            ConfigLayer.PROJECT,
            {
                "template": "sso-only",
                "realms": [{"host": "http://evil", "realm": "x", "username": "u", "password": "p"}],
            },
        )
        assert self._keycloak_config(data)["realms"][0]["password"] == "AGE-VERSLEUTELD"

    def test_clearing_the_config_keeps_the_platform_field_and_drops_the_rest(self) -> None:
        """DELETE means 'reset my settings', not 'throw away the realm-admin password'."""
        data = self._keycloak_project()
        changed = ServiceAdapter.remove_service_config(data, ServiceType.KEYCLOAK.value, ConfigLayer.PROJECT)

        assert changed is True
        config = self._keycloak_config(data)
        assert "template" not in config
        assert config["realms"][0]["password"] == "AGE-VERSLEUTELD"
        validate_service_configs(data)

    def test_a_service_without_platform_fields_still_clears_completely(self) -> None:
        data = _project()
        ServiceAdapter.set_service_config(
            data, ServiceType.AUTHORIZATION_WALL.value, ConfigLayer.PROJECT, {"banner": "Toegang beperkt"}
        )
        assert ServiceAdapter.remove_service_config(data, ServiceType.AUTHORIZATION_WALL.value, ConfigLayer.PROJECT)
        assert "authorization-wall" in [service_entry_name(e) for e in data["services"]]
        assert (
            service_entry_config(next(e for e in data["services"] if service_entry_name(e) == "authorization-wall"))
            is None
        )

    def test_a_first_write_on_a_project_without_the_service_is_unaffected(self) -> None:
        data = _project()
        ServiceAdapter.set_service_config(
            data, ServiceType.KEYCLOAK.value, ConfigLayer.PROJECT, {"template": "sso-only"}
        )
        assert self._keycloak_config(data) == {"template": "sso-only"}

    def test_a_tls_write_keeps_the_domain_approvals_and_their_history(self) -> None:
        """publish-on-web is the second block of this shape: the approval verdicts sit
        next to the inherited user settings tls/attachment. An approval is an approver's
        decision plus an audit trail, so a caller setting tls may not clear it."""
        domains = {
            "allowed-domains": [
                {
                    "domain": "voorbeeld.nl",
                    "status": "approved",
                    "history": [{"date": "2026-08-01", "status": "approved", "by": "beheerder", "message": "akkoord"}],
                }
            ]
        }
        data = _project()
        data["services"] = [{"name": "publish-on-web", "config": {"tls": "cluster", "domains": domains}}]

        ServiceAdapter.set_service_config(
            data, ServiceType.PUBLISH_ON_WEB.value, ConfigLayer.PROJECT, {"tls": "provided", "attachment": "cert"}
        )

        config = service_entry_config(next(e for e in data["services"] if service_entry_name(e) == "publish-on-web"))
        assert config["tls"] == "provided"
        assert config["domains"] == domains
        validate_service_configs(data)


class TestPatchListInsideAnObjectConfig:
    """The same add/remove patch on a list that sits INSIDE a config object.

    ``invite.active``, ``cross-domain-access.inbound``/``outbound`` and
    ``sleep-mode.match`` are lists too, but their config block is an object, so until now
    only the PUT reached them -- and a PUT rewrites the block, so adding one entry meant
    resending every other one. Whoever did not know that wiped the rest, which is exactly
    what happened in practice. These tests hold the two things that must be true: one
    entry changes, and everything around it (the other entries AND the sibling fields)
    stays byte-for-byte as it was.
    """

    def _project_config(self, data: dict, service: str) -> dict:
        entry = next(e for e in data["services"] if service_entry_name(e) == service)
        return service_entry_config(entry)

    def test_a_second_invite_leaves_the_first_and_its_key_alone(self) -> None:
        """Vraag 3: the invite key is deliberately absent from every read response, so a
        PUT-only world could not resend the first invite and a second one cost it."""
        data = _project()
        data["services"].append(
            {
                "name": "invite",
                "config": {
                    "default-language": "en",
                    "active": [{"key": "eerste-geheim", "realm-roles": ["viewer"], "contact-email": "a@b.nl"}],
                },
            }
        )
        counts = ServiceAdapter.patch_service_config_list(
            data,
            ServiceType.INVITE.value,
            ConfigLayer.PROJECT,
            add=[{"key": "tweede-geheim", "realm-roles": ["editor"]}],
            remove=[],
            list_field="active",
        )
        config = self._project_config(data, "invite")
        assert config["active"] == [
            {"key": "eerste-geheim", "realm-roles": ["viewer"], "contact-email": "a@b.nl"},
            {"key": "tweede-geheim", "realm-roles": ["editor"]},
        ]
        assert config["default-language"] == "en"  # the sibling field is not rewritten
        assert counts == {"added": 1, "updated": 0, "removed": 0}
        validate_service_configs(data)

    def test_removing_one_invite_keeps_the_other(self) -> None:
        data = _project()
        data["services"].append(
            {
                "name": "invite",
                "config": {
                    "default-language": "nl",
                    "active": [{"key": "eerste-geheim"}, {"key": "tweede-geheim"}],
                },
            }
        )
        counts = ServiceAdapter.patch_service_config_list(
            data,
            ServiceType.INVITE.value,
            ConfigLayer.PROJECT,
            add=[],
            remove=["eerste-geheim"],
            list_field="active",
        )
        config = self._project_config(data, "invite")
        assert config["active"] == [{"key": "tweede-geheim"}]
        assert config["default-language"] == "nl"
        assert counts == {"added": 0, "updated": 0, "removed": 1}
        validate_service_configs(data)

    def test_cross_domain_inbound_add_leaves_outbound_untouched(self) -> None:
        data = _project()
        data["services"].append(
            {
                "name": "cross-domain-access",
                "config": {
                    "inbound": [{"name": "van-portaal", "from": {"project": "portaal", "component": "web"}, "to": {}}],
                    "outbound": [{"name": "naar-api", "to": {"project": "andere", "component": "api", "port": 8080}}],
                },
            }
        )
        counts = ServiceAdapter.patch_service_config_list(
            data,
            ServiceType.CROSS_DOMAIN_ACCESS.value,
            ConfigLayer.PROJECT,
            add=[{"name": "van-monitor", "from": {"project": "monitor", "component": "scraper"}}],
            remove=[],
            list_field="inbound",
        )
        config = self._project_config(data, "cross-domain-access")
        assert [rule["name"] for rule in config["inbound"]] == ["van-portaal", "van-monitor"]
        assert config["outbound"] == [
            {"name": "naar-api", "to": {"project": "andere", "component": "api", "port": 8080}}
        ]
        assert counts == {"added": 1, "updated": 0, "removed": 0}
        validate_service_configs(data)

    def test_cross_domain_outbound_remove_keeps_the_other_rules(self) -> None:
        data = _project()
        data["services"].append(
            {
                "name": "cross-domain-access",
                "config": {
                    "inbound": [{"name": "van-portaal", "from": {"project": "portaal", "component": "web"}}],
                    "outbound": [
                        {"name": "naar-api", "to": {"project": "andere", "component": "api", "port": 8080}},
                        {"name": "naar-log", "to": {"project": "logboek", "component": "in", "port": 443}},
                    ],
                },
            }
        )
        counts = ServiceAdapter.patch_service_config_list(
            data,
            ServiceType.CROSS_DOMAIN_ACCESS.value,
            ConfigLayer.PROJECT,
            add=[],
            remove=["naar-api"],
            list_field="outbound",
        )
        config = self._project_config(data, "cross-domain-access")
        assert [rule["name"] for rule in config["outbound"]] == ["naar-log"]
        assert [rule["name"] for rule in config["inbound"]] == ["van-portaal"]
        assert counts == {"added": 0, "updated": 0, "removed": 1}
        validate_service_configs(data)

    def test_sleep_mode_match_is_a_set_of_plain_patterns(self) -> None:
        """A match pattern has no key field, so the pattern IS its identity: add is a
        union, and adding one that is already there is neither an append nor an error."""
        data = _project()
        data["services"].append(
            {"name": "sleep-mode", "config": {"enabled": True, "match": ["acc-*"], "sleep-after-deploy": "12h"}}
        )
        counts = ServiceAdapter.patch_service_config_list(
            data,
            ServiceType.SLEEP_MODE.value,
            ConfigLayer.PROJECT,
            add=["test-*", "acc-*"],
            remove=[],
            list_field="match",
        )
        config = self._project_config(data, "sleep-mode")
        assert config["match"] == ["acc-*", "test-*"]
        assert config["sleep-after-deploy"] == "12h"  # the sibling fields are not rewritten
        assert config["enabled"] is True
        assert counts == {"added": 1, "updated": 1, "removed": 0}
        validate_service_configs(data)

    def test_sleep_mode_match_remove_drops_only_that_pattern(self) -> None:
        data = _project()
        data["services"].append(
            {"name": "sleep-mode", "config": {"enabled": True, "match": ["acc-*", "test-*"], "wake-mode": "manual"}}
        )
        counts = ServiceAdapter.patch_service_config_list(
            data,
            ServiceType.SLEEP_MODE.value,
            ConfigLayer.PROJECT,
            add=[],
            remove=["acc-*"],
            list_field="match",
        )
        config = self._project_config(data, "sleep-mode")
        assert config["match"] == ["test-*"]
        assert config["wake-mode"] == "manual"
        assert counts == {"added": 0, "updated": 0, "removed": 1}
        validate_service_configs(data)

    def test_an_invalid_pattern_is_refused_by_the_owning_model(self) -> None:
        """A plain-value list has no item model to validate against, so the check has to
        come from the model that owns the list -- here sleep-mode's own match validator."""
        data = _project()
        data["services"].append({"name": "sleep-mode", "config": {"enabled": True, "match": ["acc-*"]}})
        with pytest.raises(ServiceValidationError):
            ServiceAdapter.patch_service_config_list(
                data,
                ServiceType.SLEEP_MODE.value,
                ConfigLayer.PROJECT,
                add=["ac*-*"],  # a '*' in the middle is refused
                remove=[],
                list_field="match",
            )
        assert self._project_config(data, "sleep-mode")["match"] == ["acc-*"]

    def test_patching_a_list_the_service_does_not_have_is_refused(self) -> None:
        data = _project()
        with pytest.raises(ServiceValidationError):
            ServiceAdapter.patch_service_config_list(
                data,
                ServiceType.INVITE.value,
                ConfigLayer.PROJECT,
                add=[{"key": "x"}],
                remove=[],
                list_field="niet-bestaand",
            )

    def test_patching_the_block_as_a_whole_is_still_refused(self) -> None:
        """The root-list patch and the named-list patch are not interchangeable: invite's
        config is an object, so there is nothing to patch without naming a list."""
        data = _project()
        with pytest.raises(ServiceValidationError):
            ServiceAdapter.patch_service_config_list(
                data,
                ServiceType.INVITE.value,
                ConfigLayer.PROJECT,
                add=[{"key": "x"}],
                remove=[],
            )


class TestPortRangeExcludesPrivilegedPorts:
    """Probe/scrape ports must be non-privileged (>=1024): images run non-root, so a
    port below 1024 can never be bound or reached. The constraint lives in the config
    model (the API body's validation source), mirrored by the editable."""

    def test_health_check_rejects_privileged_port(self) -> None:
        from opi.services.catalog.health_check.config_model import HealthCheckConfig
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            HealthCheckConfig.model_validate({"port": 1000})
        HealthCheckConfig.model_validate({"port": 8080})  # non-privileged is fine

    def test_metrics_scraper_rejects_privileged_port(self) -> None:
        from opi.services.catalog.metrics_scraper.config_model import MetricsScraperConfig
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            MetricsScraperConfig.model_validate({"port": 1000})
        MetricsScraperConfig.model_validate({"port": 8080})


class TestRemoveServiceConfig:
    def test_demotes_record_to_bare_string_and_reports_true(self) -> None:
        data = _project()
        ServiceAdapter.set_service_config(
            data, ServiceType.AUTHORIZATION_WALL.value, ConfigLayer.PROJECT, {"banner": "x"}
        )
        removed = ServiceAdapter.remove_service_config(data, ServiceType.AUTHORIZATION_WALL.value, ConfigLayer.PROJECT)
        assert removed is True
        entry = next(e for e in data["services"] if service_entry_name(e) == "authorization-wall")
        assert entry == "authorization-wall"  # still selected, config gone

    def test_absent_service_reports_false(self) -> None:
        data = _project()
        removed = ServiceAdapter.remove_service_config(data, ServiceType.KEYCLOAK.value, ConfigLayer.PROJECT)
        assert removed is False


class TestEndpointHelpers:
    """The registry-driven helpers behind the unified endpoint."""

    def test_supported_targets_derives_from_config_api_fields(self) -> None:
        from opi.api.v2.router import _supported_targets
        from opi.services.registry import get_service

        # keycloak declares config at the project layer only.
        keycloak = get_service(ServiceType.KEYCLOAK)
        assert _supported_targets(keycloak) == ["project"]
        # health-check declares config at the component layer only.
        health = get_service(ServiceType.HEALTH_CHECK)
        assert _supported_targets(health) == ["component"]

    def test_collect_service_config_reports_target_and_config(self) -> None:
        from opi.api.v2.router import _collect_service_config

        data = _project()
        ServiceAdapter.set_service_config(data, ServiceType.KEYCLOAK.value, ConfigLayer.PROJECT, {"template": "algor"})
        ServiceAdapter.set_service_config(
            data, ServiceType.HEALTH_CHECK.value, ConfigLayer.COMPONENT, {"port": 8080}, component_name="backend"
        )
        keycloak_cfg = _collect_service_config(data, "keycloak", target_filter=None)
        assert keycloak_cfg == [{"target": "project", "config": {"template": "algor"}}]

        health_cfg = _collect_service_config(data, "health-check", target_filter=None)
        assert health_cfg == [{"target": "component", "component": "backend", "config": {"port": 8080}}]

    def test_collect_service_config_respects_target_filter(self) -> None:
        from opi.api.v2.router import _collect_service_config

        data = _project()
        ServiceAdapter.set_service_config(
            data, ServiceType.HEALTH_CHECK.value, ConfigLayer.COMPONENT, {"port": 8080}, component_name="backend"
        )
        assert _collect_service_config(data, "health-check", target_filter="project") == []
        assert len(_collect_service_config(data, "health-check", target_filter="component")) == 1


#: Measured API-config coverage: every service and the config targets it exposes.
#: A snapshot guard -- if a service's config declarations change, this fails loudly
#: so the API-configurability of the catalog stays a conscious, reviewed fact.
#: ``namespace-redis``/``platform`` carry no config by design; ``postgresql-database``
#: has a config model but declares it on no layer (a known gap, see the review report).
EXPECTED_API_TARGETS: dict[str, list[str]] = {
    # The two property-owning system services expose no config route: their data is a
    # plain component property, not a block in a services list (RC-25).
    "aliases": [],
    "user-env-vars": [],
    "attachments": ["component"],
    "authorization-wall": ["project"],
    "cross-domain-access": ["project", "deployment"],
    # The health judgement takes no user config; it reads the services' own state (RC-28).
    "deployment-health": [],
    "health-check": ["component"],
    "invite": ["project"],
    "keycloak": ["project"],
    "metrics-scraper": ["component"],
    "minio-storage": ["project", "deployment"],
    "namespace-postgresql-database": ["project"],
    "namespace-redis": [],
    "persistent-storage": ["component"],
    "platform": [],
    "postgresql-database": ["project"],
    # RC-60: the web address is deployment-level config of this service. RC-78: the
    # certificate is overridable per component per deployment, so that layer is now an
    # API target too -- the model always accepted it, only the declaration was missing.
    "publish-on-web": ["component", "deployment", "deployment-component"],
    "redis": ["project"],
    "resource-tuning": [],
    "sleep-mode": ["project"],
    "temp-storage": ["component"],
}


class TestApiConfigCoverage:
    """Every service that carries user config is reachable via the unified endpoint
    at exactly the target(s) it declares -- measured, not assumed."""

    def test_supported_targets_match_measured_reality(self) -> None:
        from opi.api.v2.router import _supported_targets
        from opi.services.registry import SERVICES

        actual = {st.value: _supported_targets(svc) for st, svc in SERVICES.items()}
        assert actual == EXPECTED_API_TARGETS

    def test_every_config_carrying_service_is_reachable(self) -> None:
        from opi.api.v2.router import _supported_targets
        from opi.services.registry import SERVICES

        # A service that carries config editables or a config schema on some layer
        # must expose at least one API target -- otherwise its config is UI-only.
        for st, svc in SERVICES.items():
            if svc.owned_property is not None:
                continue  # owns a plain component property, not a services-list block
            has_component_layout = bool(svc.config_component_layout())
            declares_config = any(svc.config_editables(layer) or svc.config_api_fields(layer) for layer in ConfigLayer)
            if declares_config or has_component_layout:
                assert _supported_targets(svc), f"{st.value} carries config but exposes no API target"

    def test_every_supported_target_actually_has_a_route(self) -> None:
        # The pairing RC-38 makes one derivation: a target the API says it supports must
        # have a generated write route, and a generated route must be on a supported
        # target. They used to be two derivations (_accepts_config_at re-deriving the
        # form hooks, the registration loop separately requiring a model), so a service
        # with editables but no model at a layer could be advertised and not exist.
        from opi.api.v2.router import _CONFIG_WRITE_LAYERS, _supported_targets
        from opi.server import app
        from opi.services.registry import SERVICES

        routed: dict[str, set[str]] = {st.value: set() for st in SERVICES}
        for route in app.routes:
            path = getattr(route, "path", "")
            marker = "/services/"
            if not path.startswith("/api/v2/projects/") or marker not in path or "/config/" not in path:
                continue
            service_name, _, rest = path.split(marker, 1)[1].partition("/config/")
            if service_name in routed:
                routed[service_name].add(rest.split("/")[0])

        for service_type, service in SERVICES.items():
            advertised = {t for t in _supported_targets(service) if ConfigLayer(t) in _CONFIG_WRITE_LAYERS}
            assert routed[service_type.value] == advertised, (
                f"'{service_type.value}': routes {sorted(routed[service_type.value])} do not match the "
                f"advertised targets {sorted(advertised)}"
            )
