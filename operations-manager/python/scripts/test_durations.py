"""Turn the junit xml of a test run into tests/test_durations.json: seconds per test file.

CI uses it to split the suite over the shards (see ``_split_over_shards`` in
tests/conftest.py). Refresh it with ``task test-durations`` when the split drifts.

    uv run python scripts/test_durations.py <junit.xml>
"""

import json
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

TESTS = Path(__file__).resolve().parents[1] / "tests"


def _test_file(classname: str) -> str | None:
    """The test file behind a junit classname such as ``tests.forms.test_x.TestY``."""
    parts = classname.split(".")
    for length in range(len(parts), 1, -1):
        candidate = TESTS.parent.joinpath(*parts[:length]).with_suffix(".py")
        if candidate.is_file():
            return candidate.relative_to(TESTS).as_posix()
    return None


def main(junit: Path) -> None:
    durations: dict[str, float] = defaultdict(float)
    # The xml comes from our own pytest run, not from outside.
    for testcase in ET.parse(junit).iter("testcase"):  # noqa: S314
        test_file = _test_file(testcase.get("classname", ""))
        if test_file:
            durations[test_file] += float(testcase.get("time", "0"))
    output = TESTS / "test_durations.json"
    output.write_text(json.dumps({k: round(v, 1) for k, v in sorted(durations.items())}, indent=1) + "\n")
    print(f"{len(durations)} files, {sum(durations.values()) / 60:.1f} min, to {output}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
