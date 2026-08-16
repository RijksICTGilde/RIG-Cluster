"""A content hash of the secrets a component reads, for the pod template annotation.

The problem this solves is a change nobody sees. A component's user env-vars live in the
Secret ``{prefix}-user`` and reach the container through ``envFrom``; its file-mode
attachments live in ``{deployment}-attch-{id}`` and reach it through a ``subPath`` mount.
Both secret names are fixed, so replacing the *content* leaves the Deployment spec
byte-identical: Kubernetes sees no change, does not roll the pod, and the container keeps
the values it was started with. ``envFrom`` is injected once at container start, and a
``subPath`` mount is a one-time copy that Kubernetes never refreshes -- not after a minute,
not ever.

So the content has to reach the spec, and the only safe form for that is a hash. The pod
template ends up in a git repository (``zad-deployments``) and the source is a secret, so
nothing derived from it may be reversible: a digest of the values, never the values.

Determinism is the other requirement. This hash is rendered on every generation, so an
unchanged component must produce the same digest twice or every processing run rewrites
the manifest and churns the GitOps repo -- the failure mode ``features/sops-skip-unchanged-reencryption.md``
describes. Hence the sorted, explicitly-separated encoding below rather than anything that
depends on dict ordering or on repr.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

#: Length of the rendered digest. The full sha256 is 64 hex characters; 16 is what the
#: annotation needs to distinguish one content from another and keeps the manifest
#: readable. It is a change detector, not a signature.
HASH_LENGTH = 16

#: The annotation the pod template carries. Namespaced under the platform's own domain so
#: it cannot collide with an annotation a controller owns.
SECRET_HASH_ANNOTATION = "zad.rijksapps.nl/secret-hash"


def _feed(digest: hashlib._Hash, label: str, values: Mapping[str, str]) -> None:
    """Add one named group of key/value pairs to *digest*, order-independently.

    Every part is length-prefixed, so no combination of names and values can encode the
    same byte stream as a different combination (``a=bc`` and ``ab=c`` are distinct).
    """
    digest.update(f"{len(label)}:{label}|".encode())
    for key in sorted(values):
        value = values[key]
        digest.update(f"{len(key)}:{key}={len(value)}:".encode())
        digest.update(value.encode())
        digest.update(b"|")


def secret_content_hash(groups: Mapping[str, Mapping[str, str]]) -> str:
    """Hash the content of the secrets a component reads.

    Args:
        groups: group name -> the secret's key/value data. The group name is part of the
            hash, so moving a value from one secret to another is a change.

    Returns:
        A short hex digest, stable across runs for unchanged content. An empty mapping --
        a component with no user env-vars and no attachments -- returns an empty string,
        and the template then omits the annotation entirely rather than pinning every such
        pod to the hash of nothing.
    """
    if not any(values for values in groups.values()):
        return ""
    digest = hashlib.sha256()
    for label in sorted(groups):
        _feed(digest, label, groups[label])
    return digest.hexdigest()[:HASH_LENGTH]


def component_secret_hash(
    user_secret_name: str,
    user_env_vars: Mapping[str, object],
    attachment_file_secrets: Mapping[str, Mapping[str, str]],
) -> str:
    """The hash for one component, from the two secrets it reads.

    Args:
        user_secret_name: name of the ``{prefix}-user`` secret; part of the hash so the
            same values under another component's secret are a different pod spec.
        user_env_vars: the user env-vars as they go into that secret. Values are coerced
            to text because a project file may hold a number or a boolean there, and the
            secret stores what the manifest writes.
        attachment_file_secrets: secret name -> its data, for the file-mode attachments
            this component mounts. Env-var-mode attachments are already merged into
            ``user_env_vars`` by the caller and need no separate group.
    """
    return secret_content_hash(
        {
            user_secret_name: {str(key): str(value) for key, value in user_env_vars.items()},
            **{name: dict(data) for name, data in attachment_file_secrets.items()},
        }
    )
