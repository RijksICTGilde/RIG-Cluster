"""Guard: every own static reference in a template goes through static_url().

A bare src="/static/..." is served with no-cache, so it is never broken - but it does give
up the guarantee that a replaced file cannot arrive stale. This test makes adding one
visible right away instead of in six months.
"""

import re

from opi.core.templates import TEMPLATES_DIR

# src="/static/... or href="/static/... without a Jinja expression in it.
BARE_STATIC_REFERENCE = re.compile(r'(?:src|href)="/static/')
# ROOS assets live on their own mount and ROOS emits those URLs itself; see static_url().
ROOS_PREFIX = re.compile(r'(?:src|href)="/static/roos/')


def test_no_bare_static_references_in_templates() -> None:
    offenders: list[str] = []
    for template in sorted(TEMPLATES_DIR.rglob("*.j2")):
        for number, line in enumerate(template.read_text().splitlines(), start=1):
            if BARE_STATIC_REFERENCE.search(line) and not ROOS_PREFIX.search(line):
                offenders.append(f"{template.relative_to(TEMPLATES_DIR)}:{number}: {line.strip()}")

    assert not offenders, "Use static_url('<path>') instead of a bare /static/ URL:\n" + "\n".join(offenders)
