"""
Tests for opi.utils.secrets module.

Tests the pure and semi-pure aspects of secret dataclasses including
serialization, deserialization, property computation, and secret naming.
"""

import base64
import json

from opi.utils.secrets import (
    DatabaseSecret,
    KeycloakSecret,
    MinIOSecret,
    RedisSecret,
    RegistrySecret,
    UserSecret,
)


class TestRegistrySecret:
    """Tests for RegistrySecret serialization and round-trip."""

    def test_to_dockerconfigjson_structure(self):
        """to_dockerconfigjson returns valid JSON with auths and base64 auth."""
        secret = RegistrySecret(
            registry_url="registry.example.com",
            username="myuser",
            password="mypass",
        )
        result = json.loads(secret.to_dockerconfigjson())

        assert "auths" in result
        assert "registry.example.com" in result["auths"]
        assert "auth" in result["auths"]["registry.example.com"]

    def test_to_dockerconfigjson_auth_encoding(self):
        """to_dockerconfigjson base64-encodes username:password."""
        secret = RegistrySecret(
            registry_url="registry.example.com",
            username="myuser",
            password="mypass",
        )
        result = json.loads(secret.to_dockerconfigjson())
        auth_b64 = result["auths"]["registry.example.com"]["auth"]
        decoded = base64.b64decode(auth_b64).decode()

        assert decoded == "myuser:mypass"

    def test_to_dockerconfigjson_special_characters_in_password(self):
        """to_dockerconfigjson handles passwords with colons and special chars."""
        secret = RegistrySecret(
            registry_url="registry.example.com",
            username="user",
            password="p@ss:w0rd!",
        )
        result = json.loads(secret.to_dockerconfigjson())
        auth_b64 = result["auths"]["registry.example.com"]["auth"]
        decoded = base64.b64decode(auth_b64).decode()

        assert decoded == "user:p@ss:w0rd!"

    def test_to_k8s_secret_data_has_dockerconfigjson_key(self):
        """to_k8s_secret_data returns dict with .dockerconfigjson key."""
        secret = RegistrySecret(
            registry_url="registry.example.com",
            username="myuser",
            password="mypass",
        )
        data = secret.to_k8s_secret_data()

        assert ".dockerconfigjson" in data
        assert len(data) == 1

    def test_round_trip(self):
        """Creating a RegistrySecret, serializing, and deserializing yields the same values."""
        original = RegistrySecret(
            registry_url="ghcr.io",
            username="bot-user",
            password="gh_token_abc123",
        )
        k8s_data = original.to_k8s_secret_data()
        restored = RegistrySecret.from_k8s_secret_data(k8s_data)

        assert restored.registry_url == original.registry_url
        assert restored.username == original.username
        assert restored.password == original.password

    def test_from_k8s_secret_data_empty_auths_raises(self):
        """from_k8s_secret_data raises ValueError when auths is empty."""
        import pytest

        with pytest.raises(ValueError, match="No registry found"):
            RegistrySecret.from_k8s_secret_data({".dockerconfigjson": '{"auths": {}}'})

    def test_from_k8s_secret_data_missing_key_raises(self):
        """from_k8s_secret_data raises ValueError when .dockerconfigjson has no auths."""
        import pytest

        with pytest.raises(ValueError, match="No registry found"):
            RegistrySecret.from_k8s_secret_data({".dockerconfigjson": "{}"})

    def test_from_k8s_secret_data_legacy_username_password(self):
        """from_k8s_secret_data supports legacy username/password fields without auth."""
        config = json.dumps({
            "auths": {
                "docker.io": {
                    "username": "legacyuser",
                    "password": "legacypass",
                }
            }
        })
        secret = RegistrySecret.from_k8s_secret_data({".dockerconfigjson": config})

        assert secret.registry_url == "docker.io"
        assert secret.username == "legacyuser"
        assert secret.password == "legacypass"


class TestDatabaseSecretProperties:
    """Tests for DatabaseSecret computed properties."""

    def test_connection_string_format(self):
        """connection_string returns a properly formatted PostgreSQL URI."""
        secret = DatabaseSecret(
            host="db.example.com",
            port=5432,
            username="admin",
            password="secret",
            database="mydb",
            schema="public",
        )
        expected = "postgresql://admin:secret@db.example.com:5432/mydb?options=--search_path%3Dpublic"
        assert secret.connection_string == expected

    def test_connection_string_with_special_chars(self):
        """connection_string includes special characters in password as-is."""
        secret = DatabaseSecret(
            host="localhost",
            port=5432,
            username="user",
            password="p@ss!word",
            database="testdb",
            schema="app",
        )
        assert "p@ss!word" in secret.connection_string
        assert secret.connection_string.startswith("postgresql://")
        assert "search_path%3Dapp" in secret.connection_string

    def test_connection_string_custom_port(self):
        """connection_string uses the configured port."""
        secret = DatabaseSecret(
            host="db.local",
            port=15432,
            username="u",
            password="p",
            database="d",
            schema="s",
        )
        assert ":15432/" in secret.connection_string


class TestMinIOSecretProperties:
    """Tests for MinIOSecret computed properties."""

    def test_url_format(self):
        """url property returns host:port."""
        secret = MinIOSecret(
            host="minio.example.com",
            port=9000,
            access_key="AKID",
            secret_key="SKEY",
            bucket_name="mybucket",
        )
        assert secret.url == "minio.example.com:9000"

    def test_endpoint_url_format(self):
        """endpoint_url property returns http://host:port."""
        secret = MinIOSecret(
            host="minio.example.com",
            port=9000,
            access_key="AKID",
            secret_key="SKEY",
            bucket_name="mybucket",
        )
        assert secret.endpoint_url == "http://minio.example.com:9000"

    def test_default_region(self):
        """MinIOSecret defaults region to us-east-1."""
        secret = MinIOSecret(
            host="h",
            port=9000,
            access_key="a",
            secret_key="s",
            bucket_name="b",
        )
        assert secret.region == "us-east-1"

    def test_custom_region(self):
        """MinIOSecret accepts a custom region."""
        secret = MinIOSecret(
            host="h",
            port=9000,
            access_key="a",
            secret_key="s",
            bucket_name="b",
            region="eu-west-1",
        )
        assert secret.region == "eu-west-1"


class TestRedisSecretProperties:
    """Tests for RedisSecret computed properties."""

    def test_url_format(self):
        """url property returns redis://:password@host:port/0."""
        secret = RedisSecret(
            host="redis.example.com",
            port=6379,
            password="redispass",
        )
        assert secret.url == "redis://:redispass@redis.example.com:6379/0"

    def test_url_with_special_password(self):
        """url property includes special characters in password."""
        secret = RedisSecret(
            host="localhost",
            port=6380,
            password="p@ss:w0rd",
        )
        assert secret.url == "redis://:p@ss:w0rd@localhost:6380/0"


class TestUserSecret:
    """Tests for UserSecret serialization and deserialization."""

    def test_to_k8s_secret_data_returns_copy(self):
        """to_k8s_secret_data returns a copy of env_vars."""
        env = {"FOO": "bar", "BAZ": "qux"}
        secret = UserSecret(env_vars=env)
        data = secret.to_k8s_secret_data()

        assert data == env
        # Must be a copy, not the same object
        assert data is not secret.env_vars

    def test_from_k8s_secret_data(self):
        """from_k8s_secret_data creates UserSecret with env_vars from input."""
        input_data = {"KEY1": "val1", "KEY2": "val2"}
        secret = UserSecret.from_k8s_secret_data(input_data)

        assert secret.env_vars == input_data
        # Must be a copy
        assert secret.env_vars is not input_data

    def test_round_trip(self):
        """UserSecret round-trips through to_k8s_secret_data and from_k8s_secret_data."""
        original = UserSecret(env_vars={"APP_KEY": "value123", "DEBUG": "true"})
        restored = UserSecret.from_k8s_secret_data(original.to_k8s_secret_data())

        assert restored.env_vars == original.env_vars

    def test_empty_env_vars(self):
        """UserSecret handles empty env_vars dict."""
        secret = UserSecret(env_vars={})
        data = secret.to_k8s_secret_data()

        assert data == {}


class TestGetSecretName:
    """Tests for BaseSecret.get_secret_name across all subclasses."""

    def test_database_secret_name(self):
        """DatabaseSecret generates {prefix}-database name."""
        assert DatabaseSecret.get_secret_name("myapp") == "myapp-database"

    def test_minio_secret_name(self):
        """MinIOSecret generates {prefix}-minio name."""
        assert MinIOSecret.get_secret_name("myapp") == "myapp-minio"

    def test_keycloak_secret_name(self):
        """KeycloakSecret generates {prefix}-keycloak name."""
        assert KeycloakSecret.get_secret_name("myapp") == "myapp-keycloak"

    def test_redis_secret_name(self):
        """RedisSecret generates {prefix}-redis name."""
        assert RedisSecret.get_secret_name("myapp") == "myapp-redis"

    def test_user_secret_name(self):
        """UserSecret generates {prefix}-user name."""
        assert UserSecret.get_secret_name("myapp") == "myapp-user"

    def test_secret_name_with_complex_prefix(self):
        """get_secret_name works with multi-segment prefixes."""
        assert DatabaseSecret.get_secret_name("project-component") == "project-component-database"
        assert RedisSecret.get_secret_name("my-app-v2") == "my-app-v2-redis"


class TestFieldConversion:
    """Tests for _convert_field_value on subclasses with port fields."""

    def test_database_port_conversion(self):
        """DatabaseSecret converts port string to int."""
        assert DatabaseSecret._convert_field_value("port", "5432") == 5432

    def test_database_non_port_passthrough(self):
        """DatabaseSecret passes non-port fields through as strings."""
        assert DatabaseSecret._convert_field_value("host", "localhost") == "localhost"

    def test_minio_port_conversion(self):
        """MinIOSecret converts port string to int."""
        assert MinIOSecret._convert_field_value("port", "9000") == 9000

    def test_redis_port_conversion(self):
        """RedisSecret converts port string to int."""
        assert RedisSecret._convert_field_value("port", "6379") == 6379
