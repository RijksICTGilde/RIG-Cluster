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


class HealthCheckConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # none | tcp | http | https. None -> the generic base default (tcp, or none when
    # the component has no inbound port the kubelet could reach).
    scheme: Literal["none", "tcp", "http", "https"] | None = None
    # Port to probe. None -> the component's first inbound port (application port).
    port: int | None = None
    # Path for the liveness and startup probes; ignored when scheme is tcp/none.
    # None -> the template default "/".
    liveness_path: str | None = Field(default=None, alias="liveness-path")
    # Path for the readiness probe; ignored when scheme is tcp/none.
    # None -> the template default "/".
    readiness_path: str | None = Field(default=None, alias="readiness-path")
