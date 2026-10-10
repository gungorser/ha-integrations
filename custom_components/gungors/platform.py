"""Shared platform setup: build each YAML entry's entity class from its features."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.const import CONF_NAME, CONF_UNIQUE_ID
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.reload import async_setup_reload_service

from . import PLATFORMS, features as _features  # noqa: F401  (registers features)
from .const import CONF_FEATURES, DOMAIN
from .core import (
    build_entity_class,
    entity_claims,
    feature_list_validator,
    reserve_claims,
)

_LOGGER = logging.getLogger(__name__)


def features_schema(domain: str, base_schema: vol.Schema, options: dict) -> vol.Schema:
    """Platform schema: name, unique_id, the feature list and domain options."""
    return base_schema.extend(
        {
            vol.Required(CONF_NAME): cv.string,
            vol.Optional(CONF_UNIQUE_ID): cv.string,
            vol.Required(CONF_FEATURES): feature_list_validator(domain),
            **options,
        }
    )


async def async_setup_features_platform(
    hass: HomeAssistant,
    config: dict[str, Any],
    async_add_entities: AddEntitiesCallback,
    domain: str,
    base: type,
    option_keys: list[str],
) -> None:
    await async_setup_reload_service(hass, DOMAIN, PLATFORMS)

    name = config[CONF_NAME]
    features = config[CONF_FEATURES]
    try:
        cls = build_entity_class(domain, base, features, name)
        reserve_claims(hass, name, entity_claims(domain, features))
    except vol.Invalid as err:
        _LOGGER.error("%s.%s not created: %s", domain, name, err)
        return

    options = {key: config.get(key) for key in option_keys}
    unique_id = config.get(CONF_UNIQUE_ID) or name
    async_add_entities([cls(name, unique_id, features, options)])

