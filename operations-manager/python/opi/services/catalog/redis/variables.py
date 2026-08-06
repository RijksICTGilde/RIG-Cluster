"""Environment variables the redis service provides -- single source of truth.

Lives in the service's own package (RC-36): what a service hands to a deployment
is part of that service, not of a shared module.
"""

from enum import Enum

from opi.services.services import VariableDefinition


class RedisVariables(Enum):
    """Redis cache service variable definitions - single source of truth."""

    HOST = VariableDefinition(
        name="REDIS_HOST",
        description="Redis server hostname",
        source="secret",
        secret_key="host",
        aliases=["APP_REDIS_HOST"],
    )
    PORT = VariableDefinition(
        name="REDIS_PORT",
        description="Redis server port",
        source="secret",
        secret_key="port",
        aliases=["APP_REDIS_PORT"],
    )
    USERNAME = VariableDefinition(
        name="REDIS_USERNAME",
        description="Redis ACL username",
        source="secret",
        secret_key="username",
        aliases=["APP_REDIS_USERNAME"],
    )
    PASSWORD = VariableDefinition(
        name="REDIS_PASSWORD",
        description="Redis password",
        source="secret",
        secret_key="password",
        aliases=["APP_REDIS_PASSWORD"],
    )
    PREFIX = VariableDefinition(
        name="REDIS_PREFIX",
        description=(
            "Prefix voor Redis-sleutels en -kanalen van dit project, zonder scheidingsteken. "
            "Bouw je sleutels en kanalen als <prefix>:<naam>. De ACL geeft alleen toegang "
            "tot sleutels en kanalen die met <prefix>: beginnen"
        ),
        source="secret",
        secret_key="key_prefix",
        aliases=["APP_REDIS_PREFIX"],
    )
    URL = VariableDefinition(
        name="REDIS_URL",
        description="Full Redis connection URL",
        source="secret",
        secret_key="url",
        aliases=["APP_REDIS_URL"],
    )
