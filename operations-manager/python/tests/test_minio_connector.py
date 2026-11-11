"""De naamvalidatie van de MinIO-connector.

Wat de connector tegen een echte MinIO doet, meet de groep ``minio_live``
(``tests/integration/test_minio_live.py``). Hier blijft alleen wat zonder server te meten
is: een naam die MinIO zou weigeren, weigert de connector zelf, voordat er een bucket of
gebruiker van komt.
"""

import pytest
from opi.connectors.minio_mc import MinioConnector, MinioValidationError


@pytest.mark.parametrize("name", ["test-bucket", "my-test-bucket-123"])
def test_a_valid_bucket_name_passes_unchanged(name: str) -> None:
    assert MinioConnector._validate_bucket_name(name) == name


@pytest.mark.parametrize("name", ["", "Test-Bucket", "test--bucket"])
def test_an_invalid_bucket_name_is_refused(name: str) -> None:
    with pytest.raises(MinioValidationError):
        MinioConnector._validate_bucket_name(name)


@pytest.mark.parametrize("name", ["testuser", "test_user_123"])
def test_a_valid_username_passes_unchanged(name: str) -> None:
    assert MinioConnector._validate_username(name) == name


def test_a_username_that_starts_with_a_digit_is_refused() -> None:
    with pytest.raises(MinioValidationError):
        MinioConnector._validate_username("123user")
