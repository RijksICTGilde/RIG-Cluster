"""Summarizers - what a field looks like on a summary screen.

An editable without a summarizer shows its value, which is what almost every
field wants. These two cover the case where the value itself must not be shown:
``HiddenSummary`` leaves the field out, ``MaskedSummary`` says that it is filled
in without saying what it holds.

Both ignore the value they are given -- that is the point. They take it as an
argument because the protocol does, and because a summarizer that DOES look at
the value (shortening it, counting items) is a normal thing to write next to
these.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class HiddenSummary:
    """Keep the field out of the summary entirely.

    For values that a summary has no business repeating and that gain nothing
    from a placeholder either -- a token, a private key. The field keeps its
    normal behaviour everywhere else; only the summary skips it.
    """

    def summarize(self, value: Any, context_data: dict[str, Any] | None = None) -> str | None:
        return None


@dataclass
class MaskedSummary:
    """State that the field is set, without showing what it is set to.

    Use this where the absence of the field would read as "not configured" and
    that would be misleading. The text is a constant, never derived from the
    value.
    """

    text: str = "Ingesteld"
    empty_text: str | None = None
    """Shown when there is no value. None omits the field, which is what a
    summary usually wants: a field nobody filled in is noise."""

    def summarize(self, value: Any, context_data: dict[str, Any] | None = None) -> str | None:
        if value is None or value == "" or value == [] or value == {}:
            return self.empty_text
        return self.text
