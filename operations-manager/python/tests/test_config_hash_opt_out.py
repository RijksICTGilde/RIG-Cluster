"""A service can keep its own secret out of the application's config hash.

The ArgoCD CMP plugin hashes every Secret and ConfigMap in an Application and stamps the
result on each pod template as ``checksum/config``, so pods restart when their
configuration changes. That over-approximates: a service may ship a secret only its own
auxiliary pod reads.

Sleep-mode is the case that proved it. Its waker token Secret exists only while a
deployment sleeps, so pruning it on wake changed the hash and restarted the application a
SECOND time, right after it had just come back up. Measured on the sandbox: three
ReplicaSets whose pod templates differed in nothing but ``checksum/config``, with the
awake one identical to the pre-sleep one.

A service therefore says what its secret is (``include_in_config_hash=False``), and the
platform translates that into the label the plugin filters on.
"""

from __future__ import annotations

from pathlib import Path

from opi.generation.manifests import CONFIG_HASH_IGNORE_LABEL_KEY, CONFIG_HASH_IGNORE_LABEL_VALUE
from opi.manager.project_manager import _secret_labels_for
from opi.services.catalog.base import SecretFileSpec

_PLUGIN = Path(__file__).parent.parent.parent.parent / "bootstrap/rig-system/kustomize/configmap-sops-plugin.yaml"


def _spec(**kwargs: object) -> SecretFileSpec:
    return SecretFileSpec(secret_name="s", secret_pairs={"A": "1"}, **kwargs)  # type: ignore[arg-type]


def test_a_secret_counts_as_application_config_by_default() -> None:
    """Opting out must be a deliberate act: a secret the app reads and that silently
    stopped counting would leave pods running on stale configuration."""
    assert _spec().include_in_config_hash is True
    assert _secret_labels_for(_spec()) == {}


def test_opting_out_adds_the_label_the_plugin_filters_on() -> None:
    labels = _secret_labels_for(_spec(include_in_config_hash=False))

    assert labels[CONFIG_HASH_IGNORE_LABEL_KEY] == CONFIG_HASH_IGNORE_LABEL_VALUE


def test_opting_out_keeps_the_service_its_own_labels() -> None:
    labels = _secret_labels_for(_spec(include_in_config_hash=False, secret_labels={"eigen": "waarde"}))

    assert labels["eigen"] == "waarde"
    assert labels[CONFIG_HASH_IGNORE_LABEL_KEY] == CONFIG_HASH_IGNORE_LABEL_VALUE


def test_the_service_never_writes_the_label_itself() -> None:
    """The whole point of the boolean: a service states what its secret IS and does not
    need to know that a config hash exists, let alone what it is keyed on."""
    source = (Path(__file__).parent.parent / "opi/services/catalog").rglob("*.py")
    for path in source:
        assert CONFIG_HASH_IGNORE_LABEL_KEY not in path.read_text(), path


def test_the_plugin_filters_on_exactly_this_label() -> None:
    """Drift guard across a language boundary.

    The plugin is a shell/yq filter and cannot import the constant, so the string lives
    twice. This fails the moment one side is renamed without the other, which would
    otherwise show up only as pods restarting for no reason.
    """
    script = _PLUGIN.read_text()

    assert CONFIG_HASH_IGNORE_LABEL_KEY in script
    assert CONFIG_HASH_IGNORE_LABEL_VALUE in script
