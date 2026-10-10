"""Shared fixtures for the gungors tests."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from homeassistant.const import EVENT_CALL_SERVICE
from homeassistant.core import Event, HomeAssistant, ServiceCall
from homeassistant.setup import async_setup_component

pytest_plugins = "pytest_homeassistant_custom_component"

REPO_COMPONENTS = str(Path(__file__).resolve().parent.parent / "custom_components")


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Load custom_components/ from this repository.

    The hass fixture puts its own testing_config/custom_components package first
    on the path; add this repository's directory to that package.
    """
    import custom_components  # noqa: PLC0415

    if REPO_COMPONENTS not in custom_components.__path__:
        custom_components.__path__.append(REPO_COMPONENTS)
    yield


class Calls:
    """Records every service call (from the call_service event)."""

    def __init__(self) -> None:
        self.items: list[tuple[str, str, dict[str, Any]]] = []

    def record(self, event: Event) -> None:
        self.items.append(
            (event.data["domain"], event.data["service"], dict(event.data["service_data"]))
        )

    def of(self, domain: str, service: str, entity_id: str | None = None) -> list[dict[str, Any]]:
        result = []
        for d, s, data in self.items:
            if d != domain or s != service:
                continue
            if entity_id is not None:
                ids = data.get("entity_id")
                ids = [ids] if isinstance(ids, str) else list(ids or [])
                if entity_id not in ids:
                    continue
            result.append(data)
        return result

    def clear(self) -> None:
        self.items.clear()


@pytest.fixture
def calls(hass: HomeAssistant) -> Calls:
    """Record service calls; mock the services of domains no test sets up."""
    recorder = Calls()
    hass.bus.async_listen(EVENT_CALL_SERVICE, recorder.record)

    async def noop(call: ServiceCall) -> None:
        return None

    for domain, services in {
        "cover": ["set_cover_position", "open_cover", "close_cover", "stop_cover"],
        "climate": ["set_hvac_mode", "set_temperature"],
        "number": ["set_value"],
        "input_number": ["set_value"],
        "light": ["turn_on", "turn_off"],
        "switch": ["turn_on", "turn_off"],
    }.items():
        for service in services:
            hass.services.async_register(domain, service, noop)
    return recorder


async def setup_platform(hass: HomeAssistant, domain: str, *entries: dict[str, Any]) -> None:
    """Set up gungors entities of one domain from YAML-like dicts."""
    assert await async_setup_component(
        hass, domain, {domain: [{"platform": "gungors", **e} for e in entries]}
    )
    await hass.async_block_till_done()
