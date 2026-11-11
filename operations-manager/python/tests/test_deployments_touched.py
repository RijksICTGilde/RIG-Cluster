"""``deployments_touched``: the rollout scope of a whole-file edit, read off the two states.

Every portal save goes through one use case now, and none of those entrances knows which
deployments it touched: the wizard writes a whole project, the modal a flow's write paths,
the approval route a verdict. So the scope is computed from the difference, and this pins
which way the rule leans. Too narrow leaves the cluster behind the project file; too wide
only costs time, so every uncertainty resolves to project-wide (None).
"""

from __future__ import annotations

from typing import Any

from opi.services.project_utils import deployments_touched


def _project(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "demo",
        "display-name": "Demo",
        "components": [{"name": "web", "image": "nginx:1.25"}, {"name": "api", "image": "api:1"}],
        "deployments": [
            {"name": "live", "cluster": "local", "components": [{"reference": "web", "image": "nginx:1.25"}]},
            {"name": "test", "cluster": "local", "components": [{"reference": "api", "image": "api:1"}]},
        ],
    }
    base.update(overrides)
    return base


def test_an_unchanged_project_touches_nothing() -> None:
    assert deployments_touched(_project(), _project()) == []


def test_a_changed_deployment_touches_only_itself() -> None:
    after = _project()
    after["deployments"][0]["components"][0]["image"] = "nginx:1.26"

    assert deployments_touched(_project(), after) == ["live"]


def test_an_added_deployment_touches_only_the_new_one() -> None:
    after = _project()
    after["deployments"].append({"name": "pr-12", "cluster": "local", "components": []})

    assert deployments_touched(_project(), after) == ["pr-12"]


def test_a_removed_deployment_is_project_wide() -> None:
    """A narrowed round cannot take the manifests of a deployment that is gone out."""
    after = _project()
    after["deployments"] = [after["deployments"][0]]

    assert deployments_touched(_project(), after) is None


def test_a_changed_component_touches_the_deployments_using_it() -> None:
    after = _project()
    after["components"][1]["image"] = "api:2"

    assert deployments_touched(_project(), after) == ["test"]


def test_a_component_no_deployment_uses_touches_nothing() -> None:
    before = _project()
    before["components"].append({"name": "losse", "image": "x:1"})
    after = _project()
    after["components"].append({"name": "losse", "image": "x:2"})

    assert deployments_touched(before, after) == []


def test_any_other_top_level_change_is_project_wide() -> None:
    """The project-level manifests and every deployment read the rest of the file."""
    assert deployments_touched(_project(), _project(**{"display-name": "Anders"})) is None
    assert deployments_touched(_project(), _project(users=[{"email": "a@b.nl", "roles": ["admin"]}])) is None
    assert deployments_touched(_project(), _project(services=["publish-on-web"])) is None


def test_a_renamed_component_touches_both_sides() -> None:
    """The old name disappears from a deployment and the new one appears in it."""
    after = _project()
    after["components"][0]["name"] = "frontend"
    after["deployments"][0]["components"][0]["reference"] = "frontend"

    assert deployments_touched(_project(), after) == ["live"]
