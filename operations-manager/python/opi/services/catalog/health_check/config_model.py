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

    # none | tcp | http | https. None -> the generic base default (tcp, or none when
    # the component has no inbound port the kubelet could reach).
    scheme: Literal["none", "tcp", "http", "https"] | None = None
    # Port to probe. None -> the component's first inbound port (application port).
    port: int | None = Field(default=None, ge=1, le=65535)
    # Path for the liveness and startup probes; ignored when scheme is tcp/none.
    # None -> the template default "/". The pattern matches every other manifest-bound
    # string in project_v2.json (match/rewrite/env-name): the value is interpolated
    # unquoted into the generated pod spec, so it must not carry YAML control chars.
    liveness_path: str | None = Field(default=None, alias="liveness-path", pattern=PATH_PATTERN)
    # Path for the readiness probe; ignored when scheme is tcp/none.
    # None -> the template default "/".
    readiness_path: str | None = Field(default=None, alias="readiness-path", pattern=PATH_PATTERN)
