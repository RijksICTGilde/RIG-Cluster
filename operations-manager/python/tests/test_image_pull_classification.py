"""Only registry auth/permission image-pull failures are real errors.

A not-found / not-yet-built image is expected churn (CI/CD, PR builds) and must be
a WARNING, not an ERROR that fails the deploy task and pages ops. This locks the
classifier that routes auth failures to sync_failures (ERROR) and everything else
to health_warnings (WARN).
"""

from opi.manager.project_manager import _is_image_pull_auth_error


def test_auth_failures_are_real_errors() -> None:
    # The quay-rig outage message we actually saw in prod.
    assert _is_image_pull_auth_error(
        "initializing source docker://rcr.rijksapps.nl/quay-rig/oauth2-proxy/oauth2-proxy:v7.7.1: "
        "unable to retrieve auth token: invalid username/password: authentication required"
    )
    assert _is_image_pull_auth_error("401 Unauthorized")
    assert _is_image_pull_auth_error("pull access denied, 403 Forbidden")
    assert _is_image_pull_auth_error("no basic auth credentials")


def test_not_found_and_transient_are_not_errors() -> None:
    assert not _is_image_pull_auth_error("manifest unknown")
    assert not _is_image_pull_auth_error("not found: manifest for repo:tag not found")
    # Bare "denied" is deliberately not auth: ghcr returns it for a private-or-missing
    # tag too, so a not-built PR image would otherwise misclassify as an error.
    assert not _is_image_pull_auth_error("denied: requested access to the resource is denied")
    assert not _is_image_pull_auth_error("ImagePullBackOff")
    assert not _is_image_pull_auth_error("")
    assert not _is_image_pull_auth_error(None)
