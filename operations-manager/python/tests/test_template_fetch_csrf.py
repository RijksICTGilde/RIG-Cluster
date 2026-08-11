"""Every template fetch() that writes must send the CSRF token.

The CSRFMiddleware rejects POST/PUT/PATCH/DELETE without a valid token, so a
front-end fetch() that omits the X-CSRF-Token header gets a 403 and the action
silently fails (the user sees "Onbekende fout"). This has bitten repeatedly
(the #71 rollout missed several buttons; the project/deployment/component
delete fetch missed it too). This sweep guards the whole class.

Htmx requests are out of scope here -- they carry the token via inherited
hx-headers, not fetch().
"""

import re
from pathlib import Path

_TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "opi" / "templates_lotc"
_WRITE_METHOD = re.compile(r"""method\s*:\s*['"](POST|PUT|PATCH|DELETE)['"]""", re.IGNORECASE)


def _fetch_windows(text: str) -> list[str]:
    """The text following each ``fetch(`` (enough to cover its options object)."""
    return [text[m.start() : m.start() + 600] for m in re.finditer(r"fetch\s*\(", text)]


def test_template_fetch_writes_carry_csrf_token() -> None:
    offenders = sorted(
        {
            str(path.relative_to(_TEMPLATE_DIR))
            for path in _TEMPLATE_DIR.rglob("*.j2")
            for window in _fetch_windows(path.read_text(encoding="utf-8"))
            if _WRITE_METHOD.search(window) and "X-CSRF-Token" not in window
        }
    )

    assert not offenders, (
        f"fetch() POST/PUT/PATCH/DELETE without an X-CSRF-Token header (will 403 via CSRFMiddleware): {offenders}"
    )
