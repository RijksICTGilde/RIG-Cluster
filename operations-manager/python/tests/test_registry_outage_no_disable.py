"""A registry that cannot answer must never auto-disable a component.

Auto-disable sets ``replicas: 0``, which removes the pod that would have retried the
pull. For an image the registry says is absent that is the right call. For anything
we could not diagnose it is not: the registry never said the image is gone, so
disabling turns an outage into one that outlives its cause.

The check is an allowlist of "the registry answered: absent" on purpose. It used to
be the inverse -- a list of ways a registry can fail -- and that list is unbounded
and always one incident behind. It named 5xx and rate limits after the 2026-08-12
mirror incident, and was blind on 2026-09-10 when the same mirror stopped answering
at transport level ("...: EOF"), so every component on it was disabled as if its
image were gone.
"""

from opi.handlers.project_file_handler import ProjectFileHandler, image_is_confirmed_absent

# The exact kubelet messages from the 2026-09-10 outage, where the mirror's TLS
# endpoint accepted the connection and then sent nothing back.
OUTAGE_2026_09_10 = (
    "ErrImagePull: unable to pull image or OCI artifact: pull image err: initializing source "
    "docker://rcr.rijksapps.nl/ghcr-rig/minbzk/fbs-berichtenmagazijn:pr-307-63bd3e2: pinging "
    'container registry rcr.rijksapps.nl: Get "https://rcr.rijksapps.nl/v2/": EOF',
    'ImagePullBackOff: Back-off pulling image "rcr.rijksapps.nl/ghcr-rig/minbzk/fbs-demo-console'
    ':pr-307-63bd3e2": ErrImagePull: unable to pull image or OCI artifact: pull image err: '
    'pinging container registry rcr.rijksapps.nl: Get "https://rcr.rijksapps.nl/v2/": EOF',
    'pinging container registry rcr.rijksapps.nl: Get "https://rcr.rijksapps.nl/v2/": net/http: TLS handshake timeout',
    "ErrImagePull: rpc error: code = DeadlineExceeded desc = unable to pull image: context deadline exceeded",
)


def test_transport_level_outage_does_not_disable() -> None:
    # None of these carry an answer from the registry, so none may disable.
    for message in OUTAGE_2026_09_10:
        assert not image_is_confirmed_absent(message), message


def test_mirror_500_is_not_an_answer_about_the_image() -> None:
    # The exact kubelet message from the 2026-08-12 incident.
    assert not image_is_confirmed_absent(
        "ErrImagePull: unable to pull image or OCI artifact: pull image err: initializing source "
        "docker://rcr.rijksapps.nl/ghcr-rig/minbzk/fbs-berichtenmagazijn:pr-186-5d4e19a: reading manifest "
        "pr-186-5d4e19a in rcr.rijksapps.nl/ghcr-rig/minbzk/fbs-berichtenmagazijn: "
        "received unexpected HTTP status: 500 Internal Server Error"
    )


def test_a_full_registry_quota_is_not_an_answer_about_the_image() -> None:
    # The exact kubelet messages from 2026-09-30, when the storage quota on the shared
    # proxy-cache organisation ghcr-rig filled up. Quay answers a cache miss with the
    # distribution-spec code DENIED, so the marker "denied" matched and ~40 components
    # across six projects were auto-disabled while every image was present upstream.
    assert not image_is_confirmed_absent(
        "ErrImagePull: unable to pull image or OCI artifact: pull image err: initializing source "
        "docker://rcr.rijksapps.nl/ghcr-rig/rijksictgilde/wies:pr-692-20260930-112622-873777b: reading "
        "manifest pr-692-20260930-112622-873777b in rcr.rijksapps.nl/ghcr-rig/rijksictgilde/wies: "
        "denied: Quota has been exceeded on namespace; artifact err: get manifest: build image source: "
        "reading manifest pr-692-20260930-112622-873777b in rcr.rijksapps.nl/ghcr-rig/rijksictgilde/wies: "
        "denied: Quota has been exceeded on namespace"
    )
    assert not image_is_confirmed_absent(
        'ImagePullBackOff: Back-off pulling image "rcr.rijksapps.nl/ghcr-rig/minbzk/'
        "moza-notificatiemanagementcomponent@sha256:6a7bc2a52e838f5ca549a6201d71560b830cb30fcc8de98f5dea9c5c"
        '84b222bd": ErrImagePull: reading manifest sha256:6a7bc2a52e838f5ca549a6201d71560b830cb30fcc8de98f5'
        "dea9c5c84b222bd in rcr.rijksapps.nl/ghcr-rig/minbzk/moza-notificatiemanagementcomponent: "
        "denied: Quota has been exceeded on namespace"
    )


def test_other_registry_side_failures() -> None:
    assert not image_is_confirmed_absent("unexpected status from HEAD request: 502 Bad Gateway")
    assert not image_is_confirmed_absent("received unexpected HTTP status: 503 Service Unavailable")
    assert not image_is_confirmed_absent("received unexpected HTTP status: 504 Gateway Timeout")
    assert not image_is_confirmed_absent("toomanyrequests: 429 Too Many Requests")


def test_an_unknown_phrase_does_not_disable() -> None:
    # The point of the allowlist: a failure mode nobody has written down yet lands on
    # "we do not know", which is the side that changes nothing.
    assert not image_is_confirmed_absent("connection reset by peer")
    assert not image_is_confirmed_absent("some entirely new registry failure from 2027")
    # No message at all is no answer either.
    assert not image_is_confirmed_absent("")
    assert not image_is_confirmed_absent(None)


def test_missing_image_still_disables() -> None:
    # These mean the registry answered and the image is not there (or may not be
    # fetched), so the component is genuinely undeployable and auto-disable is correct.
    assert image_is_confirmed_absent("ErrImagePull: manifest unknown")
    assert image_is_confirmed_absent("manifest for repo:tag not found")
    assert image_is_confirmed_absent("401 Unauthorized")
    assert image_is_confirmed_absent("denied: requested access to the resource is denied")
    assert image_is_confirmed_absent("InvalidImageName: couldn't parse image reference")


def test_a_tag_that_looks_like_a_status_code_still_disables() -> None:
    # The tag is part of the same message. Matching a bare "500" would have read a
    # normal PR tag as a registry outage; the allowlist never looks at numbers.
    assert image_is_confirmed_absent(
        "ErrImagePull: reading manifest pr-500-abc1234 in rcr.rijksapps.nl/ghcr-rig/minbzk/app: manifest unknown"
    )
    assert image_is_confirmed_absent("ErrImagePull: manifest unknown for tag build-429-x")


def _project_with_disabled_component() -> dict:
    return {
        "deployments": [
            {
                "name": "pr-307",
                "components": [
                    {
                        "reference": "magazijna",
                        "image": "ghcr.io/minbzk/app:pr-307",
                        "disabled": True,
                        "disabled-reason": "ErrImagePull: manifest unknown",
                        "disabled-image": "ghcr.io/minbzk/app:pr-307",
                    }
                ],
            }
        ]
    }


def test_rewriting_the_same_disable_changes_nothing() -> None:
    # Kubelet alternates between ErrImagePull and ImagePullBackOff for one failure.
    # That must not produce a second write, because every write is a commit, a push
    # and an ArgoCD refresh.
    handler = ProjectFileHandler()
    project_data = _project_with_disabled_component()
    before = str(project_data)

    handler.set_deployment_component_disabled(
        project_data,
        "pr-307",
        "magazijna",
        True,
        'ImagePullBackOff: Back-off pulling image "ghcr.io/minbzk/app:pr-307": manifest unknown',
    )

    assert str(project_data) == before


def test_a_disable_for_a_new_image_is_written() -> None:
    # A different image is new information and must land.
    handler = ProjectFileHandler()
    project_data = _project_with_disabled_component()
    project_data["deployments"][0]["components"][0]["image"] = "ghcr.io/minbzk/app:pr-308"

    handler.set_deployment_component_disabled(
        project_data, "pr-307", "magazijna", True, "ErrImagePull: manifest unknown"
    )

    component = project_data["deployments"][0]["components"][0]
    assert component["disabled-image"] == "ghcr.io/minbzk/app:pr-308"
