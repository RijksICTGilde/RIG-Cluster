"""The ``.dockerconfigjson`` of a registry credential."""

import base64
import json


def dockerconfigjson(registry_url: str, username: str, password: str) -> str:
    """The .dockerconfigjson content in auth-only format.

    The shape is ``{"auths": {"registry.example.com": {"auth": "base64(user:pass)"}}}``.
    """
    auth = base64.b64encode(f"{username}:{password}".encode()).decode()
    return json.dumps({"auths": {registry_url: {"auth": auth}}})
