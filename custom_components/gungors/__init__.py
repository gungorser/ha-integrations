"""The gungors integration.

Wrapper entities built from features (see core.py): every gungors entity drives a
hidden original entity through its `link` feature, and the other features in its
YAML list add behaviour on top. Platforms are configured in YAML
(`cover: - platform: gungors`, `climate: ...`, `light: ...`).

The `gungors.reload` service reloads the YAML of all platforms and the shared
presets (`gungors: presets:`, presets.py) without a restart (Python changes still
need a restart). `gungors.blink` flashes gungors lights (feedback, see
features/link_light.py). Feature services (`gungors.set_pid_gain`,
`gungors.pause_program`, ...) are registered here once for all domains and call
the feature's method on each targeted entity.
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.const import ATTR_ENTITY_ID, Platform
from homeassistant.core import Event, HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.reload import async_integration_yaml_config
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .presets import CONF_PRESETS, async_set_presets, presets_schema

PLATFORMS = [
    Platform.CALENDAR,
    Platform.CLIMATE,
    Platform.COVER,
    Platform.EVENT,
    Platform.LIGHT,
]

CONFIG_SCHEMA = vol.Schema(
    {
        vol.Optional(DOMAIN): vol.Schema(
            {vol.Optional(CONF_PRESETS, default={}): presets_schema}
        )
    },
    extra=vol.ALLOW_EXTRA,
)

BLINK_SCHEMA = {
    vol.Optional("rgb_color"): vol.All(
        vol.ExactSequence((cv.byte, cv.byte, cv.byte)), vol.Coerce(tuple)
    ),
    vol.Optional("count", default=1): vol.All(vol.Coerce(int), vol.Range(min=1, max=5)),
}

__all__ = ["DOMAIN", "PLATFORMS"]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the feature services (platforms are configured via YAML)."""
    from . import features  # noqa: F401, PLC0415  (registers every feature)
    from .core import ENTITIES, FEATURES  # noqa: PLC0415

    async_set_presets(hass, (config.get(DOMAIN) or {}).get(CONF_PRESETS, {}))

    async def presets_reloaded(_event: Event) -> None:
        from .program import async_get_manager  # noqa: PLC0415

        conf = await async_integration_yaml_config(hass, DOMAIN)
        if conf is None:
            return
        async_set_presets(hass, (conf.get(DOMAIN) or {}).get(CONF_PRESETS, {}))
        async_get_manager(hass).async_presets_changed()

    hass.bus.async_listen(f"event_{DOMAIN}_reloaded", presets_reloaded)

    services: dict[str, tuple[dict, str]] = {"blink": (BLINK_SCHEMA, "gw_blink")}
    for domain_features in FEATURES.values():
        for feature in domain_features.values():
            for service, spec in feature.services.items():
                services.setdefault(service, spec)

    for service, (schema, method) in services.items():

        async def handle(call: ServiceCall, method: str = method) -> None:
            entities: dict[str, Any] = hass.data.get(DOMAIN, {}).get(ENTITIES, {})
            data = {k: v for k, v in call.data.items() if k != ATTR_ENTITY_ID}
            for entity_id in call.data[ATTR_ENTITY_ID]:
                entity = entities.get(entity_id)
                func = getattr(entity, method, None) if entity is not None else None
                if func is None:
                    raise ServiceValidationError(
                        f"{entity_id} is not a gungors entity with a feature for "
                        f"{DOMAIN}.{call.service}"
                    )
                await func(**data)

        hass.services.async_register(
            DOMAIN,
            service,
            handle,
            vol.Schema({vol.Required(ATTR_ENTITY_ID): cv.entity_ids, **schema}),
        )
    return True
