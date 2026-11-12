"""``internalChecksFilter`` in ``renovate.json`` must stay ``strict``.

Under ``flexible``, Renovate picks the newest release and skips ``minimumReleaseAge``
entirely once every candidate is too young, so the automerge groups below merge an
unreviewed, cooldown-less update. ``renovate-config-validator`` only checks the file is
valid Renovate config, so a silent revert to ``flexible`` would pass that check and be
caught here instead.
"""

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
RENOVATE_JSON = REPO_ROOT / "renovate.json"


def test_internal_checks_filter_is_strict() -> None:
    config = json.loads(RENOVATE_JSON.read_text(encoding="utf-8"))
    assert config["internalChecksFilter"] == "strict"
