"""Shared presets: named attribute sets per domain, from the `gungors:` YAML.

```yaml
gungors:
  presets:
    light:
      default: {state: "off", rgb_color: [255, 255, 255], brightness_pct: 100}
      dim: {brightness_pct: 10}
      night: {rgb_color: [255, 0, 0]}
    climate:
      default: {state: "off"}
    cover:
      default: {current_position: "{{ 100 if is_state('sun.sun', 'above_horizon') else 0 }}"}
```

- Attributes are named as in the program rules (rules.py): light `state`,
  `brightness_pct`, `rgb_color`, `color_temp_kelvin`; climate `state`,
  `temperature`; cover `current_position`. A value may be a template.
- A preset only sets the attributes it lists. Each light preset the pushbuttons
  use carries one axis: `dim` the brightness, `night` the colour; `default` gives
  both axes their value.
- `default` is also the program default of its domain (what a `set` rule's end
  returns to), over the calendar's older `defaults:` and the built-in ones.
- The built-in presets below apply when the YAML leaves one out.
- `gungors.reload` reads them again.

Stored as ``hass.data[DOMAIN]["presets"]`` = {domain: {name: {attr: value}}} with the
internal attribute names of rules.py.
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN
from .rules import SUN_TEMPLATE, parse_defaults

DATA_PRESETS = "presets"
CONF_PRESETS = "presets"
PRESET_DOMAINS = ("light", "climate", "cover")

BUILTIN: dict[str, dict[str, dict[str, Any]]] = {
    "light": {
        "default": {"on": False, "color": (255, 255, 255), "brightness": 100.0},
        "dim": {"brightness": 10.0},
        "night": {"color": (255, 0, 0)},
    },
    "climate": {"default": {"hvac_mode": "off"}},
    "cover": {"default": {"position": SUN_TEMPLATE}},
}


def presets_schema(value: Any) -> dict[str, dict[str, dict[str, Any]]]:
    """`presets:` -> {domain: {name: {attr: value}}} (internal attribute names)."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise vol.Invalid("presets: a mapping of domain: {name: {attribute: value}}")
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for domain, presets in value.items():
        if domain not in PRESET_DOMAINS:
            raise vol.Invalid(f"presets: unknown domain '{domain}'")
        if not isinstance(presets, dict):
            raise vol.Invalid(f"presets: {domain}: a mapping of name: {{attribute: value}}")
        result[domain] = {}
        for name, values in presets.items():
            try:
                result[domain][str(name)] = parse_defaults(domain, values or {})
            except vol.Invalid as err:
                raise vol.Invalid(f"presets: {domain} {name}: {err.msg}") from err
    return result


@callback
def async_set_presets(hass: HomeAssistant, presets: dict[str, dict[str, dict[str, Any]]]) -> None:
    hass.data.setdefault(DOMAIN, {})[DATA_PRESETS] = presets


def all_presets(hass: HomeAssistant, domain: str) -> dict[str, dict[str, Any]]:
    """The presets of a domain: the YAML ones over the built-in ones."""
    yaml = hass.data.get(DOMAIN, {}).get(DATA_PRESETS, {}).get(domain, {})
    return {**BUILTIN.get(domain, {}), **yaml}


def preset(hass: HomeAssistant, domain: str, name: str) -> dict[str, Any] | None:
    return all_presets(hass, domain).get(name)


def yaml_default(hass: HomeAssistant, domain: str) -> dict[str, Any]:
    """The `default` preset of a domain as written in the YAML (empty when not given)."""
    return dict(hass.data.get(DOMAIN, {}).get(DATA_PRESETS, {}).get(domain, {}).get("default", {}))
