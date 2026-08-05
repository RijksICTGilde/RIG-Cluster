"""The dangerous project actions are addressed by key, never by endpoint.

What matters here is the negative half: a key plus a target that this project does not
have must produce nothing at all. The confirmation renders the endpoint it gets back,
and one of these endpoints deletes an entire project.
"""

import pytest

from opi.web.project_actions import build_project_action

PROJECT = {
    "name": "demo",
    "display-name": "Demo Project",
    "deployments": [{"name": "prod"}, {"name": "acc"}],
    "components": [{"name": "web"}, {"name": "worker"}],
    "services": [
        {
            "attachments": {
                "data": [
                    {"id": "keystore", "filename": "keystore.p12", "content": "x"},
                    {"id": "unused", "filename": "unused.pem", "content": "y"},
                ]
            }
        }
    ],
}


@pytest.mark.parametrize(
    ("action_key", "target", "endpoint"),
    [
        ("refresh-project", None, "/projects/demo/refresh"),
        ("delete-project", None, "/projects/delete/demo"),
        ("refresh-deployment", "prod", "/projects/demo/refresh/prod"),
        ("delete-deployment", "acc", "/projects/demo/delete-deployment/acc"),
        ("delete-component", "worker", "/projects/demo/delete-component/worker"),
        ("delete-attachment", "unused", "/projects/demo/attachments/unused/delete"),
    ],
)
def test_every_action_builds_its_own_endpoint(action_key: str, target: str | None, endpoint: str) -> None:
    action = build_project_action("demo", PROJECT, action_key, target)

    assert action is not None
    assert action.endpoint == endpoint
    assert action.key == action_key
    assert action.message


@pytest.mark.parametrize(
    ("action_key", "target"),
    [
        # A target this project does not have.
        ("delete-deployment", "other-project-deployment"),
        ("refresh-deployment", "nope"),
        ("delete-component", "nope"),
        ("delete-attachment", "nope"),
        # A target where none belongs: the project-wide actions take no target, so a
        # target must not be able to steer them.
        ("delete-project", "prod"),
        ("refresh-project", "prod"),
        # A missing target where one is required.
        ("delete-deployment", None),
        ("delete-component", ""),
        # An unknown action.
        ("delete-everything", None),
        ("", None),
    ],
)
def test_unknown_action_or_foreign_target_yields_nothing(action_key: str, target: str | None) -> None:
    assert build_project_action("demo", PROJECT, action_key, target) is None


def test_target_is_url_encoded_into_the_endpoint() -> None:
    """A target is data, not a path: it can never add path segments of its own."""
    project = {"name": "demo", "components": [{"name": "a/../b"}]}

    action = build_project_action("demo", project, "delete-component", "a/../b")

    assert action is not None
    assert action.endpoint == "/projects/demo/delete-component/a%2F..%2Fb"


def test_attachment_in_use_is_refused_up_front() -> None:
    """An attachment a component still couples cannot be deleted; say so in the dialog."""
    project = {
        "name": "demo",
        "components": [
            {
                "name": "web",
                "services": [{"attachments": {"config": [{"reference": "keystore", "provide-as": "file"}]}}],
            }
        ],
        "services": [{"attachments": {"data": [{"id": "keystore", "filename": "keystore.p12"}]}}],
    }

    action = build_project_action("demo", project, "delete-attachment", "keystore")

    assert action is not None
    assert action.blocked_reason is not None
    assert "web" in action.blocked_reason


def test_free_attachment_has_no_blocked_reason() -> None:
    action = build_project_action("demo", PROJECT, "delete-attachment", "unused")

    assert action is not None
    assert action.blocked_reason is None


def test_message_names_the_project_by_its_display_name() -> None:
    """The user confirms what they recognise, not the internal name."""
    action = build_project_action("demo", PROJECT, "delete-project", None)

    assert action is not None
    assert "Demo Project" in action.message
