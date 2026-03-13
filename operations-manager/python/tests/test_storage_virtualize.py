"""Test that virtualized service config survives process_json_submission.

Covers two virtualize patterns:
- Sequence: persistent-storage/config, temp-storage/config
- Non-sequence: metrics-scraper/port, metrics-scraper/path

Reproduces bugs where the final wizard submit loses config even though
the summary shows it correctly.
"""

import copy
from typing import Any

import pytest
from opi.forms.editables.processor import EditableFormProcessor
from opi.forms.visualizers.fields.components import COMPONENTS_SEQUENCE

# The merged yaml_data as it would look when the summary is shown correctly.
# This is the data BEFORE process_json_submission in the final submit step.
MERGED_DATA_ONE_COMPONENT: dict[str, Any] = {
    "components": [
        {
            "name": "frontend",
            "image": "frontend",
            "resources": {"memory": {"request": "256Mi", "limit": "512Mi"}},
            "ports": {"inbound": [8080], "outbound": []},
            "services": [
                "publish-on-web",
                {"persistent-storage": {"config": [{"name": "data", "size": "100Mi", "mount-path": "/data"}]}},
                {"temp-storage": {"config": [{"name": "tmp", "size": "100Mi", "mount-path": "/tmp"}]}},
            ],
            "path": "/",
        },
    ],
}

MERGED_DATA_TWO_COMPONENTS: dict[str, Any] = {
    "components": [
        {
            "name": "frontend",
            "image": "frontend",
            "resources": {"memory": {"request": "256Mi", "limit": "512Mi"}},
            "ports": {"inbound": [8080], "outbound": []},
            "services": ["publish-on-web"],
            "path": "/",
        },
        {
            "name": "backend",
            "image": "backend",
            "resources": {"memory": {"request": "256Mi", "limit": "512Mi"}},
            "ports": {"inbound": [8080], "outbound": []},
            "services": [
                "publish-on-web",
                {"persistent-storage": {"config": [{"name": "data", "size": "100Mi", "mount-path": "/data"}]}},
                {"temp-storage": {"config": [{"name": "tmp", "size": "100Mi", "mount-path": "/tmp"}]}},
            ],
            "path": "/",
        },
    ],
}

# What the POST data from the form looks like (services as flat list + _services-config).
POST_DATA_ONE_COMPONENT: dict[str, Any] = {
    "components": [
        {
            "name": "frontend",
            "image": "frontend",
            "resources": {"memory": {"request": "256Mi", "limit": "512Mi"}},
            "ports": {"inbound": ["8080"]},
            "services": ["publish-on-web", "persistent-storage", "temp-storage"],
            "path": "/",
            "_services-config": [
                {"persistent-storage": {"config": [{"name": "data", "size": "100Mi", "mount-path": "/data"}]}},
                {"temp-storage": {"config": [{"name": "tmp", "size": "100Mi", "mount-path": "/tmp"}]}},
            ],
        },
    ],
}

POST_DATA_TWO_COMPONENTS: dict[str, Any] = {
    "components": [
        {
            "name": "frontend",
            "image": "frontend",
            "resources": {"memory": {"request": "256Mi", "limit": "512Mi"}},
            "ports": {"inbound": ["8080"]},
            "services": "publish-on-web",
            "path": "/",
        },
        {
            "name": "backend",
            "image": "backend",
            "resources": {"memory": {"request": "256Mi", "limit": "512Mi"}},
            "ports": {"inbound": ["8080"]},
            "services": ["publish-on-web", "persistent-storage", "temp-storage"],
            "path": "/",
            "_services-config": [
                {"persistent-storage": {"config": [{"name": "data", "size": "100Mi", "mount-path": "/data"}]}},
                {"temp-storage": {"config": [{"name": "tmp", "size": "100Mi", "mount-path": "/tmp"}]}},
            ],
        },
    ],
}


def _find_service_config(services: list, service_name: str) -> list | None:
    """Find the config list for a named service in a mixed services list."""
    for svc in services:
        if isinstance(svc, dict) and service_name in svc:
            return svc[service_name].get("config")
    return None


def _find_service_value(services: list, service_name: str, key: str) -> Any:
    """Find a scalar value for a named service in a mixed services list."""
    for svc in services:
        if isinstance(svc, dict) and service_name in svc:
            cfg = svc[service_name]
            if isinstance(cfg, dict):
                return cfg.get(key)
    return None


# --- Metrics test data ---

MERGED_DATA_WITH_METRICS: dict[str, Any] = {
    "components": [
        {
            "name": "frontend",
            "image": "frontend",
            "resources": {"memory": {"request": "256Mi", "limit": "512Mi"}},
            "ports": {"inbound": [8080], "outbound": []},
            "services": [
                "publish-on-web",
                {"metrics-scraper": {"port": 8080, "path": "/metrics"}},
            ],
            "path": "/",
        },
    ],
}

POST_DATA_WITH_METRICS: dict[str, Any] = {
    "components": [
        {
            "name": "frontend",
            "image": "frontend",
            "resources": {"memory": {"request": "256Mi", "limit": "512Mi"}},
            "ports": {"inbound": ["8080"]},
            "services": ["publish-on-web", "metrics-scraper"],
            "path": "/",
            "_services-config": [
                {"metrics-scraper": {"port": "8080", "path": "/metrics"}},
            ],
        },
    ],
}

POST_DATA_TWO_COMPONENTS_STORAGE_AND_METRICS: dict[str, Any] = {
    "components": [
        {
            "name": "frontend",
            "image": "frontend",
            "resources": {"memory": {"request": "256Mi", "limit": "512Mi"}},
            "ports": {"inbound": ["8080"]},
            "services": ["publish-on-web", "metrics-scraper"],
            "path": "/",
            "_services-config": [
                {"metrics-scraper": {"port": "8080", "path": "/metrics"}},
            ],
        },
        {
            "name": "backend",
            "image": "backend",
            "resources": {"memory": {"request": "256Mi", "limit": "512Mi"}},
            "ports": {"inbound": ["8080"]},
            "services": ["publish-on-web", "metrics-scraper", "persistent-storage", "temp-storage"],
            "path": "/",
            "_services-config": [
                {"persistent-storage": {"config": [{"name": "dataaa", "size": "100Mi", "mount-path": "/data"}]}},
                {"temp-storage": {"config": [{"name": "tmpaa", "size": "100Mi", "mount-path": "/tmp"}]}},
                {"metrics-scraper": {"port": "8080", "path": "/metrics"}},
            ],
        },
    ],
}


class TestStorageVirtualizeFinalSubmit:
    """Test that storage config survives the final submit processing."""

    @pytest.mark.asyncio
    async def test_merged_data_one_component_preserves_storage(self, mock_settings: Any) -> None:
        """When merged data already has storage config in services,
        process_json_submission should preserve it."""
        processor = EditableFormProcessor()
        data = copy.deepcopy(MERGED_DATA_ONE_COMPONENT)

        result, errors = await processor.process_json_submission(
            data,
            [COMPONENTS_SEQUENCE],
            data,
            strip_transients=True,
        )

        assert not errors
        services = result["components"][0]["services"]
        persistent_config = _find_service_config(services, "persistent-storage")
        assert persistent_config is not None, f"persistent-storage config missing. services={services}"
        assert persistent_config[0]["name"] == "data"
        assert persistent_config[0]["size"] == "100Mi"

        temp_config = _find_service_config(services, "temp-storage")
        assert temp_config is not None, f"temp-storage config missing. services={services}"
        assert temp_config[0]["name"] == "tmp"

        # _services-config must NOT leak into the output
        assert "_services-config" not in result["components"][0]

    @pytest.mark.asyncio
    async def test_post_data_one_component_writes_storage(self, mock_settings: Any) -> None:
        """When POST data has _services-config (virtual path), the processor
        should write storage config into services and remove _services-config."""
        processor = EditableFormProcessor()
        submitted = copy.deepcopy(POST_DATA_ONE_COMPONENT)
        yaml_data = copy.deepcopy(POST_DATA_ONE_COMPONENT)

        result, errors = await processor.process_json_submission(
            submitted,
            [COMPONENTS_SEQUENCE],
            yaml_data,
            strip_transients=True,
        )

        assert not errors
        services = result["components"][0]["services"]
        persistent_config = _find_service_config(services, "persistent-storage")
        assert persistent_config is not None, f"persistent-storage config missing. services={services}"
        assert persistent_config[0]["name"] == "data"
        assert persistent_config[0]["size"] == "100Mi"

        temp_config = _find_service_config(services, "temp-storage")
        assert temp_config is not None, f"temp-storage config missing. services={services}"
        assert temp_config[0]["name"] == "tmp"

        assert "_services-config" not in result["components"][0]

    @pytest.mark.asyncio
    async def test_two_components_second_has_storage(self, mock_settings: Any) -> None:
        """With two components where only the second has storage,
        the storage config should be written to the correct component."""
        processor = EditableFormProcessor()
        submitted = copy.deepcopy(POST_DATA_TWO_COMPONENTS)
        yaml_data = copy.deepcopy(POST_DATA_TWO_COMPONENTS)

        result, errors = await processor.process_json_submission(
            submitted,
            [COMPONENTS_SEQUENCE],
            yaml_data,
            strip_transients=True,
        )

        assert not errors

        # Frontend should NOT have storage
        frontend_services = result["components"][0]["services"]
        assert _find_service_config(frontend_services, "persistent-storage") is None
        assert "_services-config" not in result["components"][0]

        # Backend SHOULD have storage
        backend_services = result["components"][1]["services"]
        persistent_config = _find_service_config(backend_services, "persistent-storage")
        assert persistent_config is not None, f"persistent-storage config missing. services={backend_services}"
        assert persistent_config[0]["name"] == "data"
        assert persistent_config[0]["size"] == "100Mi"

        temp_config = _find_service_config(backend_services, "temp-storage")
        assert temp_config is not None, f"temp-storage config missing. services={backend_services}"
        assert temp_config[0]["name"] == "tmp"

        assert "_services-config" not in result["components"][1]

    @pytest.mark.asyncio
    async def test_merged_data_two_components_preserves_storage(self, mock_settings: Any) -> None:
        """When merged data already has storage embedded in services (no
        _services-config), the processor should preserve it."""
        processor = EditableFormProcessor()
        data = copy.deepcopy(MERGED_DATA_TWO_COMPONENTS)

        result, errors = await processor.process_json_submission(
            data,
            [COMPONENTS_SEQUENCE],
            data,
            strip_transients=True,
        )

        assert not errors

        backend_services = result["components"][1]["services"]
        persistent_config = _find_service_config(backend_services, "persistent-storage")
        assert persistent_config is not None, f"persistent-storage config missing. services={backend_services}"
        assert persistent_config[0]["name"] == "data"

        temp_config = _find_service_config(backend_services, "temp-storage")
        assert temp_config is not None, f"temp-storage config missing. services={backend_services}"
        assert temp_config[0]["name"] == "tmp"

        assert "_services-config" not in result["components"][1]


class TestMetricsVirtualizeFinalSubmit:
    """Test that metrics-scraper config (non-sequence virtualize) survives processing."""

    @pytest.mark.asyncio
    async def test_merged_data_preserves_metrics(self, mock_settings: Any) -> None:
        """When merged data has metrics config in services, preserve it."""
        processor = EditableFormProcessor()
        data = copy.deepcopy(MERGED_DATA_WITH_METRICS)

        result, errors = await processor.process_json_submission(
            data,
            [COMPONENTS_SEQUENCE],
            data,
            strip_transients=True,
        )

        assert not errors
        services = result["components"][0]["services"]
        port = _find_service_value(services, "metrics-scraper", "port")
        assert port is not None, f"metrics-scraper port missing. services={services}"
        assert port == 8080

        path = _find_service_value(services, "metrics-scraper", "path")
        assert path == "/metrics"

        assert "_services-config" not in result["components"][0]

    @pytest.mark.asyncio
    async def test_post_data_writes_metrics(self, mock_settings: Any) -> None:
        """When POST data has metrics in _services-config, write to services."""
        processor = EditableFormProcessor()
        submitted = copy.deepcopy(POST_DATA_WITH_METRICS)
        yaml_data = copy.deepcopy(POST_DATA_WITH_METRICS)

        result, errors = await processor.process_json_submission(
            submitted,
            [COMPONENTS_SEQUENCE],
            yaml_data,
            strip_transients=True,
        )

        assert not errors
        services = result["components"][0]["services"]
        port = _find_service_value(services, "metrics-scraper", "port")
        assert port is not None, f"metrics-scraper port missing. services={services}"

        path = _find_service_value(services, "metrics-scraper", "path")
        assert path is not None, f"metrics-scraper path missing. services={services}"

        assert "_services-config" not in result["components"][0]

    @pytest.mark.asyncio
    async def test_two_components_storage_and_metrics(self, mock_settings: Any) -> None:
        """Both storage (sequence) and metrics (non-sequence) virtualize work together."""
        processor = EditableFormProcessor()
        submitted = copy.deepcopy(POST_DATA_TWO_COMPONENTS_STORAGE_AND_METRICS)
        yaml_data = copy.deepcopy(POST_DATA_TWO_COMPONENTS_STORAGE_AND_METRICS)

        result, errors = await processor.process_json_submission(
            submitted,
            [COMPONENTS_SEQUENCE],
            yaml_data,
            strip_transients=True,
        )

        assert not errors

        # Frontend: metrics only
        frontend_services = result["components"][0]["services"]
        frontend_port = _find_service_value(frontend_services, "metrics-scraper", "port")
        assert frontend_port is not None, f"frontend metrics-scraper port missing. services={frontend_services}"
        assert "_services-config" not in result["components"][0]

        # Backend: storage + metrics
        backend_services = result["components"][1]["services"]
        persistent_config = _find_service_config(backend_services, "persistent-storage")
        assert persistent_config is not None, f"persistent-storage config missing. services={backend_services}"
        assert persistent_config[0]["name"] == "dataaa"

        temp_config = _find_service_config(backend_services, "temp-storage")
        assert temp_config is not None, f"temp-storage config missing. services={backend_services}"
        assert temp_config[0]["name"] == "tmpaa"

        backend_port = _find_service_value(backend_services, "metrics-scraper", "port")
        assert backend_port is not None, f"backend metrics-scraper port missing. services={backend_services}"

        assert "_services-config" not in result["components"][1]
