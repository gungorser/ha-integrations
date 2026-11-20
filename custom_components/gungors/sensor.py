"""Sensor platform for the gungors integration: `sensor.immich`, the last Immich run, and the
Budget Thuis usage sensors (budget_thuis.py, from the config entry).

State `tamam` / `hata` / `başlamadı`; attributes: the mode, the last run, new
assets per person album, the trips (name, dates, assets, new ones) and the
faceless counts. In `Deneme` mode these are the plan; nothing was written.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import budget_thuis
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
    _unrecorded_attributes = frozenset({"people", "trips", "faceless", "error", "users"})

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


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Budget Thuis: unavailable while it is not configured."""
    async_add_entities([BudgetThuisSensor(kind) for kind in budget_thuis.KINDS])


class BudgetThuisSensor(SensorEntity):
    """The month's usage so far (Energy dashboard: total_increasing, 0 at a new month)."""

    _attr_should_poll = False
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, kind: str) -> None:
        self._kind = kind
        self._attr_name = f"budget thuis {kind}"
        self._attr_unique_id = f"budget_thuis_{kind}"
        if kind == "gaz":
            self._attr_device_class = SensorDeviceClass.GAS
            self._attr_native_unit_of_measurement = UnitOfVolume.CUBIC_METERS
        else:
            self._attr_device_class = SensorDeviceClass.ENERGY
            self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, budget_thuis.SIGNAL_UPDATE, self.async_write_ha_state
            )
        )

    @property
    def available(self) -> bool:
        manager = budget_thuis.async_get_manager(self.hass)
        return manager is not None  # unknown until the first read; status says why

    @property
    def native_value(self) -> float | None:
        manager = budget_thuis.async_get_manager(self.hass)
        return manager.month.get(self._kind) if manager and manager.month_start else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        manager = budget_thuis.async_get_manager(self.hass)
        if manager is None:
            return {}
        attrs: dict[str, Any] = {"status": manager.status}
        if manager.month_start:
            attrs["month_start"] = manager.month_start.isoformat()
        if self._kind in manager.last_day:
            day, amount = manager.last_day[self._kind]
            attrs["last_day"] = day.isoformat()
            attrs["last_day_value"] = amount
        if manager.last_run:
            attrs["last_run"] = manager.last_run.isoformat()
        if manager.error:
            attrs["error"] = manager.error
        return attrs
