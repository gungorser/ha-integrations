"""Select platform for the gungors integration: `select.immich`, the Immich albums mode.

Loaded only when `gungors: immich:` is configured (immich.py); the mode is restored
after a restart (`Deneme` the first time).
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .immich import MODE_DRY, MODE_TRASH, MODES, async_get_manager


async def async_setup_platform(
    hass: HomeAssistant,
    config: dict[str, Any],
    async_add_entities: AddEntitiesCallback,
    discovery_info: dict[str, Any] | None = None,
) -> None:
    if discovery_info is None or async_get_manager(hass) is None:
        return
    async_add_entities([ImmichModeSelect()])


class ImmichModeSelect(SelectEntity, RestoreEntity):
    """What the Immich album rules may do."""

    _attr_name = "immich"
    _attr_unique_id = "immich_mode"
    _attr_icon = "mdi:image-album"
    _attr_options = MODES

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        known = (*MODES, MODE_TRASH)  # the old trash mode restores as Albüm
        mode = last.state if last is not None and last.state in known else MODE_DRY
        await async_get_manager(self.hass).async_set_mode(mode)

    @property
    def current_option(self) -> str | None:
        manager = async_get_manager(self.hass)
        return manager.mode if manager else None

    async def async_select_option(self, option: str) -> None:
        await async_get_manager(self.hass).async_set_mode(option)
        self.async_write_ha_state()
