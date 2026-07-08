"""Regression: build_component_config must emit `path` in the schema shape.

The catalog-component schema ($defs/component-path in project_v2.json) requires
`path` to be an array of {match} objects, matching the COMPONENT_PATH editable's
[{match}] default. build_component_config previously wrote the bare input string,
so add-component and non-wizard creation produced files their own schema rejected
at process time (Veld 'components/N/path': '/' is not of type 'array').
"""

from opi.utils.project_utils import build_component_config


async def test_path_is_array_of_match_default() -> None:
    cfg = await build_component_config(
        name="web",
        component_type="single",
        port=8080,
        path="/",
        services=[],
    )
    assert cfg["path"] == [{"match": "/"}]


async def test_path_is_array_of_match_custom() -> None:
    cfg = await build_component_config(
        name="api",
        component_type="single",
        port=8080,
        path="/api",
        services=[],
    )
    assert cfg["path"] == [{"match": "/api"}]
