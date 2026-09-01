"""What a project may set on a service, and how far it may go (RC-168).

Three things keep turning up together whenever a service field becomes user-settable:
a *bound* on what is responsible, a *value* a project picks inside it, and something
that checks the choice. Today each of those sits somewhere else -- a ``le=`` on a
pydantic field, a hardcoded number in a connector, a dropdown in the wizard -- and the
second service that needs one builds a second variant.

This module is the one shape for it. A service declares, per field:

* the **room**: minimum, maximum (or the allowed set), and the default;
* the **layers** on which a project may set the field at all.

Those three are a platform decision, so they live in the service package and cannot be
widened from a project file. The value is user input and is judged against the
declaration -- never against a number that happens to sit in a model.

::

    dienst declareert:    min 1,  max 100,  standaard 20
                                  |
    projectbestand kiest:         60          <- getoetst aan het bovenstaande
    deployment overschrijft:      80          <- idem
                                  |
    connector krijgt:             80

Three rules follow, and they are what the code below implements:

* **One source per bound.** The model, the wizard and the error message all read the
  same declaration. A bound restated as ``le=100`` next to it is two rules that drift.
* **More specific wins.** Deployment over project, project over the service default.
  One function (``resolve_setting``), not a merge re-invented per service.
* **What a service does not declare is not settable.** Not by wizard, not by API, and
  not by hand-editing the project file either.

Three kinds of bound, and deliberately no fourth: whole numbers, Kubernetes quantities
(``100m`` and ``2`` are both CPU, ``512Mi`` and ``1Gi`` both memory, so they are parsed
and compared as numbers rather than as text), and a value out of a closed set.

Nothing in the catalog declares a setting yet; a service that declares none behaves
exactly as it did. The two plans this one sits under (the connection limit, and a
project's own database cluster) are what fill it in.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Final

from opi.services.catalog.base import ConfigLayer, config_path
from opi.services.resource_analyzer import parse_k8s_cpu_to_m, parse_k8s_memory_to_mi

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from opi.services.services_enums import ServiceType


class SettingError(ValueError):
    """A value that falls outside what the service declared, or a field it did not.

    Carries the message a user reads, so every road into a project file -- wizard, API,
    hand-edited YAML -- reports the same sentence.
    """


class QuantityKind(Enum):
    """Which Kubernetes quantity a bound is expressed in.

    The two are not interchangeable: ``2`` means two cores for CPU and two bytes for
    memory. Naming the kind picks the parser, which is the whole reason quantities are
    compared as numbers here instead of as strings -- ``1Gi`` sorts before ``512Mi``
    alphabetically and is four times as large.
    """

    MEMORY = "memory"
    CPU = "cpu"


#: The parser per kind. Both already existed and are reused rather than rewritten:
#: ``opi/services/resource_analyzer.py`` is where the platform turns a Kubernetes
#: quantity into a number, for auto-tune and for the storage ceiling.
_QUANTITY_PARSERS: Final[dict[QuantityKind, Callable[[str], float]]] = {
    QuantityKind.MEMORY: parse_k8s_memory_to_mi,
    QuantityKind.CPU: parse_k8s_cpu_to_m,
}

#: How a layer is named to a user, for the message that refuses a value on a layer the
#: service does not open up.
_LAYER_LABEL: Final[dict[ConfigLayer, str]] = {
    ConfigLayer.PROJECT: "op projectniveau",
    ConfigLayer.COMPONENT: "per component",
    ConfigLayer.DEPLOYMENT: "per deployment",
    ConfigLayer.DEPLOYMENT_COMPONENT: "per component binnen een deployment",
}

#: The layers from least to most specific. ``resolve_setting`` walks this backwards, so
#: this tuple IS the "more specific wins" rule; there is no second ordering anywhere.
_SPECIFICITY: Final[tuple[ConfigLayer, ...]] = (
    ConfigLayer.PROJECT,
    ConfigLayer.COMPONENT,
    ConfigLayer.DEPLOYMENT,
    ConfigLayer.DEPLOYMENT_COMPONENT,
)


class _Missing:
    """Sentinel for "this config block does not mention the field at all".

    A distinct type rather than ``None``, because ``None`` is a value a project file can
    genuinely carry and "absent" and "explicitly empty" are not the same answer.
    """

    def __repr__(self) -> str:
        return "MISSING"


#: The one instance; compare with ``is``.
MISSING: Final[_Missing] = _Missing()


@dataclass(frozen=True, kw_only=True)
class ConfigSetting(ABC):
    """One field of a service's config that a project may set, plus its room.

    ``path`` is dot-separated and relative to the service's ``config`` block, so a
    nested field is ``"resources.requests.memory"``. ``layers`` names every layer the
    field may be set on; anywhere else it is refused, which is how a field is opened up
    deliberately rather than by accident. ``default`` is what the service does when no
    layer says anything, and it is checked against the declaration's own bounds at
    import time -- a default outside its own room is a bug in the declaration, not
    something to discover in production.
    """

    path: str
    layers: tuple[ConfigLayer, ...]
    default: Any
    #: What the field is called on screen. Used by the form field built from this
    #: declaration, so the label is not typed a second time next to the widget.
    label: str

    def __post_init__(self) -> None:
        if not self.path:
            raise ValueError("Een instelbaar veld heeft een pad nodig")
        if not self.layers:
            raise ValueError(f"Instelbaar veld '{self.path}' noemt geen enkele laag waarop het gezet mag worden")
        try:
            self.check(self.default)
        except SettingError as e:
            raise ValueError(f"De standaardwaarde van '{self.path}' valt buiten zijn eigen speelruimte: {e}") from e

    @property
    def path_parts(self) -> tuple[str, ...]:
        """``path`` split into the keys to walk in a config block."""
        return tuple(self.path.split("."))

    def allows(self, layer: ConfigLayer) -> bool:
        """Whether a project may set this field at ``layer``."""
        return layer in self.layers

    def yaml_path(self, service: ServiceType, layer: ConfigLayer) -> str:
        """Where this field lives in the project file at ``layer``.

        Built with ``config_path`` like every other service path, so a form field made
        from this declaration writes to the same place the validation reads.

        Raises:
            SettingError: if the service does not open the field up at ``layer``.
        """
        if not self.allows(layer):
            raise SettingError(wrong_layer_message(self, layer))
        return config_path(layer, service, "config", *self.path_parts)

    @abstractmethod
    def check(self, value: Any) -> Any:
        """Judge one value and return it (normalised where that means the same value).

        Raises:
            SettingError: with the sentence the user reads.
        """

    @abstractmethod
    def latitude(self) -> str:
        """The room, as one Dutch sentence, for a help text or an API description.

        The wizard's help text comes from here rather than being written out a second
        time, which is what keeps the screen and the validation on the same bounds.
        """

    def check_change(self, previous: Any, new: Any) -> None:  # noqa: B027 - opt-in hook, most fields move both ways
        """Judge a value against the one it replaces. Default: any change is fine.

        Only a setting whose field cannot move both ways overrides this.

        Raises:
            SettingError: if the change itself is refused, whatever the bounds say.
        """


@dataclass(frozen=True, kw_only=True)
class IntegerSetting(ConfigSetting):
    """A whole number between two bounds, e.g. a connection limit or an instance count."""

    minimum: int
    maximum: int

    def __post_init__(self) -> None:
        if self.minimum > self.maximum:
            raise ValueError(f"Instelbaar veld '{self.path}': minimum {self.minimum} ligt boven maximum {self.maximum}")
        super().__post_init__()

    def check(self, value: Any) -> int:
        # bool is an int in Python, and "true" is not a number anyone meant to write.
        if isinstance(value, bool):
            raise SettingError(f"'{self.path}' moet een geheel getal zijn; je gaf {value!r}.")
        if isinstance(value, str):
            try:
                value = int(value.strip())
            except ValueError:
                raise SettingError(f"'{self.path}' moet een geheel getal zijn; je gaf {value!r}.") from None
        if not isinstance(value, int):
            raise SettingError(f"'{self.path}' moet een geheel getal zijn; je gaf {value!r}.")
        if not self.minimum <= value <= self.maximum:
            raise SettingError(f"'{self.path}' moet tussen {self.minimum} en {self.maximum} liggen; je gaf {value}.")
        return value

    def latitude(self) -> str:
        return f"Een geheel getal van {self.minimum} tot en met {self.maximum}. Standaard {self.default}."


@dataclass(frozen=True, kw_only=True)
class QuantitySetting(ConfigSetting):
    """A Kubernetes quantity between two bounds: memory, CPU or a volume size.

    Compared as a number, never as text. ``grow_only`` marks a field that cannot move
    back down -- a PVC cannot shrink, so a bound of "between 1Gi and 100Gi" would let a
    reduction through that then silently does nothing or wedges the rollout.
    """

    minimum: str
    maximum: str
    kind: QuantityKind
    grow_only: bool = False

    def __post_init__(self) -> None:
        if self._parse(self.minimum) > self._parse(self.maximum):
            raise ValueError(f"Instelbaar veld '{self.path}': minimum {self.minimum} ligt boven maximum {self.maximum}")
        super().__post_init__()

    def _parse(self, value: Any) -> float:
        """The value as a number in the kind's base unit (MiB or millicores)."""
        if not isinstance(value, str):
            raise SettingError(
                f"'{self.path}' moet een Kubernetes-hoeveelheid zijn zoals {self.minimum} of {self.maximum}; "
                f"je gaf {value!r}."
            )
        try:
            return float(_QUANTITY_PARSERS[self.kind](value))
        except ValueError:
            raise SettingError(
                f"'{self.path}' is geen geldige Kubernetes-hoeveelheid: {value!r}. "
                f"Gebruik iets als {self.minimum} of {self.maximum}."
            ) from None

    def check(self, value: Any) -> str:
        amount = self._parse(value)
        if not self._parse(self.minimum) <= amount <= self._parse(self.maximum):
            raise SettingError(f"'{self.path}' moet tussen {self.minimum} en {self.maximum} liggen; je gaf {value}.")
        return value

    def check_change(self, previous: Any, new: Any) -> None:
        if not self.grow_only:
            return
        if self._parse(new) < self._parse(previous):
            raise SettingError(
                f"'{self.path}' kan alleen omhoog: de waarde staat nu op {previous} en {new} is kleiner."
            )

    def latitude(self) -> str:
        room = f"Een hoeveelheid van {self.minimum} tot en met {self.maximum}. Standaard {self.default}."
        return f"{room} Je kunt deze waarde later alleen verhogen." if self.grow_only else room


@dataclass(frozen=True, kw_only=True)
class ChoiceSetting(ConfigSetting):
    """A value out of a closed set, e.g. an image or a named registry.

    Not a bound but a membership: ``image`` and ``registry`` are free text today, and a
    value the cluster's admission rewrite does not accept turns into an endless
    OutOfSync loop instead of a sentence someone can read.
    """

    allowed: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.allowed:
            raise ValueError(f"Instelbaar veld '{self.path}' noemt geen enkele toegestane waarde")
        super().__post_init__()

    def check(self, value: Any) -> str:
        if not isinstance(value, str) or value not in self.allowed:
            raise SettingError(
                f"'{self.path}' moet een van deze waarden zijn: {', '.join(self.allowed)}; je gaf {value!r}."
            )
        return value

    def latitude(self) -> str:
        return f"Een van: {', '.join(self.allowed)}. Standaard {self.default}."


def wrong_layer_message(setting: ConfigSetting, layer: ConfigLayer) -> str:
    """The refusal for a field set on a layer the service does not open it up on."""
    allowed = " of ".join(_LAYER_LABEL[known] for known in _SPECIFICITY if known in setting.layers)
    return f"'{setting.path}' kun je niet {_LAYER_LABEL[layer]} zetten; dat kan alleen {allowed}."


def read_setting_value(config: Any, setting: ConfigSetting) -> Any:
    """The raw value of ``setting`` in one config block, or ``MISSING``.

    Walks ``setting.path`` key by key. Anything that is not a dict on the way down means
    the field is not there -- a service whose config at a layer is a list (storage
    mounts) simply has nothing to read.
    """
    node: Any = config
    for part in setting.path_parts:
        if not isinstance(node, dict) or part not in node:
            return MISSING
        node = node[part]
    return node


def resolve_setting(setting: ConfigSetting, values: Mapping[ConfigLayer, Any]) -> Any:
    """The effective value: the most specific layer that says something, else the default.

    ``values`` maps a layer to what the project file has there; a layer that is absent,
    or holds ``None``, says nothing. Layers the service does not open up are never
    consulted, so a value smuggled onto one cannot take effect even if validation were
    somehow skipped.

    The returned value is checked, so a caller handing it to a connector is handing over
    something the service declared it can live with.
    """
    for layer in reversed(_SPECIFICITY):
        if not setting.allows(layer):
            continue
        value = values.get(layer)
        if value is not None:
            return setting.check(value)
    return setting.default


def check_settings(settings: Sequence[ConfigSetting], config: Any, layer: ConfigLayer) -> None:
    """Judge every declared setting that ``config`` mentions, at ``layer``.

    Two refusals: a value outside the declared room, and a value on a layer the service
    does not open the field up on. Fails on the first one.

    Raises:
        SettingError: with the sentence the user reads.
    """
    for setting in settings:
        value = read_setting_value(config, setting)
        if value is MISSING:
            continue
        if not setting.allows(layer):
            raise SettingError(wrong_layer_message(setting, layer))
        setting.check(value)


def check_setting_changes(settings: Sequence[ConfigSetting], previous: Any, config: Any, layer: ConfigLayer) -> None:
    """Judge what ``config`` changes about the settings, against ``previous``.

    A field a version does not mention stands on the service default, and that holds for
    BOTH versions. Judging only the fields the new one names would make the rule
    avoidable by leaving the field out: the effective value drops to the default, which
    is the very reduction the rule exists to refuse, and the empty wizard field is the
    road to it. So each side is read the same way -- the value if it is there, the
    default if it is not -- and a field neither version mentions is not a change at all.

    Only settings that declare a rule about changes (``grow_only``) do anything here.

    Raises:
        SettingError: with the sentence the user reads.
    """
    for setting in settings:
        if not setting.allows(layer):
            continue
        old_value = read_setting_value(previous, setting)
        new_value = read_setting_value(config, setting)
        if old_value is MISSING and new_value is MISSING:
            continue  # neither version mentions the field: both stand on the default
        setting.check_change(
            setting.default if old_value is MISSING else old_value,
            setting.default if new_value is MISSING else new_value,
        )
