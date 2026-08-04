"""env-vars and aliases as system services (RC-25).

They were the two things that behaved exactly like a service config -- two layers with a
merge between them, AGE-encrypted values, a place in the UI, a need for validation -- but
sat outside the model as bare schema properties. Modelling them adds the config model,
the schema fragment, the validator and the per-layer form sections; it moves no data, so
the yaml paths below are the paths every existing project file already uses.
"""

from __future__ import annotations

import pytest
from opi.core.project_schema import ProjectIntegrityError
from opi.manager.project_validation import validate_service_configs
from opi.services.catalog.aliases.config_model import AliasesConfig
from opi.services.catalog.aliases.editables import AliasMapValidator
from opi.services.catalog.base import ConfigLayer
from opi.services.catalog.user_env_vars.config_model import UserEnvVarsConfig
from opi.services.registry import SERVICES, property_owning_services
from opi.services.services_enums import ServiceKind, ServiceType
from pydantic import ValidationError

ENV_VARS = SERVICES[ServiceType.USER_ENV_VARS]
ALIASES = SERVICES[ServiceType.ALIASES]

AGE_BLOCK = "-----BEGIN AGE ENCRYPTED FILE-----\nYWJj\n-----END AGE ENCRYPTED FILE-----"


class TestSystemServiceShape:
    @pytest.mark.parametrize("service", [ENV_VARS, ALIASES], ids=["user-env-vars", "aliases"])
    def test_is_a_system_service(self, service) -> None:
        # A user must never have to tick these on: every component has them.
        assert service.definition.kind is ServiceKind.SYSTEM

    @pytest.mark.parametrize("service", [ENV_VARS, ALIASES], ids=["user-env-vars", "aliases"])
    def test_never_offered_in_the_picker(self, service) -> None:
        from opi.forms.visualizers.providers import ServiceOptionsProvider

        offered = {option["value"] for option in ServiceOptionsProvider().get_options()}
        assert service.service_type.value not in offered

    @pytest.mark.parametrize("service", [ENV_VARS, ALIASES], ids=["user-env-vars", "aliases"])
    def test_applies_to_every_project_without_being_listed(self, service) -> None:
        assert service.applies_to({"services": [], "components": []}, "any-deployment") is True

    @pytest.mark.parametrize("service", [ENV_VARS, ALIASES], ids=["user-env-vars", "aliases"])
    def test_owns_a_plain_component_property(self, service) -> None:
        assert service.owned_property == service.service_type.value
        assert service in property_owning_services()


class TestLayersAndPaths:
    def test_env_vars_live_on_both_component_layers(self) -> None:
        assert ENV_VARS.config_layers() == [ConfigLayer.COMPONENT, ConfigLayer.DEPLOYMENT_COMPONENT]

    def test_aliases_live_on_the_component_only(self) -> None:
        assert ALIASES.config_layers() == [ConfigLayer.COMPONENT]

    def test_yaml_paths_are_unchanged_by_the_service_model(self) -> None:
        # The whole point: modelling them as a service must not relocate any data.
        assert [e.yaml_path for e in ENV_VARS.config_editables(ConfigLayer.COMPONENT)] == [
            "components[*]/user-env-vars"
        ]
        assert [e.yaml_path for e in ENV_VARS.config_editables(ConfigLayer.DEPLOYMENT_COMPONENT)] == [
            "deployments[*]/components[*]/user-env-vars"
        ]
        assert [e.yaml_path for e in ALIASES.config_editables(ConfigLayer.COMPONENT)] == ["components[*]/aliases"]

    def test_both_layers_have_a_form_section(self) -> None:
        assert ENV_VARS.config_form_section(ConfigLayer.COMPONENT) is not None
        assert ENV_VARS.config_form_section(ConfigLayer.DEPLOYMENT_COMPONENT) is not None
        assert ALIASES.config_form_section(ConfigLayer.COMPONENT) is not None

    def test_system_service_fieldsets_are_unconditional(self) -> None:
        # A user service's fieldset hides until the service is ticked; a system service's
        # must not, or it would never show at all.
        for service in (ENV_VARS, ALIASES):
            for node in service.config_component_layout():
                assert getattr(node, "depends_on", None) is None


class TestUserEnvVarsConfigModel:
    def test_accepts_key_value_text(self) -> None:
        assert UserEnvVarsConfig.model_validate("API_KEY=secret\nDEBUG=true").root

    def test_accepts_yaml_text(self) -> None:
        assert UserEnvVarsConfig.model_validate("API_KEY: secret\nPORT: 8080").root

    def test_accepts_an_age_block_without_looking_inside(self) -> None:
        assert UserEnvVarsConfig.model_validate(AGE_BLOCK).root == AGE_BLOCK

    def test_accepts_the_legacy_mapping_shape(self) -> None:
        assert UserEnvVarsConfig.model_validate({"API_KEY": "secret"}).root == {"API_KEY": "secret"}

    def test_the_stored_encrypted_shape_is_a_block_not_a_prefix(self) -> None:
        # user-env-vars is stored as an AGE block. The single-line `base64+age:` form is
        # what passwords elsewhere in a project file use; it is not a shape this field
        # takes, so accepting it here would make the model lie about the format.
        with pytest.raises(ValidationError):
            UserEnvVarsConfig.model_validate("base64+age:UExBQ0VIT0xERVJfRU5WX1ZBUlM=")

    def test_rejects_a_line_that_is_neither_format(self) -> None:
        with pytest.raises(ValidationError):
            UserEnvVarsConfig.model_validate("this is not a variable")

    def test_rejects_an_invalid_variable_name(self) -> None:
        with pytest.raises(ValidationError):
            UserEnvVarsConfig.model_validate("9INVALID=x")

    def test_a_dollar_in_a_password_is_fine(self) -> None:
        # substitute_known_variables is lenient for exactly this reason; the model must
        # not be stricter than the deploy path.
        assert UserEnvVarsConfig.model_validate("DB_PASSWORD=pa$$wL7nQr4").root


class TestAliasesConfigModel:
    def test_accepts_a_reference_map(self) -> None:
        assert AliasesConfig.model_validate({"POSTGRES_HOST": "$DATABASE_SERVER_HOST"}).root

    def test_accepts_an_encrypted_value(self) -> None:
        assert AliasesConfig.model_validate({"POSTGRES_HOST": AGE_BLOCK}).root

    def test_rejects_an_invalid_alias_name(self) -> None:
        with pytest.raises(ValidationError, match="Ongeldige aliasnaam"):
            AliasesConfig.model_validate({"POSTGRES-HOST": "$DATABASE_SERVER_HOST"})

    def test_a_constant_value_is_not_a_model_level_error(self) -> None:
        # Deliberate: an already-stored constant deploys fine, so rejecting it at file
        # level would break working projects. The form validator is where it is caught.
        assert AliasesConfig.model_validate({"MODE": "production"}).root


class TestAliasMapValidator:
    def test_accepts_an_alias_with_a_reference(self) -> None:
        assert AliasMapValidator().validate("POSTGRES_HOST=$DATABASE_SERVER_HOST") == []

    def test_accepts_the_braced_form(self) -> None:
        assert AliasMapValidator().validate("URL=${PUBLIC_HOST}/api") == []

    def test_rejects_an_alias_without_a_reference(self) -> None:
        messages = AliasMapValidator().validate("MODE=production")
        assert messages and "MODE" in messages[0]

    def test_names_every_offending_alias(self) -> None:
        messages = AliasMapValidator().validate("A=$HOST\nB=one\nC=two")
        assert "B" in messages[0] and "C" in messages[0] and "A" not in messages[0].split(":")[1].split(".")[0]

    def test_reports_a_parse_error_once(self) -> None:
        assert len(AliasMapValidator().validate("not an alias line")) == 1

    def test_says_nothing_about_an_encrypted_value(self) -> None:
        assert AliasMapValidator().validate(AGE_BLOCK) == []

    def test_says_nothing_about_an_empty_field(self) -> None:
        assert AliasMapValidator().validate("") == []


def _project(**component_extra) -> dict:
    return {
        "name": "proj",
        "components": [{"name": "web", **component_extra}],
        "deployments": [{"name": "prd", "components": [{"reference": "web"}]}],
    }


class TestOwnedPropertyValidationAtSaveTime:
    """The gap that made these worth modelling: nothing validated them on the way in."""

    def test_a_valid_project_passes(self) -> None:
        validate_service_configs(_project(**{"user-env-vars": "API_KEY=x", "aliases": {"H": "$PUBLIC_HOST"}}))

    def test_encrypted_values_pass(self) -> None:
        validate_service_configs(_project(**{"user-env-vars": AGE_BLOCK}))

    def test_a_broken_env_var_block_is_rejected(self) -> None:
        with pytest.raises(ProjectIntegrityError, match="user-env-vars"):
            validate_service_configs(_project(**{"user-env-vars": "9BAD=x"}))

    def test_a_broken_alias_name_is_rejected(self) -> None:
        with pytest.raises(ProjectIntegrityError, match="aliases"):
            validate_service_configs(_project(aliases={"BAD-NAME": "$PUBLIC_HOST"}))

    def test_the_deployment_component_layer_is_walked_too(self) -> None:
        data = _project()
        data["deployments"][0]["components"][0]["user-env-vars"] = "9BAD=x"
        with pytest.raises(ProjectIntegrityError, match="deployment 'prd'"):
            validate_service_configs(data)

    def test_absent_properties_are_not_an_error(self) -> None:
        validate_service_configs(_project())


class TestTheConverterNeverLogsAValue:
    """user-env-vars and aliases hold secrets; the deploy path says so in as many words
    ("Never log the values", project_manager.py) but the form converter logged both the
    input and the result at INFO on every read and write. Found while modelling them."""

    def test_read_does_not_log_the_value(self, caplog) -> None:
        from opi.forms.editables.converters import KeyValueConverter

        with caplog.at_level("DEBUG", logger="opi.forms.editables.converters"):
            KeyValueConverter(fmt="env").read({"API_KEY": "s3cr3t-value"})
        assert "s3cr3t-value" not in caplog.text

    def test_write_logs_neither_input_nor_result(self, caplog) -> None:
        from opi.forms.editables.converters import KeyValueConverter

        with caplog.at_level("DEBUG", logger="opi.forms.editables.converters"):
            KeyValueConverter(fmt="env", write_as="string").write("API_KEY=s3cr3t-value")
        assert "s3cr3t-value" not in caplog.text
