"""The policy document that gives an identity access to one bucket and nothing else.

One place for two object storage services (RC-331, decision 3): `minio-storage` and
`seaweedfs-storage` share the isolation, so they share the document. If it differs per
service, the isolation differs per service.
"""

from __future__ import annotations

from typing import Any

#: The permissions the managers always ask for together. No caller picks a subset; the
#: argument stays because `MinioConnector.grant_bucket_access` offers it publicly.
ALL_BUCKET_PERMISSIONS = ["read", "write", "delete", "list"]


def bucket_access_policy(bucket: str, permissions: list[str]) -> dict[str, Any]:
    """The document for ``bucket``, with the actions ``permissions`` allows.

    Listing and reading bucket properties on the bucket ARN, the data permissions on the
    objects. No ``s3:ListAllMyBuckets``: whoever holds this policy does not see the other
    buckets.
    """
    statements: list[dict[str, Any]] = []

    if "list" in permissions or "read" in permissions:
        statements.append(
            {
                "Effect": "Allow",
                "Action": [
                    "s3:ListBucket",
                    # SeaweedFS demands this action for ListObjectVersions (RC-331, row G7);
                    # MinIO already covers that call with s3:ListBucket, so it adds nothing
                    # there. Sits on the bucket ARN and not on *.
                    "s3:ListBucketVersions",
                    # Needed by applications such as Grist, which read the versioning state.
                    "s3:GetBucketVersioning",
                    "s3:GetBucketLocation",
                ],
                "Resource": [f"arn:aws:s3:::{bucket}"],
            }
        )

    actions: list[str] = []
    if "read" in permissions:
        actions.extend(["s3:GetObject", "s3:GetObjectVersion"])
    if "write" in permissions:
        actions.append("s3:PutObject")
    if "delete" in permissions:
        actions.append("s3:DeleteObject")

    if actions:
        statements.append({"Effect": "Allow", "Action": actions, "Resource": [f"arn:aws:s3:::{bucket}/*"]})

    return {"Version": "2012-10-17", "Statement": statements}
