"""Program rules: the content of a `calendar.program` entry.

An entry's description holds the program as JSON or YAML::

    target: [salon, light.melike_light]   # area ids/names and entity ids
    rules:
      on: true                 # a plain value is `eq`
      brightness: {max: 40}    # percent
      color: red               # a colour name or [r, g, b]
      temperature: {min: 19, max: 22}

A rule is a constraint on one attribute: `eq`, `min` and/or `max`. Each attribute
belongs to one domain, so a rule only touches the targeted entities of that
domain. Two programs conflict on an entity when they overlap in time and the
same attribute's constraints can't hold together (`color` and `color_temp`
count as one attribute: a light can't follow both).
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
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
CHOICES: dict[str, tuple[Any, ...]] = {
    "on": (True, False),
    "hvac_mode": ("heat", "off"),
}
# Attributes that are one dimension for conflicts.
SAME_DIMENSION = {"color": "color", "color_temp": "color"}
TOLERANCE = 0.05


@dataclass(frozen=True)
class Constraint:
    """eq, or a range [min, max] (either end open)."""

    eq: Any = None
    min: float | None = None
    max: float | None = None

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
            return {"eq": list(self.eq) if isinstance(self.eq, tuple) else self.eq}
        return {k: v for k, v in (("min", self.min), ("max", self.max)) if v is not None}


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
            value = str(value).lower()
        if value not in CHOICES[attr]:
            raise vol.Invalid(f"{attr}: one of {', '.join(map(str, CHOICES[attr]))}")
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


def parse_program(description: str | None) -> Program:
    """Parse an entry description; raise vol.Invalid with a readable message."""
    if not description or not description.strip():
        raise vol.Invalid("the description must hold the program (target and rules)")
    try:
        data = yaml.safe_load(description)
    except yaml.YAMLError as err:
        raise vol.Invalid(f"the description is not valid JSON/YAML: {err}") from err
    if not isinstance(data, dict):
        raise vol.Invalid("the description must be a mapping with target and rules")
    unknown = set(data) - {"target", "rules"}
    if unknown:
        raise vol.Invalid(f"unknown keys: {', '.join(sorted(map(str, unknown)))}")
    target = data.get("target")
    if isinstance(target, str):
        target = [target]
    if not isinstance(target, list) or not target or not all(
        isinstance(t, str) and t.strip() for t in target
    ):
        raise vol.Invalid("target: an area, an entity id or a list of them")
    rules = data.get("rules")
    if not isinstance(rules, dict) or not rules:
        raise vol.Invalid("rules: at least one rule")
    parsed: dict[str, Constraint] = {}
    for attr, value in rules.items():
        if attr is True:  # YAML 1.1 reads a bare `on:` key as True
            attr = "on"
        if attr not in DOMAINS:
            raise vol.Invalid(f"unknown rule '{attr}' (known: {', '.join(DOMAINS)})")
        parsed[attr] = _constraint(attr, value)
    if "color" in parsed and "color_temp" in parsed:
        raise vol.Invalid("color and color_temp can't be in the same program")
    return Program(target=tuple(t.strip() for t in target), rules=parsed)


def dump_program(program: Program) -> str:
    """The description text of a program (JSON)."""
    rules: dict[str, Any] = {}
    for attr, constraint in program.rules.items():
        as_dict = constraint.as_dict()
        rules[attr] = as_dict["eq"] if "eq" in as_dict else as_dict
    return json.dumps({"target": list(program.target), "rules": rules})


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
    """The first attribute on which two rule sets conflict for a domain, or None."""
    a = {k: v for k, v in a.items() if DOMAINS[k] == domain}
    b = {k: v for k, v in b.items() if DOMAINS[k] == domain}
    for attr, constraint in a.items():
        if attr in b and constraints_conflict(attr, constraint, b[attr]):
            return attr
        other = next(
            (o for o in b if o != attr and SAME_DIMENSION.get(o, o) == SAME_DIMENSION.get(attr)),
            None,
        )
        if other is not None:
            return f"{attr}/{other}"
    return None


def parse_defaults(domain: str, data: dict[str, Any]) -> dict[str, Any]:
    """Validate the default values of a domain: {attr: value}.

    A cover's `position` may be `sun` (open while the sun is up, closed at night).
    """
    if not isinstance(data, dict):
        raise vol.Invalid(f"{domain} defaults: a mapping of attribute: value")
    result: dict[str, Any] = {}
    for attr, value in data.items():
        if attr is True:
            attr = "on"
        if DOMAINS.get(attr) != domain:
            raise vol.Invalid(f"{domain} defaults: unknown attribute '{attr}'")
        if attr == "position" and value == "sun":
            result[attr] = "sun"
            continue
        constraint = _constraint(attr, value)
        if constraint.eq is None:
            raise vol.Invalid(f"{domain} defaults: {attr} needs a single value")
        result[attr] = constraint.eq
    return result
