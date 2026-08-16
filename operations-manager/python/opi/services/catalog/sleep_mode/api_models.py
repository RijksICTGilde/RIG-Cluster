"""What the two sleep-mode endpoints answer, as models a client can read off the spec.

Both endpoints used to return a bare ``JSONResponse``, so ``/openapi.json`` held no schema,
no values and no explanation: a generated client saw an object and had to guess. Worse, the
two neighbouring endpoints both called their field ``state`` while meaning different things:

* ``/status`` answers ``starting | ready``. That is the poll contract of the waker image,
  which only checks whether the word is ``ready`` (``images/zad-waker/main.go``). The image
  is pulled from a registry and can be older than this code, so that field is frozen: it
  keeps its two values and its exact spelling, forever.
* ``/wake`` answers ``awake | sleeping | waking``, the real sleep state.

One word, three meanings. Rather than break a running waker, both endpoints now carry a
second field, ``sleep_state``, that always means the same thing: the deployment's real
sleep state, plus ``disabled`` when sleep-mode does not apply to this deployment at all.
That last value is what ``/status`` could never say -- it reported a hardcoded ``starting``
for a project without a waker, which is why the CLI saw ``starting`` and nothing else.

The per-value descriptions travel as ``x-choices``, the same key ``opi/api/openapi_choices.py``
puts on service config fields, so a reader meets one convention for "these are the values
and this is what each one means" instead of two.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from opi.api.openapi_choices import CHOICES_KEY

#: The real sleep state of a deployment, as reported by both endpoints.
SleepStateValue = Literal["awake", "sleeping", "waking", "disabled"]

#: What the waker polls for. Frozen: a waker image older than this code reads it.
WakerStateValue = Literal["starting", "ready"]

DISABLED = "disabled"
"""Sleep-mode does not apply to this deployment, so it has no sleep state of its own."""


def _choices(*options: tuple[str, str]) -> dict[str, Any]:
    """The allowed values with a description each, in the shared ``x-choices`` shape."""
    return {CHOICES_KEY: [{"const": value, "title": value, "description": text} for value, text in options]}


_SLEEP_STATE_CHOICES = _choices(
    ("awake", "The deployment runs normally; its components are scaled up."),
    ("sleeping", "Scaled to zero. A waker page stands in for it and the first visitor wakes it."),
    ("waking", "A wake is in progress: the app is cold-starting while the waker still serves the page."),
    (DISABLED, "Sleep-mode is not configured for this deployment, so it never sleeps."),
)

_WAKER_STATE_CHOICES = _choices(
    ("starting", "The app behind the waker has no ready pod yet; keep polling."),
    ("ready", "The app has a ready pod. The waker steps out of the EndpointSlice."),
)


class SleepStatusResponse(BaseModel):
    """The answer to ``GET .../status``: what the waker polls, and the real state."""

    state: WakerStateValue = Field(
        ...,
        description=(
            "The waker's poll contract, and only that: 'ready' means the app behind the waker has a "
            "ready pod. Deliberately unchanged and deliberately narrow -- the waker image is pulled "
            "separately and may be older than this API. It is 'starting' whenever the app is not "
            "ready yet AND whenever there is no waker to speak of, so it is not a sleep state. Read "
            "'sleep_state' for that."
        ),
        json_schema_extra=_WAKER_STATE_CHOICES,
    )
    sleep_state: SleepStateValue = Field(
        ...,
        description=(
            "The deployment's real sleep state. 'disabled' means sleep-mode is not configured for "
            "this deployment; the three other values are the states it moves between."
        ),
        json_schema_extra=_SLEEP_STATE_CHOICES,
    )


class WakeResponse(BaseModel):
    """The answer to ``POST .../wake``: 202 when a transition started, 200 for a no-op."""

    state: SleepStateValue = Field(
        ...,
        description=(
            "The sleep state after this call. Same values as 'sleep_state' and kept for the callers "
            "that already read it; new code should read 'sleep_state', which means the same thing on "
            "both endpoints."
        ),
        json_schema_extra=_SLEEP_STATE_CHOICES,
    )
    sleep_state: SleepStateValue = Field(
        ...,
        description="The deployment's real sleep state after this call. The same word on both endpoints.",
        json_schema_extra=_SLEEP_STATE_CHOICES,
    )
