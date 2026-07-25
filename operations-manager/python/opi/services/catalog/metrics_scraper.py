"""metrics-scraper service."""

from __future__ import annotations

import logging

from opi.core.config import settings
from opi.services.catalog.base import ManifestContext, ManifestContribution, SecretFileSpec, Service
from opi.services.config_models.metrics_scraper import MetricsScraperConfig
from opi.services.services import service_entry_config, service_entry_name
from opi.services.services_enums import ServiceType
from opi.utils.secrets import MetricsAuthSecret

logger = logging.getLogger(__name__)


class MetricsScraperService(Service):
    service_type = ServiceType.METRICS_SCRAPER
    config_model = MetricsScraperConfig
    config_schema_version = "1.0"
    manifest_secret_class = MetricsAuthSecret
    manifest_order = 50

    def contribute_manifest_context(self, ctx: ManifestContext) -> ManifestContribution:
        # Contribute the scrape port/path as a template var so the deployment's
        # prometheus.io annotations point at the app's real metrics endpoint. The
        # template falls back to the application port / "/metrics" when either is None.
        contribution = super().contribute_manifest_context(ctx)  # envFrom + secret file
        port, path = None, None
        for service_item in (ctx.component_def or {}).get("services", []):
            if service_entry_name(service_item) == ServiceType.METRICS_SCRAPER.value:
                config = service_entry_config(service_item)
                if isinstance(config, dict):
                    port, path = config.get("port"), config.get("path")
                break
        contribution.template_vars["metrics_config"] = {"port": port, "path": path}
        return contribution

    def build_secret_files(self, ctx: ManifestContext) -> list[SecretFileSpec]:
        token = settings.PROMETHEUS_METRICS_AUTH_TOKEN
        if not token:
            logger.warning(
                f"Deployment '{ctx.deployment_name}' uses metrics-scraper but "
                f"PROMETHEUS_METRICS_AUTH_TOKEN is not configured"
            )
            return []
        return [
            SecretFileSpec(
                secret_name=MetricsAuthSecret.get_secret_name(ctx.deployment_name),
                secret_pairs=MetricsAuthSecret(token=token).to_k8s_secret_data(),
                secret_type="metrics",
            )
        ]
