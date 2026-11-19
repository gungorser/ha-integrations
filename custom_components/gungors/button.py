"""Button platform for the gungors integration: `button.immich_sync`, an Immich run now.

Loaded only when `gungors: immich:` is configured (immich.py); does what
`gungors.immich_run` does, in the current `select.immich` mode.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .immich import async_get_manager


async def async_setup_platform(
    hass: HomeAssistant,
    config: dict[str, Any],
    async_add_entities: AddEntitiesCallback,
    discovery_info: dict[str, Any] | None = None,
) -> None:
    if discovery_info is None or async_get_manager(hass) is None:
        return
    async_add_entities([ImmichSyncButton()])


class ImmichSyncButton(ButtonEntity):
    """Sync the Immich albums now."""

    _attr_name = "immich sync"
    _attr_unique_id = "immich_sync"
    _attr_icon = "mdi:sync"

    async def async_press(self) -> None:
        manager = async_get_manager(self.hass)
        if manager is not None:
            await manager.async_run()
