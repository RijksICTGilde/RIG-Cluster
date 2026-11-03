"""Count how many jsonpath expressions get compiled.

A compile costs about 7 ms. Code that puts a deployment name into a jsonpath compiles one
per deployment, and on a large project that held the event loop for seconds in production
(15 October 2026, RC-263). Counting compiles catches that regardless of how fast the
machine running the test is.
"""

from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import patch

from opi.utils import yaml_util

if TYPE_CHECKING:
    from collections.abc import Iterator


@dataclass
class JsonpathCompiles:
    count: int = 0


@contextmanager
def count_jsonpath_compiles() -> Iterator[JsonpathCompiles]:
    """Count real compiles, starting from an empty compiler cache."""
    compiles = JsonpathCompiles()
    real_parse = yaml_util.jsonpath_parse

    def counting_parse(path: str) -> object:
        compiles.count += 1
        return real_parse(path)

    yaml_util.compiled_jsonpath.cache_clear()
    with patch.object(yaml_util, "jsonpath_parse", counting_parse):
        yield compiles
    yaml_util.compiled_jsonpath.cache_clear()
