"""Where a component is used, and how a reference to it is taken away again (RC-73).

The foundation under deleting a component. Before a definition may leave the project file,
one question has to have exactly one answer: which places name this component. It is asked
from three sides -- the confirmation dialog, the API delete and the delete guard itself --
so the walk produces records and every reader derives its own phrasing from those records
instead of walking the file again.

Two properties are measured:

* the walk finds a reference *everywhere* one can sit. A place it misses is a place a
  delete would silently break, so each site shape gets its own case;
* removing the references leaves nothing behind, and the project file still validates.
  That last one is the real check: ``validate_component_references`` is what would catch a
  cleanup that missed a spot.
"""

from __future__ import annotations

import pytest
from opi.handlers.project_file_handler import (
    COMPONENT_USAGE_DEPENDENCY,
    COMPONENT_USAGE_DEPLOYMENT,
    COMPONENT_USAGE_WEB_ADDRESS,
    ComponentUsageSite,
    component_usage_sites,
    remove_component_references,
)
from opi.manager.project_validation import validate_component_references

WEB = "web"


def _deployment(name: str = "staging", *refs: str, config: dict | None = None) -> dict:
    deployment: dict = {
        "name": name,
        "cluster": "local",
        "namespace": "demo",
        "components": [{"reference": r} for r in refs],
    }
    if config is not None:
        deployment["services"] = [{"reference": "publish-on-web", "config": config}]
    return deployment


# ---------------------------------------------------------------------------
# Every place a reference can sit
# ---------------------------------------------------------------------------


class TestTheWalkFindsEverySite:
    def test_a_deployment_that_deploys_it(self) -> None:
        project = {"components": [{"name": WEB}], "deployments": [_deployment("staging", WEB)]}

        assert component_usage_sites(project)[WEB] == [ComponentUsageSite("staging", None, COMPONENT_USAGE_DEPLOYMENT)]

    def test_another_component_depending_on_it(self) -> None:
        project = {"components": [{"name": WEB}, {"name": "worker", "uses-components": [WEB]}]}

        assert component_usage_sites(project)[WEB] == [ComponentUsageSite(None, "worker", COMPONENT_USAGE_DEPENDENCY)]

    def test_a_deployment_whose_web_address_is_built_around_it(self) -> None:
        project = {
            "components": [{"name": WEB}],
            "deployments": [_deployment("staging", WEB, config={"root-component": WEB})],
        }

        assert ComponentUsageSite("staging", None, COMPONENT_USAGE_WEB_ADDRESS) in component_usage_sites(project)[WEB]

    def test_a_component_exposed_on_the_bare_domain(self) -> None:
        """The second setting that names a component. Missing it would let a delete take
        away the component the bare domain points at."""
        project = {
            "components": [{"name": WEB}],
            "deployments": [_deployment("staging", WEB, config={"expose-component-on-bare-domain": WEB})],
        }

        assert ComponentUsageSite("staging", None, COMPONENT_USAGE_WEB_ADDRESS) in component_usage_sites(project)[WEB]

    def test_a_web_address_setting_still_at_the_deployment_root(self) -> None:
        """Pre-v2.7 files keep the setting on the deployment itself; get_domain_setting
        reads both, and so does this walk by going through it."""
        deployment = _deployment("staging", WEB)
        deployment["root-component"] = WEB
        project = {"components": [{"name": WEB}], "deployments": [deployment]}

        assert ComponentUsageSite("staging", None, COMPONENT_USAGE_WEB_ADDRESS) in component_usage_sites(project)[WEB]

    def test_the_component_s_own_definition_is_not_a_use(self) -> None:
        """Otherwise nothing would ever be free and every delete would need confirming."""
        project = {"components": [{"name": WEB}], "deployments": []}

        assert component_usage_sites(project) == {}

    def test_every_site_is_reported_once_per_place(self) -> None:
        project = {
            "components": [{"name": WEB}, {"name": "worker", "uses-components": [WEB]}],
            "deployments": [
                _deployment("staging", WEB, config={"root-component": WEB}),
                _deployment("production", WEB),
            ],
        }

        assert [site.kind for site in component_usage_sites(project)[WEB]] == [
            COMPONENT_USAGE_DEPENDENCY,
            COMPONENT_USAGE_DEPLOYMENT,
            COMPONENT_USAGE_WEB_ADDRESS,
            COMPONENT_USAGE_DEPLOYMENT,
        ]

    def test_a_deployment_that_deploys_something_else_is_not_a_use(self) -> None:
        project = {"components": [{"name": WEB}, {"name": "worker"}], "deployments": [_deployment("staging", "worker")]}

        assert WEB not in component_usage_sites(project)

    @pytest.mark.parametrize(
        "project",
        [
            {},
            {"components": None, "deployments": None},
            {"components": ["nonsense"], "deployments": ["nonsense"]},
            {"components": [{"name": WEB, "uses-components": None}]},
        ],
    )
    def test_a_shape_the_walk_cannot_read_is_not_a_crash(self, project: dict) -> None:
        """The walk runs on whatever is in the file, including on the way to a 404."""
        assert component_usage_sites(project) == {}


class TestTheLabels:
    def test_a_deployment_is_named_by_its_deployment(self) -> None:
        assert ComponentUsageSite("staging", None, COMPONENT_USAGE_DEPLOYMENT).label == "deployment 'staging'"

    def test_a_dependency_is_named_by_the_component_that_declares_it(self) -> None:
        assert ComponentUsageSite(None, "worker", COMPONENT_USAGE_DEPENDENCY).label == "component 'worker'"

    def test_a_web_address_says_it_is_the_address(self) -> None:
        """Not just 'deployment staging' -- the reader has to know why this one refuses."""
        assert (
            ComponentUsageSite("staging", None, COMPONENT_USAGE_WEB_ADDRESS).label
            == "het webadres van deployment 'staging'"
        )

    def test_a_site_reads_the_same_as_a_dict_and_as_a_label(self) -> None:
        site = ComponentUsageSite("staging", None, COMPONENT_USAGE_DEPLOYMENT)

        assert site.as_dict() == {
            "deployment": "staging",
            "component": None,
            "kind": COMPONENT_USAGE_DEPLOYMENT,
            "label": "deployment 'staging'",
        }


# ---------------------------------------------------------------------------
# Taking the references away
# ---------------------------------------------------------------------------


class TestRemovingTheReferences:
    def test_the_deployment_entry_goes(self) -> None:
        project = {"components": [{"name": WEB}], "deployments": [_deployment("staging", WEB, "worker")]}

        removed = remove_component_references(project, WEB)

        assert project["deployments"][0]["components"] == [{"reference": "worker"}]
        assert [site.label for site in removed] == ["deployment 'staging'"]

    def test_the_deployment_itself_stays(self) -> None:
        """Its last component leaving is not the deployment leaving: what else it carries
        (services, backup, sleep) is not this call's to throw away."""
        project = {"components": [{"name": WEB}], "deployments": [_deployment("staging", WEB)]}

        remove_component_references(project, WEB)

        assert project["deployments"][0]["name"] == "staging"
        assert project["deployments"][0]["components"] == []

    def test_the_dependency_declaration_goes(self) -> None:
        project = {"components": [{"name": WEB}, {"name": "worker", "uses-components": [WEB, "cache"]}]}

        removed = remove_component_references(project, WEB)

        assert project["components"][1]["uses-components"] == ["cache"]
        assert [site.label for site in removed] == ["component 'worker'"]

    def test_a_web_address_use_is_left_alone(self) -> None:
        """Deliberate: it is refused before this ever runs, because deciding how the site
        should be served instead is not a decision a delete gets to make."""
        project = {
            "components": [{"name": WEB}],
            "deployments": [_deployment("staging", WEB, config={"root-component": WEB})],
        }

        remove_component_references(project, WEB)

        assert project["deployments"][0]["services"][0]["config"]["root-component"] == WEB

    def test_nothing_is_left_pointing_at_the_deleted_component(self) -> None:
        """The property that matters: after the cleanup and the removal, the reference
        check that guards every save is satisfied."""
        project = {
            "name": "demo",
            "components": [{"name": WEB}, {"name": "worker", "uses-components": [WEB]}],
            "deployments": [_deployment("staging", WEB, "worker"), _deployment("production", WEB)],
        }

        remove_component_references(project, WEB)
        project["components"] = [c for c in project["components"] if c["name"] != WEB]

        assert WEB not in component_usage_sites(project)
        for deployment in project["deployments"]:
            assert validate_component_references(project, deployment["components"], "deployment")["success"]

    def test_a_component_nothing_references_changes_nothing(self) -> None:
        project = {"components": [{"name": WEB}], "deployments": [_deployment("staging", "worker")]}
        before = str(project)

        assert remove_component_references(project, WEB) == []
        assert str(project) == before
