"""A registry that cannot answer must never auto-disable a component.

Auto-disable sets ``replicas: 0``, which removes the pod that would have retried the
pull. For a missing image that is the right call. For a registry 5xx or a rate limit
it is not: the registry never said the image is absent, so disabling turns a hiccup
into an outage that outlives it (the incident on 2026-08-12, where the ODCN
pull-through mirror returned 500 on manifests that upstream served fine).
"""

from opi.handlers.project_file_handler import is_transient_registry_error


def test_mirror_500_is_a_registry_failure() -> None:
    # The exact kubelet message from the 2026-08-12 incident.
    assert is_transient_registry_error(
        "ErrImagePull: unable to pull image or OCI artifact: pull image err: initializing source "
        "docker://rcr.rijksapps.nl/ghcr-rig/minbzk/fbs-berichtenmagazijn:pr-186-5d4e19a: reading manifest "
        "pr-186-5d4e19a in rcr.rijksapps.nl/ghcr-rig/minbzk/fbs-berichtenmagazijn: "
        "received unexpected HTTP status: 500 Internal Server Error"
    )


def test_other_registry_side_failures() -> None:
    assert is_transient_registry_error("unexpected status from HEAD request: 502 Bad Gateway")
    assert is_transient_registry_error("received unexpected HTTP status: 503 Service Unavailable")
    assert is_transient_registry_error("received unexpected HTTP status: 504 Gateway Timeout")
    assert is_transient_registry_error("toomanyrequests: 429 Too Many Requests")


def test_missing_image_still_disables() -> None:
    # These mean the registry answered and the image is not there, so the component
    # is genuinely undeployable and auto-disable stays correct.
    assert not is_transient_registry_error("ErrImagePull: manifest unknown")
    assert not is_transient_registry_error("manifest for repo:tag not found")
    assert not is_transient_registry_error("401 Unauthorized")
    assert not is_transient_registry_error("denied: requested access to the resource is denied")
    assert not is_transient_registry_error("")
    assert not is_transient_registry_error(None)


def test_a_tag_that_looks_like_a_status_code_is_not_a_registry_failure() -> None:
    # The tag is part of the same message; matching a bare "500" would read a
    # perfectly normal PR tag as a registry outage and never disable anything again.
    assert not is_transient_registry_error(
        "ErrImagePull: reading manifest pr-500-abc1234 in rcr.rijksapps.nl/ghcr-rig/minbzk/app: manifest unknown"
    )
    assert not is_transient_registry_error("ErrImagePull: manifest unknown for tag build-429-x")
