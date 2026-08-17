"""How the API documentation is grouped, and that it stays that way (RC-45).

Swagger UI groups on tag and shows an operation under *every* tag it carries. So a
second tag is not extra information, it is a second copy of the same endpoint on the
page. Measured before this test existed: 101 operations rendered as 245 lines, because
service endpoints carried four tags (``v2`` twice, ``services``, and the service name).

The rule is therefore one tag per operation: the group it belongs to, and nothing else.
The version lives in the path (``/api/v2/...``), so it is not a group -- a ``v2`` tag
with 106 members groups nothing, it puts everything together.

These numbers run back silently, which is why they are asserted rather than remembered.
"""

from __future__ import annotations

import pytest
from opi.server import app

_METHODS = ("get", "post", "put", "delete", "patch")


@pytest.fixture(scope="module")
def spec() -> dict:
    return app.openapi()


@pytest.fixture(scope="module")
def operations(spec: dict) -> list[tuple[str, str, dict]]:
    return [
        (path, method, operation)
        for path, item in spec["paths"].items()
        for method, operation in item.items()
        if method in _METHODS
    ]


class TestOneGroupPerOperation:
    def test_every_operation_carries_exactly_one_tag(self, operations) -> None:
        offenders = {
            f"{method.upper()} {path}": operation.get("tags")
            for path, method, operation in operations
            if len(operation.get("tags", [])) != 1
        }
        assert not offenders, (
            f"every operation belongs to one group; these carry a different number of tags: {offenders}"
        )

    def test_the_page_shows_each_operation_once(self, operations) -> None:
        # The measurement that made this plan: rendered lines is the sum over tags,
        # because Swagger UI repeats an operation under each of its tags.
        rendered = sum(len(operation.get("tags", [])) for _, _, operation in operations)
        assert rendered == len(operations)

    def test_no_operation_repeats_a_tag(self, operations) -> None:
        offenders = {
            f"{method.upper()} {path}": operation["tags"]
            for path, method, operation in operations
            if len(operation["tags"]) != len(set(operation["tags"]))
        }
        assert not offenders, f"a repeated tag renders the operation twice under one group: {offenders}"

    def test_the_version_is_not_a_group(self, operations) -> None:
        # /api/v2/... already says it. A version tag would collect every v2 endpoint into
        # one heading, which is the opposite of grouping.
        tags = {tag for _, _, operation in operations for tag in operation["tags"]}
        assert not {tag for tag in tags if tag in {"v1", "v2"}}

    def test_a_service_endpoint_is_grouped_under_its_service(self, operations) -> None:
        # The endpoints generated per service (config writes and declared actions) belong
        # to that service's group, not also to a generic "services" one.
        by_key = {f"{method.upper()} {path}": operation for path, method, operation in operations}
        upload = by_key["POST /api/v2/projects/{project_name}/services/attachments/attachment"]
        assert upload["tags"] == ["attachments"]
        config_write = by_key["PUT /api/v2/projects/{project_name}/services/redis/config/project"]
        assert config_write["tags"] == ["redis"]


class TestEveryServiceOperationExplainsItself:
    """A service endpoint says what it does, not only what it is called (RC-45).

    Measured before this test existed: 34 of the 39 service operations had a summary and
    nothing else. "Upsert redis config (project)" names the service and the layer, which
    the path already did. What a caller cannot guess is where the value lands, whether
    writing it starts a rollout, and what happens when there is nothing to clear.

    The same rule RC-38 set for config *fields*, now for the operations themselves.
    """

    @pytest.fixture(scope="class")
    def service_operations(self, operations) -> dict[str, dict]:
        return {f"{method.upper()} {path}": operation for path, method, operation in operations if "/services" in path}

    def test_every_service_operation_carries_a_description(self, service_operations) -> None:
        undocumented = sorted(key for key, operation in service_operations.items() if not operation.get("description"))
        assert not undocumented, f"these service operations say only what they are called: {undocumented}"

    def test_a_description_is_not_the_summary_again(self, service_operations) -> None:
        # The trap the plan names: "Upload an attachment" is already the summary. A
        # description that repeats it documents nothing and passes a presence check.
        for key, operation in service_operations.items():
            description = operation["description"].strip()
            assert description != operation.get("summary", "").strip(), key
            assert len(description) > len(operation.get("summary", "")), key

    def test_a_config_write_says_where_it_lands_and_whether_it_rolls_out(self, service_operations) -> None:
        write = service_operations["PUT /api/v2/projects/{project_name}/services/redis/config/project"]["description"]
        assert "zad-projects" in write
        assert "rolled out" in write
        assert "/api/tasks/" in write

    def test_clearing_says_what_an_empty_clear_does(self, service_operations) -> None:
        clear = service_operations["DELETE /api/v2/projects/{project_name}/services/redis/config/project"][
            "description"
        ]
        assert "not there changes nothing" in clear

    def test_a_component_write_says_it_selects_the_service(self, service_operations) -> None:
        # The side effect a caller would otherwise discover by reading the project file:
        # configuring at component level selects the service at project level.
        key = "PUT /api/v2/projects/{project_name}/services/health-check/config/component/{component_name}"
        assert "selects it at project level" in service_operations[key]["description"]
