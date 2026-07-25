"""Committed per-service JSON-schema fragments (RC-5 Phase 2).

Each configurable service's Pydantic ``config_model`` is the source of truth for
its config shape; this module renders that model to a committed, versioned JSON
Schema fragment under ``opi/schemas/services/<name>.v<version>.json``. Committing
the fragment (rather than generating it only at runtime) gives three things:

* a drift lock -- ``tests/test_service_config_schema.py`` fails CI if a model change
  is not reflected in its committed fragment, so schema and model can't silently
  diverge (the same guarantee the plan asks of ``project_v2.json``);
* a self-contained, independently versioned artifact per service, ready to be
  "lose getrokken" into a real plugin later;
* documentation / external-tooling consumption of a stable file.

The single ``render`` function is used by both the regeneration entrypoint
(``python -m opi.services.config_schema``) and the drift test, so the bytes written
and the bytes checked are produced the same way.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opi.services.catalog.base import Service

SERVICE_SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas" / "services"


def fragment_filename(service_name: str, version: str) -> str:
    return f"{service_name}.v{version}.json"


def fragment_path(service_name: str, version: str) -> Path:
    return SERVICE_SCHEMA_DIR / fragment_filename(service_name, version)


def render_service_config_schema(provider: Service) -> str:
    """Render a provider's config model to deterministic JSON-schema text.

    ``sort_keys`` + fixed indent + trailing newline make the output byte-stable
    across runs and Pydantic versions, so the committed file only changes when the
    model actually changes.
    """
    if provider.config_model is None:
        raise TypeError(f"Service '{provider.service_type.value}' has no config_model to render")
    schema = provider.config_model.model_json_schema()
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def write_all_service_config_schemas() -> list[Path]:
    """(Re)generate the committed fragment for every provider that has a config model."""
    # Imported here to avoid an import cycle (registry imports provider + config models).
    from opi.services.registry import SERVICES

    SERVICE_SCHEMA_DIR.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for provider in SERVICES.values():
        if provider.config_model is None:
            continue
        path = fragment_path(provider.service_type.value, provider.config_schema_version)
        path.write_text(render_service_config_schema(provider), encoding="utf-8")
        written.append(path)
    return written


if __name__ == "__main__":
    for path in write_all_service_config_schemas():
        print(f"wrote {path}")
