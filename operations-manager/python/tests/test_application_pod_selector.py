"""Health checks look at the application's pods, not at pods a service runs alongside it.

Observed on the sandbox 5 August. A sleeping deployment scales its component to zero and
sleep-mode runs a waker in its place, deliberately carrying the SAME ``app`` label so it
can take over the component's Service. The health check selected on that label alone, so
it found the waker, read its ImagePullBackOff, and reported:

    productie - frontend: image ophalen mislukt ... zad-waker:latest

for a component that was not running and does not use that image, while ArgoCD reported
the application Synced and Healthy. That is not cosmetic: the same path disables a
component on an image-pull failure, so a waker that briefly cannot pull could take the
real component out of service.

Measured against the running cluster, before and after:

    app=productie-frontend             -> productie-frontend-waker-...  Running
    app=productie-frontend,!zad-role   -> (niets)

Nothing is the right answer there: the application was asleep.
"""

from __future__ import annotations

import inspect

from opi.services.catalog.base import SERVICE_ROLE_LABEL_KEY, application_pod_selector
from opi.services.catalog.sleep_mode.manifests import WAKER_ROLE_LABEL


def test_the_selector_excludes_anything_carrying_a_service_role() -> None:
    """``!key`` means "does not carry that label at all", so a service marking its pod
    with any role is excluded without this having to know which roles exist."""
    assert application_pod_selector("productie-frontend") == "app=productie-frontend,!zad-role"


def test_the_waker_label_is_built_from_the_platform_key() -> None:
    """One concept, one spelling. A service inventing its own key would carry a role the
    application-level lookups do not know to skip, and the bug would be back."""
    assert SERVICE_ROLE_LABEL_KEY in WAKER_ROLE_LABEL
    assert WAKER_ROLE_LABEL[SERVICE_ROLE_LABEL_KEY] == "waker"


def test_the_health_check_uses_the_selector_instead_of_a_bare_app_label() -> None:
    """Drift guard on the call site that caused this. A bare ``app={name}`` here is
    exactly the bug: it reads a service's pod as if it were the application's."""
    from opi.services import oom_watcher

    source = inspect.getsource(oom_watcher)

    assert "application_pod_selector(unique_name)" in source
    assert 'f"app={unique_name}"' not in source, "a bare app selector is back in the health check"
