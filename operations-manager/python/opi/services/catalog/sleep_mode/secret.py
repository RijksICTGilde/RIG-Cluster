"""The waker pod's token Secret.

Rendered as a SOPS secret into the deployment namespace and mounted into the waker
pod as ``envFrom.secretRef``, exactly like the auth-wall's cookie secret. It carries
only the plaintext wake token under ``ZAD_WAKE_TOKEN``; the ServiceDefinition exposes
no app variables, so ``to_k8s_secret_data`` is overridden rather than derived.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from opi.services.catalog.sleep_mode.token import WAKE_TOKEN_ENV
from opi.services.services_enums import ServiceType
from opi.utils.secrets import BaseSecret


@dataclass
class WakeTokenSecret(BaseSecret):
    """The per-deployment wake token, mounted into the waker pod."""

    token: str

    SECRET_NAME_TEMPLATE: ClassVar[str] = "{prefix}-waker-token"
    SERVICE_TYPE: ClassVar[ServiceType] = ServiceType.SLEEP_MODE

    def to_k8s_secret_data(self) -> dict[str, str]:
        return {WAKE_TOKEN_ENV: self.token}
