"""Config model for the ``health-check`` service.

Component-level config carried under a ``config:`` wrapper (like publish-on-web and
attachments), NOT inline like metrics-scraper. Every field is optional: an absent key
falls back to the platform default the generic code / template already provides
(scheme -> ``tcp``, port -> the application port, paths -> ``/``). The service only
overrides what the user actually fills in.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Same constraint the retired component ``probe`` block enforced on these paths in
# project_v2.json (and that all sibling manifest-bound strings still carry): an
# absolute path built only from safe URL characters. This is the guard that keeps a
# tenant-supplied path from injecting sibling YAML keys into the generated pod spec,
# where the value is interpolated unquoted.
PATH_PATTERN = r"^/[A-Za-z0-9/_.\-]*\Z"


class HealthCheckConfig(BaseModel):
    # python-re engine so the path ``\Z`` end-of-string anchor compiles (the default
    # rust-regex engine rejects ``\Z``) and behaves exactly like the pattern the
    # retired ``probe`` block enforced through project_v2.json's jsonschema validator.
    model_config = ConfigDict(extra="forbid", populate_by_name=True, regex_engine="python-re")

    scheme: Literal["none", "tcp", "http", "https"] | None = Field(
        default=None,
        description=(
            "How the component is probed: 'tcp' opens a connection, 'http'/'https' request a path, 'none' "
            "disables probing. Left out means tcp, or none when the component has no inbound port."
        ),
    )
    port: int | None = Field(
        default=None,
        ge=1024,
        le=65535,
        description=(
            "Port to probe; the component's first inbound port when left out. Must be 1024 or higher: images "
            "run non-root and cannot bind below that."
        ),
    )
    liveness_path: str | None = Field(
        default=None,
        alias="liveness-path",
        pattern=PATH_PATTERN,
        description=(
            "Path for the liveness and startup probes, '/' when left out. Only used with an http(s) scheme. "
            "Absolute, and limited to safe URL characters: it is interpolated into the generated pod spec."
        ),
    )
    readiness_path: str | None = Field(
        default=None,
        alias="readiness-path",
        pattern=PATH_PATTERN,
        description="Path for the readiness probe, '/' when left out. Only used with an http(s) scheme.",
    )
