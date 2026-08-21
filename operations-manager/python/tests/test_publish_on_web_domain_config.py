"""The web address belongs to the service: one read path, one write path (RC-60).

``domain_config`` is the single authority on WHERE a deployment's seven web-address
settings live, exactly as ``connectors/subdomain.py`` is for the project-level ``domains``
block. These tests hold it to the two properties that make the relocation safe:

* readers accept both locations, so a file that has not been migrated yet keeps working;
* writers only ever land on the service path AND clear the root copy, so the state cannot
  split into two values that disagree.
"""

import pytest
from opi.services.catalog.publish_on_web.domain_config import (
    DOMAIN_SETTING_KEYS,
    DomainSetting,
    ensure_domain_config,
    get_domain_config,
    get_domain_setting,
    get_domain_settings,
    has_domain_setting,
    pop_domain_setting,
    relocate_domain_settings,
    set_domain_setting,
)


def _legacy_deployment() -> dict:
    """A deployment as it was written before v2.7: everything loose in the root."""
    return {
        "name": "productie",
        "cluster": "odcn-production",
        "namespace": "rig-prd-x",
        "base-domain": "rijksapp.nl",
        "subdomain": "wies",
        "domain-format": "component-deployment-project",
        "issuer": "letsencrypt",
        "root-component": "frontend",
        "expose-component-on-bare-domain": "frontend",
    }


def _migrated_deployment() -> dict:
    """The same deployment in the target shape."""
    return {
        "name": "productie",
        "cluster": "odcn-production",
        "namespace": "rig-prd-x",
        "services": [
            {
                "reference": "publish-on-web",
                "config": {
                    "base-domain": "rijksapp.nl",
                    "subdomain": "wies",
                    "domain-format": "component-deployment-project",
                    "issuer": "letsencrypt",
                    "root-component": "frontend",
                    "expose-component-on-bare-domain": "frontend",
                },
            }
        ],
    }


class TestTheSettingSet:
    def test_six_settings_no_more_no_less(self) -> None:
        # Six since v2.8 retired domain-mode; a field added or dropped without thought
        # shows up here.
        assert DOMAIN_SETTING_KEYS == (
            "base-domain",
            "subdomain",
            "domain-format",
            "issuer",
            "root-component",
            "expose-component-on-bare-domain",
        )

    def test_the_enum_values_are_the_on_disk_keys(self) -> None:
        # The relocation moves the values, not their spelling.
        assert DomainSetting.BARE_DOMAIN_COMPONENT.value == "expose-component-on-bare-domain"


class TestReadingAcceptsBothLocations:
    def test_reads_the_service_path(self) -> None:
        dep = _migrated_deployment()
        assert get_domain_setting(dep, DomainSetting.BASE_DOMAIN) == "rijksapp.nl"
        assert get_domain_setting(dep, DomainSetting.SUBDOMAIN) == "wies"

    def test_reads_the_deployment_root_as_fallback(self) -> None:
        dep = _legacy_deployment()
        assert get_domain_setting(dep, DomainSetting.BASE_DOMAIN) == "rijksapp.nl"
        assert get_domain_setting(dep, DomainSetting.DOMAIN_FORMAT) == "component-deployment-project"

    def test_both_locations_read_the_same_seven_values(self) -> None:
        # The relocation is not a behaviour change: same file, same answers.
        assert get_domain_settings(_legacy_deployment()) == get_domain_settings(_migrated_deployment())

    def test_the_service_path_wins_over_a_stale_root_copy(self) -> None:
        dep = _migrated_deployment()
        dep["base-domain"] = "stale.example.org"
        assert get_domain_setting(dep, DomainSetting.BASE_DOMAIN) == "rijksapp.nl"

    def test_an_explicit_null_is_not_a_missing_value(self) -> None:
        # A stored null means "no domain of its own"; falling through to the root for it
        # would make the two storage locations answer differently.
        dep = _migrated_deployment()
        dep["services"][0]["config"]["base-domain"] = None
        dep["base-domain"] = "leftover.example.org"
        assert get_domain_setting(dep, DomainSetting.BASE_DOMAIN) is None

    def test_absent_everywhere_gives_the_default(self) -> None:
        dep = {"name": "productie"}
        assert get_domain_setting(dep, DomainSetting.BARE_DOMAIN_COMPONENT, False) is False
        assert get_domain_setting(dep, DomainSetting.SUBDOMAIN) is None

    def test_reading_never_mutates(self) -> None:
        dep = _legacy_deployment()
        before = dict(dep)
        get_domain_settings(dep)
        assert dep == before
        assert get_domain_config(dep) is None

    @pytest.mark.parametrize(
        "entry",
        [
            "publish-on-web",
            {"reference": "publish-on-web", "config": {"subdomain": "wies"}},
            {"name": "publish-on-web", "config": {"subdomain": "wies"}},
            {"publish-on-web": {"config": {"subdomain": "wies"}}},
        ],
        ids=["bare-string", "reference-record", "name-record", "legacy-nested"],
    )
    def test_every_service_entry_form_is_understood(self, entry) -> None:
        # Three record forms live in production files; matching only ``reference`` is how
        # the deployment clone-state lookup silently missed the others (checklist 5).
        dep = {"name": "productie", "services": [entry], "subdomain": "root-fallback"}
        expected = "root-fallback" if entry == "publish-on-web" else "wies"
        assert get_domain_setting(dep, DomainSetting.SUBDOMAIN) == expected


class TestPresenceIsNotTruth:
    """``has_domain_setting`` answers "is it configured", not "does it have a value".

    The backup-restore clone only fills in a subdomain when the caller did not configure
    one. Before the relocation it asked ``"subdomain" not in new_deployment``, so an
    explicitly stored ``subdomain: null`` counted as configured and was left alone. Asking
    ``get_domain_setting(...) is None`` instead would silently overwrite that decision, so
    the presence question got its own accessor (RC-60 review, suggestion 3).
    """

    def test_an_explicit_null_counts_as_configured(self) -> None:
        dep = {"name": "productie", "services": [{"reference": "publish-on-web", "config": {"subdomain": None}}]}
        assert has_domain_setting(dep, DomainSetting.SUBDOMAIN) is True
        assert get_domain_setting(dep, DomainSetting.SUBDOMAIN) is None

    def test_an_explicit_null_in_the_root_counts_too(self) -> None:
        dep = {"name": "productie", "subdomain": None}
        assert has_domain_setting(dep, DomainSetting.SUBDOMAIN) is True

    def test_absent_is_absent(self) -> None:
        assert has_domain_setting({"name": "productie"}, DomainSetting.SUBDOMAIN) is False
        assert has_domain_setting({"name": "productie", "services": ["publish-on-web"]}, DomainSetting.ISSUER) is False

    def test_a_configured_value_is_present(self) -> None:
        assert has_domain_setting(_migrated_deployment(), DomainSetting.SUBDOMAIN) is True
        assert has_domain_setting(_legacy_deployment(), DomainSetting.SUBDOMAIN) is True


class TestWritingLandsOnTheServicePath:
    def test_writes_into_a_new_service_entry(self) -> None:
        dep = {"name": "productie"}
        set_domain_setting(dep, DomainSetting.DOMAIN_FORMAT, "component-deployment-project")
        assert dep["services"] == [
            {"reference": "publish-on-web", "config": {"domain-format": "component-deployment-project"}}
        ]
        assert "domain-format" not in dep

    def test_promotes_a_bare_string_reference_in_place(self) -> None:
        dep = {"name": "productie", "services": ["postgresql-database", "publish-on-web"]}
        set_domain_setting(dep, DomainSetting.SUBDOMAIN, "wies")
        assert dep["services"][0] == "postgresql-database"
        assert dep["services"][1] == {"reference": "publish-on-web", "config": {"subdomain": "wies"}}

    def test_writes_into_an_existing_record_without_touching_its_siblings(self) -> None:
        dep = _migrated_deployment()
        set_domain_setting(dep, DomainSetting.ISSUER, "eigen-issuer")
        config = dep["services"][0]["config"]
        assert config["issuer"] == "eigen-issuer"
        assert config["subdomain"] == "wies"
        assert len(dep["services"]) == 1

    def test_ensure_absorbs_root_settings_and_removes_them(self) -> None:
        dep = _legacy_deployment()
        config = ensure_domain_config(dep)
        assert config == _migrated_deployment()["services"][0]["config"]
        for key in DOMAIN_SETTING_KEYS:
            assert key not in dep

    def test_ensure_does_not_overwrite_an_already_relocated_value(self) -> None:
        dep = _migrated_deployment()
        dep["base-domain"] = "stale.example.org"
        config = ensure_domain_config(dep)
        assert config["base-domain"] == "rijksapp.nl"
        assert "base-domain" not in dep

    def test_ensure_is_idempotent(self) -> None:
        dep = _legacy_deployment()
        ensure_domain_config(dep)
        once = {"deployment": dict(dep), "config": dict(get_domain_config(dep) or {})}
        ensure_domain_config(dep)
        assert dep == once["deployment"]
        assert get_domain_config(dep) == once["config"]

    def test_ensure_repairs_an_unaddressable_body(self) -> None:
        dep = {"name": "productie", "services": [{"publish-on-web": None}], "subdomain": "wies"}
        config = ensure_domain_config(dep)
        assert config == {"subdomain": "wies"}
        assert get_domain_setting(dep, DomainSetting.SUBDOMAIN) == "wies"

    def test_popping_clears_both_locations(self) -> None:
        # Leaving the root copy behind would resurrect the deleted value on the next read.
        dep = _migrated_deployment()
        dep["subdomain"] = "leftover"
        pop_domain_setting(dep, DomainSetting.SUBDOMAIN)
        assert get_domain_setting(dep, DomainSetting.SUBDOMAIN) is None
        assert "subdomain" not in dep

    def test_popping_an_absent_setting_is_a_no_op(self) -> None:
        dep = {"name": "productie"}
        pop_domain_setting(dep, DomainSetting.ISSUER)
        assert dep == {"name": "productie"}


class TestRelocatePrimitive:
    def test_relocates_and_reports_it(self) -> None:
        dep = _legacy_deployment()
        assert relocate_domain_settings(dep) is True
        assert get_domain_config(dep) == _migrated_deployment()["services"][0]["config"]

    def test_an_already_migrated_deployment_is_untouched(self) -> None:
        dep = _migrated_deployment()
        assert relocate_domain_settings(dep) is False
        assert dep == _migrated_deployment()

    def test_a_deployment_without_a_web_address_gets_no_empty_entry(self) -> None:
        # A deployment that never had a web address must not grow a publish-on-web entry:
        # that would read as "this deployment uses the service" in every services overview.
        dep = {"name": "productie", "cluster": "local", "namespace": "x"}
        assert relocate_domain_settings(dep) is False
        assert "services" not in dep


class TestTheWizardsVirtualRoot:
    """Mid-wizard the same config sits under ``_services-config`` (RC-60).

    Every enforcer, condition and generator that decides what the domain step shows reads
    the deployment dict straight out of the form value. If the readers only knew
    ``services``, all of them would see None while the user is still filling the form in,
    and each field would silently fall back to its own default.
    """

    def test_reads_the_virtual_root(self) -> None:
        from opi.forms.editables.editable import SERVICE_VIRTUALIZE

        virtual = SERVICE_VIRTUALIZE[1]
        dep = {"name": "productie", virtual: [{"reference": "publish-on-web", "config": {"subdomain": "wies"}}]}
        assert get_domain_setting(dep, DomainSetting.SUBDOMAIN) == "wies"

    def test_the_real_root_is_preferred_over_the_virtual_one(self) -> None:
        from opi.forms.editables.editable import SERVICE_VIRTUALIZE

        real, virtual = SERVICE_VIRTUALIZE
        dep = {
            "name": "productie",
            real: [{"reference": "publish-on-web", "config": {"subdomain": "saved"}}],
            virtual: [{"reference": "publish-on-web", "config": {"subdomain": "posted"}}],
        }
        assert get_domain_setting(dep, DomainSetting.SUBDOMAIN) == "saved"

    def test_a_write_goes_to_the_root_that_already_holds_the_entry(self) -> None:
        from opi.forms.editables.editable import SERVICE_VIRTUALIZE

        virtual = SERVICE_VIRTUALIZE[1]
        dep = {"name": "productie", virtual: [{"reference": "publish-on-web", "config": {}}]}
        set_domain_setting(dep, DomainSetting.ISSUER, "letsencrypt")
        assert dep[virtual][0]["config"] == {"issuer": "letsencrypt"}
        assert "services" not in dep

    def test_a_write_without_an_entry_lands_on_the_real_root(self) -> None:
        dep = {"name": "productie"}
        set_domain_setting(dep, DomainSetting.ISSUER, "letsencrypt")
        assert "services" in dep


def test_the_two_service_root_declarations_agree() -> None:
    """``domain_config`` keeps its own copy of the virtual-root pair; it must not drift.

    Importing anything under ``opi.forms`` from this module is a cycle (``forms/__init__``
    imports the renderer, which reads settings through it), so the pair is spelled out
    there instead of imported. That is exactly the kind of second copy that silently turns
    virtualization off, so the equality is a test rather than a comment.
    """
    from opi.forms.editables.editable import SERVICE_VIRTUALIZE
    from opi.services.catalog.publish_on_web.domain_config import _SERVICE_ROOTS

    assert _SERVICE_ROOTS == SERVICE_VIRTUALIZE
