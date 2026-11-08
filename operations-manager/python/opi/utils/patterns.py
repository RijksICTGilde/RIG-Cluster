"""The name patterns shared by the edge of the API and the service config.

A pattern belongs in one place: the moment the same rule sits in two files as a regex, it
means two things sooner or later. ``DNS1123_LABEL`` lived in the ``cross_domain_access``
package, which needed it first; it moved here when the restore endpoints turned out to need
the same pattern (RC-339).

The patterns are anchored, so they mean the same thing in a ``pydantic`` ``pattern=`` as in
``re.match``.
"""

#: A DNS-1123 label: what Kubernetes accepts as the name of a namespace or a label.
DNS1123_LABEL = r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"

#: A DNS-1123 subdomain: what Kubernetes accepts as the name of an object (PVC,
#: StorageClass). One or more labels separated by dots.
DNS1123_SUBDOMAIN = r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$"

#: A Kubernetes quantity such as 500Mi, 1Gi or 262144k. The same shape that
#: ``parse_k8s_memory_to_mi`` parses; as a pattern here, because the edge has to be able to
#: refuse a value before anything is done with it.
KUBERNETES_QUANTITY = r"^\d+(\.\d+)?(Ki|Mi|Gi|Ti|Pi|k|M|G|T|P)?$"

#: A PostgreSQL identifier, for a schema or user name.
#: ``PostgresConnector._validate_identifier`` reads it here.
POSTGRES_IDENTIFIER = r"^[a-zA-Z_][a-zA-Z0-9_$]*$"

#: A schema name a clone may carry over from a source database (RC-360). Stricter than
#: ``POSTGRES_IDENTIFIER``: the ``psql -c`` statements of the clone do not quote, so an
#: uppercase letter or a ``$`` does not get through there.
POSTGRES_CLONEABLE_SCHEMA = r"^[a-z_][a-z0-9_]*$"

#: An S3 bucket name by the AWS rules: 3 to 63 characters, lowercase letters, digits,
#: hyphens and dots, starting and ending on a letter or a digit.
S3_BUCKET_NAME = r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$"

#: A host name or IP address, optionally with a port. Uppercase is allowed because a DNS name
#: is case insensitive; the port because a target host is supplied that way
#: (``postgresql.elsewhere:5432``), next to the separate port field.
HOST_WITH_OPTIONAL_PORT = (
    r"^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)*(:\d{1,5})?$"
)

#: An endpoint as a URL or as host:port, the way a MinIO endpoint is given.
ENDPOINT_URL_OR_HOST_PORT = (
    r"^(https?://)?[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)*"
    r"(:\d{1,5})?(/[A-Za-z0-9._~\-/]*)?$"
)

#: A kopia snapshot id: a hexadecimal key with a letter prefix (``k1f2a...``). Set wider than
#: that, because the id comes from kopia and the edge should not describe it more narrowly
#: than it takes to refuse text with newlines and quotes.
KOPIA_SNAPSHOT_ID = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
