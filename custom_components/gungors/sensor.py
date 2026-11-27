"""Sensor platform for the gungors integration: `sensor.immich_processing` (the last Immich run),
the video and photo queues, and the
Budget Thuis usage sensors (budget_thuis.py, from the config entry).

State `tamam` / `hata` / `başlamadı`; attributes: the mode, the last run, new
assets per person album, the trips (name, dates, assets, new ones) and the
faceless counts. In `Deneme` mode these are the plan; nothing was written.
"""
from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import budget_thuis
from .const import DOMAIN
from .immich import SIGNAL_UPDATE, async_get_manager


async def async_setup_platform(
    hass: HomeAssistant,
    config: dict[str, Any],
    async_add_entities: AddEntitiesCallback,
    discovery_info: dict[str, Any] | None = None,
) -> None:
    if discovery_info is None or async_get_manager(hass) is None:
        return
    # English entity ids since 1.34.0 (labels stay Turkish): rename the old ones in place
    registry = er.async_get(hass)
    for unique_id, entity_id in RENAMED.items():
        old = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
        if old and old != entity_id and registry.async_get(entity_id) is None:
            registry.async_update_entity(old, new_entity_id=entity_id)
    async_add_entities([ImmichSensor(), ImmichVideoQueueSensor(), ImmichPhotoQueueSensor()], True)


# unique_id -> entity_id
RENAMED = {
    "immich_status": "sensor.immich_processing",
    "immich_video_queue": "sensor.immich_video_queue",
}
# Immich queues that hold a new photo before it reaches the person albums
PHOTO_QUEUES = ("metadataExtraction", "thumbnailGeneration", "faceDetection", "facialRecognition")
_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = timedelta(minutes=1)  # the photo queue sensor


class ImmichSensor(SensorEntity):
    """Result (or plan) of the last Immich album run: çalışıyor / tamam / hata."""

    _attr_name = "Immich işleme"
    _attr_unique_id = "immich_status"
    entity_id = "sensor.immich_processing"
    _attr_icon = "mdi:image-album"
    _attr_should_poll = False
    _unrecorded_attributes = frozenset({"people", "people_users", "trips", "faceless", "error", "users"})

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


class ImmichVideoQueueSensor(SensorEntity):
    """Videos waiting for the face scan; updated after each scanned video."""

    _attr_name = "Immich video kuyruğu"
    _attr_unique_id = "immich_video_queue"
    entity_id = "sensor.immich_video_queue"
    _attr_icon = "mdi:video-account"
    _attr_native_unit_of_measurement = "video"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_should_poll = False

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_UPDATE, self.async_write_ha_state)
        )

    @property
    def available(self) -> bool:
        manager = async_get_manager(self.hass)
        return bool(manager and manager.config["videos"].get("ml_url"))

    @property
    def native_value(self) -> int | None:
        manager = async_get_manager(self.hass)
        return manager.video_queue.get("bekleyen") if manager else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        manager = async_get_manager(self.hass)
        if manager is None:
            return {}
        return {k: v for k, v in manager.video_queue.items() if k != "bekleyen"}


class ImmichPhotoQueueSensor(SensorEntity):
    """Jobs in Immich's own photo pipeline (metadata, thumbnails, faces) not done yet.

    A new photo reaches the person albums only after these; polled every minute.
    """

    _attr_name = "Immich fotoğraf kuyruğu"
    _attr_unique_id = "immich_photo_queue"
    entity_id = "sensor.immich_photo_queue"
    _attr_icon = "mdi:image-multiple"
    _attr_native_unit_of_measurement = "iş"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_should_poll = True
    _counts: dict[str, int] | None = None

    async def async_update(self) -> None:
        manager = async_get_manager(self.hass)
        api = manager._apis.get(manager.owner) if manager else None
        self._counts = await api.queue_counts() if api else None

    @property
    def available(self) -> bool:
        return self._counts is not None

    @property
    def native_value(self) -> int | None:
        if self._counts is None:
            return None
        return sum(self._counts.get(q, 0) for q in PHOTO_QUEUES)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {q: (self._counts or {}).get(q, 0) for q in PHOTO_QUEUES}


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
