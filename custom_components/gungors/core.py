"""Feature machinery shared by every gungors platform.

A gungors entity is a domain base class (``GungorsCover``, ``GungorsClimate``,
``GungorsLight``) plus an ordered list of features from YAML::

    cover:
      - platform: gungors
        name: sercan_cover
        features:
          - hold
          - window: binary_sensor.sercan_window_contact
          - direction
          - link: cover.sercan_blind

Each feature is a mixin class. The entity class is built at setup time with the
features as bases in YAML order, so the first feature is the outermost one in the
MRO: ``type(name, (Hold, Window, Direction, Link, GungorsCover), {})``.

Rules every feature follows (cooperative inheritance):

- Every hook calls ``super()``. Not calling it is always deliberate: the feature
  handled the call and nothing below it should see it (a deferred request, a
  device command the link sends itself).
- Merging hooks (``extra_state_attributes``, ``gw_save``) take what ``super()``
  returns and add their own part.
- A feature reads its YAML value with ``self.gw_conf(KEY)``.
- A feature that writes to an entity claims it (``gw_claims``); the same entity
  can be claimed only once in the whole integration. Any number of features may
  read an entity.
- ``requires`` / ``provides`` name values features pass to each other; an entity
  whose requirements are not met is not created.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Any, ClassVar

import voluptuous as vol

from homeassistant.core import HomeAssistant, State
from homeassistant.helpers.restore_state import RestoreEntity, RestoredExtraData

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Request sources.
SOURCE_REMOTE = "remote"  # an HA service call: UI, automation, voice, script
SOURCE_PHYSICAL = "physical"  # the device itself or a physical button
SOURCE_INTERNAL = "internal"  # a feature replaying or enforcing a request

# Extra restore data key of the base class's own data.
BASE_KEY = "base"

# hass.data[DOMAIN][CLAIMS] = {claimed entity id: owning wrapper name}
CLAIMS = "claims"
# hass.data[DOMAIN][ENTITIES] = {entity id: gungors entity}, for the feature services
ENTITIES = "entities"


@dataclass
class Request:
    """A change request travelling down the feature chain.

    ``data`` holds the domain fields (cover: position / stop, climate: hvac_mode /
    temperature, light: on + service data). ``from_device`` marks a change the
    device already made: it is applied to the wrapper but not sent back.
    """

    data: dict[str, Any]
    source: str = SOURCE_REMOTE
    from_device: bool = False
    tags: set[str] = field(default_factory=set)


class Feature:
    """Base of every feature mixin."""

    # YAML key; also the key of the feature's extra restore data.
    feature_name: ClassVar[str]
    # Validates the feature's YAML value (None for a bare `- name` entry).
    schema: ClassVar[Any] = vol.Any(None, {})
    requires: ClassVar[frozenset[str]] = frozenset()
    provides: ClassVar[frozenset[str]] = frozenset()
    # Must be the last feature (right above the base class).
    last: ClassVar[bool] = False
    # Entity services this feature implements: {service: (schema, method name)}.
    services: ClassVar[dict[str, tuple[dict, str]]] = {}

    @classmethod
    def gw_claims(cls, conf: Any) -> set[str]:
        """Entities (or exclusive roles) this feature writes; default none."""
        return set()


# FEATURES[domain][name] = feature class
FEATURES: dict[str, dict[str, type[Feature]]] = {}


def register(*domains: str):
    """Class decorator registering a feature for one or more domains."""

    def wrap(cls: type[Feature]) -> type[Feature]:
        for domain in domains:
            existing = FEATURES.setdefault(domain, {})
            if cls.feature_name in existing:
                raise ValueError(f"{domain} feature {cls.feature_name} registered twice")
            existing[cls.feature_name] = cls
        return cls

    return wrap


def feature_list_validator(domain: str):
    """Voluptuous validator for a `features:` list of a domain.

    Accepts `- name` or `- name: value` items and returns [(name, value), ...]
    with each value validated by its feature's schema.
    """

    def validate(value: Any) -> list[tuple[str, Any]]:
        if not isinstance(value, list) or not value:
            raise vol.Invalid("features must be a non-empty list")
        known = FEATURES.get(domain, {})
        result: list[tuple[str, Any]] = []
        for item in value:
            if isinstance(item, str):
                name, conf = item, None
            elif isinstance(item, dict) and len(item) == 1:
                name, conf = next(iter(item.items()))
            else:
                raise vol.Invalid(f"invalid feature entry: {item!r}")
            if name not in known:
                raise vol.Invalid(
                    f"unknown {domain} feature '{name}' (known: {', '.join(sorted(known))})"
                )
            try:
                conf = known[name].schema(conf)
            except vol.Invalid as err:
                raise vol.Invalid(f"feature '{name}': {err}") from err
            result.append((name, conf))
        return result

    return validate


def build_entity_class(
    domain: str, base: type, features: list[tuple[str, Any]], wrapper: str
) -> type:
    """Build the entity class for a feature list, checking the composition rules."""
    known = FEATURES[domain]
    classes = [known[name] for name, _ in features]
    names = [name for name, _ in features]

    duplicates = {n for n in names if names.count(n) > 1}
    if duplicates:
        raise vol.Invalid(f"{wrapper}: feature listed twice: {', '.join(sorted(duplicates))}")

    for index, cls in enumerate(classes):
        if cls.last and index != len(classes) - 1:
            raise vol.Invalid(f"{wrapper}: feature '{cls.feature_name}' must be the last one")

    claimed: dict[str, str] = {}
    for (name, conf), cls in zip(features, classes, strict=True):
        for claim in cls.gw_claims(conf):
            if claim in claimed:
                raise vol.Invalid(
                    f"{wrapper}: '{claim}' is claimed by both '{claimed[claim]}' and '{name}'"
                )
            claimed[claim] = name

    provided = set().union(*(cls.provides for cls in classes))
    for cls in classes:
        missing = cls.requires - provided
        if missing:
            raise vol.Invalid(
                f"{wrapper}: feature '{cls.feature_name}' needs {', '.join(sorted(missing))}, "
                "which no feature in the list provides"
            )

    class_name = f"{base.__name__}_" + "_".join(names)
    return type(class_name, (*classes, base), {})


def entity_claims(domain: str, features: list[tuple[str, Any]]) -> set[str]:
    """Claimed entity ids of a feature list (roles without a dot are left out)."""
    known = FEATURES[domain]
    claims: set[str] = set()
    for name, conf in features:
        claims |= {c for c in known[name].gw_claims(conf) if "." in c}
    return claims


def reserve_claims(hass: HomeAssistant, wrapper: str, claims: set[str]) -> None:
    """Reserve entity claims integration-wide; raise if another wrapper owns one."""
    registry: dict[str, str] = hass.data.setdefault(DOMAIN, {}).setdefault(CLAIMS, {})
    taken = {c: registry[c] for c in claims if registry.get(c, wrapper) != wrapper}
    if taken:
        detail = ", ".join(f"{c} (by {w})" for c, w in sorted(taken.items()))
        raise vol.Invalid(f"{wrapper}: entities already written by another wrapper: {detail}")
    for claim in claims:
        registry[claim] = wrapper


def release_claims(hass: HomeAssistant, wrapper: str) -> None:
    registry: dict[str, str] = hass.data.get(DOMAIN, {}).get(CLAIMS, {})
    for claim in [c for c, w in registry.items() if w == wrapper]:
        del registry[claim]


class GungorsEntity(RestoreEntity):
    """Domain-agnostic part of every gungors base class.

    Holds the feature configuration and runs the restore chain. Hooks every
    feature may override (calling super()):

    - ``gw_restore(last_state, extra)``: restore from the last state and the
      extra restore data (``extra[feature_name]``, or legacy flat keys).
    - ``gw_save() -> dict``: data for the next restart, under the feature's key.
    - ``gw_request(request) -> bool``: a change request; return False when it was
      rejected or deferred.
    - ``gw_apply(request)``: an accepted request is applied (the link sends it).
    - ``gw_push()``: push the wrapper's state to the device again (enforcement).
    - ``gw_device_available(available)``: the device became (un)available.
    """

    _attr_should_poll = False

    def __init__(self, name: str, unique_id: str, features: list[tuple[str, Any]],
                 options: dict[str, Any]) -> None:
        self._attr_name = name
        self._attr_unique_id = unique_id
        self.gw_wrapper = name
        self.gw_features = features
        self.gw_options = options
        self._gw_conf = dict(features)

    def gw_conf(self, key: str) -> Any:
        """YAML value of a feature in this entity."""
        return self._gw_conf.get(key)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        extra_data = await self.async_get_last_extra_data()
        extra = extra_data.as_dict() if extra_data is not None else {}
        self.gw_restore(last, extra)
        self.hass.data.setdefault(DOMAIN, {}).setdefault(ENTITIES, {})[self.entity_id] = self

    async def async_will_remove_from_hass(self) -> None:
        release_claims(self.hass, self.gw_wrapper)
        self.hass.data.get(DOMAIN, {}).get(ENTITIES, {}).pop(self.entity_id, None)
        await super().async_will_remove_from_hass()

    # --- chain hooks (base implementations end the chains) -----------------

    def gw_restore(self, last: State | None, extra: dict[str, Any]) -> None:
        """End of the restore chain."""

    def gw_save(self) -> dict[str, Any]:
        """End of the save chain."""
        return {}

    async def gw_request(self, request: Request) -> bool:
        await self.gw_apply(request)
        return True

    async def gw_apply(self, request: Request) -> None:
        """End of the apply chain: domain bases override this to store the state."""

    async def gw_push(self) -> None:
        """End of the push chain (nothing to push to without a link)."""

    def gw_device_available(self, available: bool) -> None:
        """End of the availability chain."""

    async def gw_physical(self, data: dict[str, Any]) -> bool:
        """The device changed by itself: request it, or push back if rejected."""
        accepted = await self.gw_request(
            Request(data=data, source=SOURCE_PHYSICAL, from_device=True)
        )
        if not accepted:
            await self.gw_push()
        return accepted

    @property
    def extra_restore_state_data(self) -> RestoredExtraData:
        return RestoredExtraData(self.gw_save())


def as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
