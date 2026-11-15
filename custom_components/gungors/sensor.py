"""Sensor platform for the gungors integration: `sensor.immich`, the last Immich run.

State `tamam` / `hata` / `başlamadı`; attributes: the mode, the last run, new
assets per person album, the trips (name, dates, assets, new ones) and the
faceless counts. In `Deneme` mode these are the plan; nothing was written.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .immich import SIGNAL_UPDATE, async_get_manager


async def async_setup_platform(
    hass: HomeAssistant,
    config: dict[str, Any],
    async_add_entities: AddEntitiesCallback,
    discovery_info: dict[str, Any] | None = None,
) -> None:
    if discovery_info is None or async_get_manager(hass) is None:
        return
    async_add_entities([ImmichSensor()])


class ImmichSensor(SensorEntity):
    """Result (or plan) of the last Immich album run."""

    _attr_name = "immich"
    _attr_unique_id = "immich_status"
    _attr_icon = "mdi:image-album"
    _attr_should_poll = False
    _unrecorded_attributes = frozenset({"people", "trips", "faceless", "error"})

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_UPDATE, self.async_write_ha_state)
        )

    @property
    def native_value(self) -> str | None:
        manager = async_get_manager(self.hass)
        return manager.status if manager else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        manager = async_get_manager(self.hass)
        return dict(manager.summary) if manager else {}
