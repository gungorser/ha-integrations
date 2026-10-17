"""Program rules: the content of a `calendar.program` entry.

An entry's description holds the program as YAML, the rules grouped by domain::

    target: [Salon, light.melike_light]   # area ids/names and entity ids
    light:
      state: "on"              # a plain value is `eq`
      brightness_pct: {min: 50, set: true}
      rgb_color: [255, 0, 0]   # or a colour name
    climate:
      temperature: {min: 19, max: 22}

The names follow Home Assistant (state, attributes, service fields):
light `state` ("on"/"off"), `brightness_pct`, `rgb_color`, `color_temp_kelvin`;
climate `state` (the HVAC mode: heat/off), `temperature`; cover
`current_position`. Inside the integration the attributes keep short keys
(`on`, `brightness`, `color`, `color_temp`, `hvac_mode`, `temperature`,
`position`); the older names (those keys) and the older form with all attributes
under one `rules:` key are still read.

A rule is a constraint on one attribute: `eq`, `min` and/or `max`; `min`/`max`
only for the numeric attributes brightness_pct, temperature and current_position. A rule is
`let` (the default) or `set` (`set: true`): a `let` rule only limits changes (an
off light or heating satisfies it), a `set` rule also brings the device into it
(a set brightness, colour or colour temperature rule keeps the light on, a set
temperature rule keeps the heating on), and at the end of the program only a
`set` rule's attributes return to their defaults. A rule only touches the
targeted entities of its domain. Two programs conflict on an entity when they
overlap in time and the same attribute's constraints can't hold together
(`color` and `color_temp` count as one attribute: a light can't follow both;
a set rule's implied `on: true` / `hvac_mode: heat` counts too).
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import re
from typing import Any

import voluptuous as vol
import yaml

from homeassistant.util import color as color_util

# attribute -> domain
DOMAINS: dict[str, str] = {
    "on": "light",
    "brightness": "light",
    "color": "light",
    "color_temp": "light",
    "hvac_mode": "climate",
    "temperature": "climate",
    "position": "cover",
}
NUMERIC: dict[str, tuple[float, float]] = {
    "brightness": (0, 100),
    "color_temp": (1000, 10000),
    "temperature": (5, 35),
    "position": (0, 100),
}
# Attributes that only take an exact value.
EQ_ONLY = {"on", "hvac_mode", "color", "color_temp"}
CHOICES: dict[str, tuple[Any, ...]] = {
    "on": (True, False),
    "hvac_mode": ("heat", "off"),
}
# The name of each attribute in descriptions, defaults and attributes (Home Assistant's).
NAMES: dict[str, str] = {
    "on": "state",
    "brightness": "brightness_pct",
    "color": "rgb_color",
    "color_temp": "color_temp_kelvin",
    "hvac_mode": "state",
    "temperature": "temperature",
    "position": "current_position",
}
# A default may be a template, rendered when it is applied; e.g. covers by the sun:
SUN_TEMPLATE = "{{ 100 if is_state('sun.sun', 'above_horizon') else 0 }}"


def attr_of(domain: str, key: Any) -> str | None:
    """The attribute a name stands for in a domain (Home Assistant's name or the older key)."""
    if key is True:
        key = "on"  # YAML 1.1 reads a bare `on:` key as True
    for attr, name in NAMES.items():
        if DOMAINS[attr] == domain and key == name:
            return attr
    return key if DOMAINS.get(key) == domain else None


def is_template(value: Any) -> bool:
    return isinstance(value, str) and ("{{" in value or "{%" in value)


def external(attr: str, value: Any) -> Any:
    """A value as written in descriptions and defaults (light state "on"/"off", lists)."""
    if attr == "on" and isinstance(value, bool):
        return "on" if value else "off"
    return list(value) if isinstance(value, tuple) else value


# Attributes that are one dimension for conflicts.
SAME_DIMENSION = {"color": "color", "color_temp": "color"}
TOLERANCE = 0.05


@dataclass(frozen=True)
class Constraint:
    """eq, or a range [min, max] (either end open); `set`: the system applies it."""

    eq: Any = None
    min: float | None = None
    max: float | None = None
    set: bool = False

    def bounds(self) -> tuple[float, float]:
        if self.eq is not None:
            return float(self.eq), float(self.eq)
        low = self.min if self.min is not None else float("-inf")
        high = self.max if self.max is not None else float("inf")
        return low, high

    def clamp(self, value: float) -> float:
        low, high = self.bounds()
        return min(max(value, low), high)

    def holds(self, value: Any) -> bool:
        if self.eq is not None and not isinstance(self.eq, (int, float)):
            return value == self.eq
        if isinstance(self.eq, bool):
            return value == self.eq
        try:
            number = float(value)
        except (TypeError, ValueError):
            return False
        low, high = self.bounds()
        return low - TOLERANCE <= number <= high + TOLERANCE

    def as_dict(self) -> dict[str, Any]:
        if self.eq is not None:
            result: dict[str, Any] = {
                "eq": list(self.eq) if isinstance(self.eq, tuple) else self.eq
            }
        else:
            result = {k: v for k, v in (("min", self.min), ("max", self.max)) if v is not None}
        if self.set:
            result["set"] = True
        return result


@dataclass(frozen=True)
class Program:
    target: tuple[str, ...]
    rules: dict[str, Constraint] = field(default_factory=dict)

    def domains(self) -> set[str]:
        return {DOMAINS[attr] for attr in self.rules}


def _color(value: Any) -> tuple[int, int, int]:
    if isinstance(value, str):
        try:
            return tuple(color_util.color_name_to_rgb(value))  # type: ignore[return-value]
        except ValueError as err:
            raise vol.Invalid(f"unknown colour '{value}'") from err
    if isinstance(value, (list, tuple)) and len(value) == 3:
        rgb = tuple(int(v) for v in value)
        if all(0 <= v <= 255 for v in rgb):
            return rgb  # type: ignore[return-value]
    raise vol.Invalid(f"invalid colour {value!r}: a name or [r, g, b]")


def _constraint(attr: str, value: Any) -> Constraint:
    try:
        return _constraint_of(attr, value)
    except vol.Invalid as err:
        raise vol.Invalid(str(err.msg).replace(f"{attr}:", f"{NAMES[attr]}:", 1)) from err


def _constraint_of(attr: str, value: Any) -> Constraint:
    if isinstance(value, dict) and "set" in value:
        value = dict(value)
        flag = value.pop("set")
        if not isinstance(flag, bool):
            raise vol.Invalid(f"{attr}: set is true or false")
        if not value:
            raise vol.Invalid(f"{attr}: a value (eq, min or max) besides set")
        return replace(_constraint_of(attr, value), set=flag)
    if attr == "color":
        if isinstance(value, dict):
            if set(value) != {"eq"}:
                raise vol.Invalid("color: only an exact colour (eq)")
            value = value["eq"]
        return Constraint(eq=_color(value))
    if attr in CHOICES:
        if isinstance(value, dict):
            if set(value) != {"eq"}:
                raise vol.Invalid(f"{attr}: only an exact value (eq)")
            value = value["eq"]
        if attr == "hvac_mode":
            # YAML 1.1 reads a bare `off` as False.
            value = "off" if value is False else str(value).lower()
        elif attr == "on" and isinstance(value, str) and value.lower() in ("on", "off"):
            value = value.lower() == "on"
        if value not in CHOICES[attr]:
            shown = ("on", "off") if attr == "on" else CHOICES[attr]
            raise vol.Invalid(f"{attr}: one of {', '.join(map(str, shown))}")
        return Constraint(eq=value)
    low, high = NUMERIC[attr]

    def number(v: Any) -> float:
        try:
            result = float(v)
        except (TypeError, ValueError) as err:
            raise vol.Invalid(f"{attr}: {v!r} is not a number") from err
        if isinstance(v, bool) or not low <= result <= high:
            raise vol.Invalid(f"{attr}: {v!r} is outside {low:g}-{high:g}")
        return result

    if not isinstance(value, dict):
        return Constraint(eq=number(value))
    if attr in EQ_ONLY and set(value) != {"eq"}:
        raise vol.Invalid(f"{attr}: only an exact value (eq); min/max are for numbers")
    unknown = set(value) - {"eq", "min", "max"}
    if unknown or not value:
        raise vol.Invalid(f"{attr}: use eq, min and/or max")
    if "eq" in value:
        if len(value) > 1:
            raise vol.Invalid(f"{attr}: eq can't be combined with min/max")
        return Constraint(eq=number(value["eq"]))
    result = Constraint(
        min=number(value["min"]) if "min" in value else None,
        max=number(value["max"]) if "max" in value else None,
    )
    if result.min is not None and result.max is not None and result.min > result.max:
        raise vol.Invalid(f"{attr}: min is above max")
    return result


# A set rule on these keeps the device on: attr -> (attribute, value) it implies.
IMPLIES: dict[str, tuple[str, Any]] = {
    "brightness": ("on", True),
    "color": ("on", True),
    "color_temp": ("on", True),
    "temperature": ("hvac_mode", "heat"),
}


def implied(rules: dict[str, Constraint]) -> dict[str, Constraint]:
    """Rules plus what their set rules imply (`on: true`, `hvac_mode: heat`)."""
    result = dict(rules)
    for attr, constraint in rules.items():
        if constraint.set and attr in IMPLIES:
            other, value = IMPLIES[attr]
            if other not in result:
                result[other] = Constraint(eq=value, set=True)
    return result


def parse_program(description: str | None) -> Program:
    """Parse an entry description; raise vol.Invalid with a readable message."""
    if not description or not description.strip():
        raise vol.Invalid("the description must hold the program (target and rules)")
    try:
        data = yaml.safe_load(description)
    except yaml.YAMLError as err:
        raise vol.Invalid(f"the description is not valid YAML: {err}") from err
    if not isinstance(data, dict):
        raise vol.Invalid("the description must be a mapping with target and rules")
    domains = sorted(set(DOMAINS.values()))
    unknown = set(data) - {"target", "rules", *domains}
    if unknown:
        raise vol.Invalid(
            f"unknown keys: {', '.join(sorted(map(str, unknown)))}"
            f" (known: target, {', '.join(domains)})"
        )
    target = data.get("target")
    if isinstance(target, str):
        target = [target]
    if not isinstance(target, list) or not target or not all(
        isinstance(t, str) and t.strip() for t in target
    ):
        raise vol.Invalid("target: an area, an entity id or a list of them")
    pairs: list[tuple[Any, Any]] = []
    if "rules" in data:
        if not isinstance(data["rules"], dict):
            raise vol.Invalid("rules: a mapping of attribute: value")
        for key, value in data["rules"].items():
            key = "on" if key is True else key
            attr = next((a for d in domains if (a := attr_of(d, key)) is not None), key)
            pairs.append((attr, value))
    for domain in domains:
        if domain not in data:
            continue
        values = data[domain]
        if not isinstance(values, dict) or not values:
            raise vol.Invalid(f"{domain}: a mapping of attribute: value")
        for key, value in values.items():
            attr = attr_of(domain, key)
            if attr is None:
                known = ", ".join(NAMES[a] for a, d in DOMAINS.items() if d == domain)
                raise vol.Invalid(f"{domain}: unknown attribute '{key}' (known: {known})")
            if attr in (p[0] for p in pairs):
                raise vol.Invalid(f"{domain}: '{NAMES[attr]}' is given twice")
            pairs.append((attr, value))
    if not pairs:
        raise vol.Invalid(f"at least one rule under {', '.join(domains)}")
    parsed: dict[str, Constraint] = {}
    for attr, value in pairs:
        if attr not in DOMAINS:
            raise vol.Invalid(f"unknown rule '{attr}' (known: {', '.join(dict.fromkeys(NAMES.values()))})")
        if attr in parsed:
            raise vol.Invalid(f"'{NAMES[attr]}' is given twice")
        parsed[attr] = _constraint(attr, value)
    if "color" in parsed and "color_temp" in parsed:
        raise vol.Invalid("rgb_color and color_temp_kelvin can't be in the same program")
    for attr, constraint in parsed.items():
        if constraint.set and attr in IMPLIES:
            other, value = IMPLIES[attr]
            if other in parsed and parsed[other].eq != value:
                raise vol.Invalid(
                    f"set {NAMES[attr]} keeps {NAMES[other]} at {_scalar(external(other, value))}"
                )
    return Program(target=tuple(t.strip() for t in target), rules=parsed)


_PLAIN = re.compile(r"^[^\s\-?:,\[\]{}#&*!|>'\"%@`][^:#,\[\]{}]*$")
_RESERVED = re.compile(
    r"^(y|n|yes|no|true|false|on|off|null|~|[-+.]?\d.*|\.(inf|nan))$", re.IGNORECASE
)


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(int(value)) if float(value).is_integer() else str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_scalar(v) for v in value) + "]"
    text = str(value)
    if _PLAIN.match(text) and not _RESERVED.match(text) and text == text.strip():
        return text
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def dump_program(program: Program) -> str:
    """The description text of a program (YAML, rules grouped by domain)."""
    lines = [f"target: {_scalar(list(program.target))}"]
    for domain in dict.fromkeys(DOMAINS.values()):
        rules = {a: c for a, c in program.rules.items() if DOMAINS[a] == domain}
        if not rules:
            continue
        lines.append(f"{domain}:")
        for attr, constraint in rules.items():
            as_dict = constraint.as_dict()
            if "eq" in as_dict:
                as_dict["eq"] = external(attr, constraint.eq)
            if "eq" in as_dict and len(as_dict) == 1:
                value = _scalar(as_dict["eq"])
            else:
                value = "{" + ", ".join(f"{k}: {_scalar(v)}" for k, v in as_dict.items()) + "}"
            lines.append(f"  {NAMES[attr]}: {value}")
    return "\n".join(lines) + "\n"


def constraints_conflict(attr: str, a: Constraint, b: Constraint) -> bool:
    """Whether two constraints on one attribute can't hold together."""
    if attr in CHOICES or attr == "color":
        return a.eq != b.eq
    low_a, high_a = a.bounds()
    low_b, high_b = b.bounds()
    return max(low_a, low_b) > min(high_a, high_b) + TOLERANCE


def rules_conflict(
    a: dict[str, Constraint], b: dict[str, Constraint], domain: str
) -> str | None:
    """The first attribute on which two rule sets conflict for a domain (its name), or None."""
    a = {k: v for k, v in implied(a).items() if DOMAINS[k] == domain}
    b = {k: v for k, v in implied(b).items() if DOMAINS[k] == domain}
    for attr, constraint in a.items():
        if attr in b and constraints_conflict(attr, constraint, b[attr]):
            return NAMES[attr]
        other = next(
            (o for o in b if o != attr and SAME_DIMENSION.get(o, o) == SAME_DIMENSION.get(attr)),
            None,
        )
        if other is not None:
            return f"{NAMES[attr]}/{NAMES[other]}"
    return None


def _check_template(name: str, text: str) -> None:
    import jinja2  # noqa: PLC0415  (Home Assistant's own dependency)

    try:
        jinja2.Environment().parse(text)
    except jinja2.TemplateSyntaxError as err:
        raise vol.Invalid(f"{name}: template error: {err}") from err


def parse_defaults(domain: str, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the default values of a domain: {name: value} -> {attr: value}.

    A value may be a template (rendered with default_value when applied);
    the older cover `position: sun` reads as SUN_TEMPLATE.
    """
    if not isinstance(data, dict):
        raise vol.Invalid(f"{domain} defaults: a mapping of attribute: value")
    result: dict[str, Any] = {}
    for key, value in data.items():
        attr = attr_of(domain, key)
        if attr is None:
            raise vol.Invalid(f"{domain} defaults: unknown attribute '{key}'")
        if attr == "position" and value == "sun":
            value = SUN_TEMPLATE
        if is_template(value):
            _check_template(f"{domain} defaults: {NAMES[attr]}", value)
            result[attr] = value
            continue
        try:
            result[attr] = default_value(attr, value)
        except vol.Invalid as err:
            raise vol.Invalid(f"{domain} defaults: {err.msg}") from err
    return result


def default_value(attr: str, value: Any) -> Any:
    """One default value (a rendered template too), checked: the attribute's exact value."""
    if attr == "on" and isinstance(value, str) and value.lower() in ("true", "false"):
        value = value.lower() == "true"
    if attr in NUMERIC and isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            pass
    constraint = _constraint(attr, value)
    if constraint.eq is None:
        raise vol.Invalid(f"{NAMES[attr]} needs a single value")
    return constraint.eq


def defaults_json(defaults: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Defaults as written (Home Assistant's names, plain JSON)."""
    return {
        domain: {NAMES[a]: external(a, v) for a, v in values.items()}
        for domain, values in defaults.items()
    }
