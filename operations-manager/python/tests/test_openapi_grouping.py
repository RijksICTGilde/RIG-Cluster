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
        upload = by_key["POST /api/v2/projects/{project_name}/services/attachments/attachments"]
        assert upload["tags"] == ["attachments"]
        config_write = by_key["PUT /api/v2/projects/{project_name}/services/redis/config/project"]
        assert config_write["tags"] == ["redis"]
