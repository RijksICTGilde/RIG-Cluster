"""redis service (shared cache)."""

from __future__ import annotations

import logging

from opi.services.catalog.base import ManifestContext, ProvisionContext, SecretFileSpec, Service
from opi.services.catalog.redis.config_model import RedisConfig
from opi.services.services_enums import ServiceType
from opi.utils.secrets import RedisSecret

logger = logging.getLogger(__name__)


class RedisService(Service):
    service_type = ServiceType.REDIS
    config_model = RedisConfig
    config_schema_version = "1.0"
    cleanup_manager_key = "redis"
    provision_order = 40
    manifest_secret_class = RedisSecret
    manifest_order = 40
    # Shared service: fires for both the shared and namespace redis variant.
    manifest_activated_by = (ServiceType.REDIS, ServiceType.NAMESPACE_REDIS)

    async def provision(self, ctx: ProvisionContext) -> None:
        # redis_manager handles both the shared and namespace redis variants.
        await ctx.redis_manager.create_resources_for_deployment(ctx.project_data, ctx.deployment)

    def build_secret_files(self, ctx: ManifestContext) -> list[SecretFileSpec]:
        creds = ctx.get_secret(ctx.deployment_name, "redis", RedisSecret)
        if creds is None:
            logger.warning(f"Deployment '{ctx.deployment_name}' uses Redis but no cache credentials found")
            return []
        secret = RedisSecret(
            host=creds.host,
            port=creds.port,
            username=creds.username,
            password=creds.password,
            key_prefix=creds.key_prefix,
        )
        return [
            SecretFileSpec(
                secret_name=RedisSecret.get_secret_name(ctx.deployment_name),
                secret_pairs=secret.to_k8s_secret_data(),
                secret_type="redis",
                resolve_aliases=True,
            )
        ]
