"""Sleep-mode owns the durations it offers, and a cluster may add one.

The two lists used to sit in the shared providers module while sleep-mode's cluster
defaults already lived in its own package. A service that decides how it behaves should
decide what it offers, so the providers now ask.

The five-minute option exists for the sandbox only: the sweeper there runs every minute,
so a sleep/wake cycle completes while someone is still watching it. On production that
same choice would put a real deployment to sleep five minutes after every deploy, which
is why it is declared per cluster instead of added to the shared list.
"""

from __future__ import annotations

import inspect

import pytest
from opi.services.catalog.sleep_mode.config_model import SleepModeConfig
from opi.services.catalog.sleep_mode.options import sleep_after_deploy_options, sleep_after_wake_options


def _values(options: list[dict[str, object]]) -> list[object]:
    return [option["value"] for option in options]


@pytest.mark.parametrize("options_for", [sleep_after_deploy_options, sleep_after_wake_options])
def test_the_sandbox_offers_five_minutes(options_for) -> None:
    assert "5m" in _values(options_for("sandboxed-local"))


@pytest.mark.parametrize("options_for", [sleep_after_deploy_options, sleep_after_wake_options])
@pytest.mark.parametrize("cluster", ["odcn-production", "local", "", None])
def test_no_other_cluster_offers_it(options_for, cluster) -> None:
    """Five minutes on a real deployment is a footgun, not a shorter option."""
    assert "5m" not in _values(options_for(cluster))


@pytest.mark.parametrize("options_for", [sleep_after_deploy_options, sleep_after_wake_options])
def test_the_extra_comes_first(options_for) -> None:
    """Shortest first: a list that starts at four hours and hides five minutes at the
    bottom reads as if the short one were an afterthought."""
    assert _values(options_for("sandboxed-local"))[0] == "5m"


@pytest.mark.parametrize("options_for", [sleep_after_deploy_options, sleep_after_wake_options])
def test_a_cluster_extra_never_removes_a_shared_choice(options_for) -> None:
    """Adding, not replacing: a cluster that offered fewer choices than the rest would be
    a difference nobody asked for."""
    shared = set(_values(options_for("odcn-production")))
    assert shared <= set(_values(options_for("sandboxed-local")))


@pytest.mark.parametrize("options_for", [sleep_after_deploy_options, sleep_after_wake_options])
def test_every_offered_duration_is_one_the_model_accepts(options_for) -> None:
    """An option the config model rejects would fail on save, after the user picked it."""
    for value in _values(options_for("sandboxed-local")):
        SleepModeConfig(**{"enabled": True, "sleep-after-deploy": value, "sleep-after-wake": value})


def test_the_form_layer_no_longer_holds_the_list() -> None:
    """Drift guard: the durations moved to the service, and a copy left behind in the
    providers module is exactly how the two would start disagreeing."""
    from opi.forms.visualizers import providers

    source = inspect.getsource(providers)

    assert '"168h"' not in source, "the duration list is back in the form layer"
    assert "sleep_after_deploy_options" in source
    assert "sleep_after_wake_options" in source
