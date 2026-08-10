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

import inspect
import json
from pathlib import Path
from typing import TYPE_CHECKING

from opi.services.catalog.base import ConfigLayer

if TYPE_CHECKING:
    from pydantic import BaseModel

    from opi.services.catalog.base import Service

#: Fallback location for services not yet migrated to a self-contained package.
SERVICE_SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas" / "services"


def fragment_filename(service_name: str, version: str) -> str:
    return f"{service_name}.v{version}.json"


def layer_fragment_filename(service_name: str, layer: ConfigLayer, version: str) -> str:
    """File name for the schema of ONE layer: ``<service>.<layer>.v<version>.json``."""
    return f"{service_name}.{layer.value}.v{version}.json"


def fragment_dir(provider: Service) -> Path:
    """Directory that holds a provider's committed schema fragment.

    A packaged service (``catalog/<svc>/__init__.py``) keeps its fragment beside its
    code, so everything a service needs lives in one place. A not-yet-migrated flat
    module (``catalog/<svc>.py``) falls back to the shared ``schemas/services`` dir.
    """
    module_file = Path(inspect.getfile(type(provider))).resolve()
    if module_file.name == "__init__.py":
        return module_file.parent
    return SERVICE_SCHEMA_DIR


def fragment_path(provider: Service) -> Path:
    version = provider.config_schema_version
    if version is None:
        msg = f"Service '{provider.service_type.value}' has no config_schema_version, so it has no schema fragment"
        raise ValueError(msg)
    return fragment_dir(provider) / fragment_filename(provider.service_type.value, version)


def layer_fragment_paths(provider: Service) -> dict[ConfigLayer, Path]:
    """The committed fragment per layer whose model DIFFERS from ``config_model``.

    "One service, one config shape" holds for most of the catalog, and for those services
    the single fragment above says everything. It does not hold for a service whose layers
    answer different questions -- publish-on-web (which domains may I use / how is this
    address composed / how is TLS terminated), postgresql-database, the storage pair,
    attachments. There the single fragment documents ONE of the shapes and silently omits
    the rest, which is how a client discovers a field list that does not apply to the target
    it is writing to.

    Only the differing layers get a file: for a service where every layer is
    ``config_model`` the extra files would be identical copies, and a copy that has to be
    kept in step is the thing this whole mechanism exists to avoid. The API already answers
    per layer (``config_model_for`` drives the generated request bodies); these files are
    the committed, reviewable record of the same thing.
    """
    version = provider.config_schema_version
    if version is None:
        return {}
    directory = fragment_dir(provider)
    name = provider.service_type.value
    paths: dict[ConfigLayer, Path] = {}
    for layer in ConfigLayer:
        model = provider.config_model_for(layer)
        if model is None or model is provider.config_model:
            continue
        paths[layer] = directory / layer_fragment_filename(name, layer, version)
    return paths


def render_model_schema(model: type[BaseModel]) -> str:
    """Render one Pydantic model to deterministic JSON-schema text.

    ``sort_keys`` + fixed indent + trailing newline make the output byte-stable across runs
    and Pydantic versions, so a committed file only changes when the model actually changes.
    """
    return json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n"


def render_service_config_schema(provider: Service) -> str:
    """Render a provider's config model to deterministic JSON-schema text.

    ``sort_keys`` + fixed indent + trailing newline make the output byte-stable
    across runs and Pydantic versions, so the committed file only changes when the
    model actually changes.
    """
    if provider.config_model is None:
        raise TypeError(f"Service '{provider.service_type.value}' has no config_model to render")
    return render_model_schema(provider.config_model)


def write_all_service_config_schemas() -> list[Path]:
    """(Re)generate the committed fragment for every provider that has a config model."""
    # Imported here to avoid an import cycle (registry imports provider + config models).
    from opi.services.registry import SERVICES

    written: list[Path] = []
    for provider in SERVICES.values():
        if provider.config_model is None:
            continue
        path = fragment_path(provider)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_service_config_schema(provider), encoding="utf-8")
        written.append(path)
        for layer, layer_path in layer_fragment_paths(provider).items():
            model = provider.config_model_for(layer)
            if model is None:  # unreachable: layer_fragment_paths only yields layers with one
                continue
            layer_path.write_text(render_model_schema(model), encoding="utf-8")
            written.append(layer_path)
    return written


if __name__ == "__main__":
    for path in write_all_service_config_schemas():
        print(f"wrote {path}")
