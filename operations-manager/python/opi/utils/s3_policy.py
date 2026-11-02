"""Het policy-document dat een identiteit toegang geeft tot een bucket, en niets anders.

Eén plek voor twee object-storage-services (RC-331 besluit 3): `minio-storage` en
`seaweedfs-storage` delen de isolatie, dus ze delen het document. Wijkt het per service,
dan wijkt de isolatie per service.
"""

from __future__ import annotations

from typing import Any

#: De rechten die de managers altijd samen vragen. Een losse keuze bestaat nergens in de
#: codebase; het argument blijft omdat `MinioConnector.grant_bucket_access` hem publiek
#: aanbiedt.
ALL_BUCKET_PERMISSIONS = ["read", "write", "delete", "list"]


def bucket_access_policy(bucket: str, permissions: list[str]) -> dict[str, Any]:
    """Het document voor ``bucket``, met de acties die ``permissions`` toestaat.

    Op de bucket-ARN alleen opsommen en lezen van bucket-eigenschappen, op de objecten de
    datarechten. Geen ``s3:ListAllMyBuckets``: wie deze policy heeft, ziet de andere
    buckets niet.
    """
    statements: list[dict[str, Any]] = []

    if "list" in permissions or "read" in permissions:
        statements.append(
            {
                "Effect": "Allow",
                "Action": [
                    "s3:ListBucket",
                    # SeaweedFS eist deze actie voor ListObjectVersions (RC-331, rij G7);
                    # MinIO dekt die aanroep al met s3:ListBucket, dus daar geeft hij niets
                    # nieuws. Staat op de bucket-ARN en niet op *.
                    "s3:ListBucketVersions",
                    # Nodig voor applicaties als Grist, die de versioning-stand uitlezen.
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
