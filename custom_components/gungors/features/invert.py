"""`invert`: the device's open is the room's close.

Flips positions between the room frame (what the wrapper shows and accepts) and
the device frame (what the link sends and reads). Open/close commands follow
automatically, because the link picks them from the device-frame position.
"""
from __future__ import annotations

from ..core import Feature, register

KEY = "invert"


@register("cover")
class Invert(Feature):
    feature_name = KEY

    def gw_to_device(self, position: float) -> float:
        return 100 - super().gw_to_device(position)

    def gw_from_device(self, position: float) -> float:
        return super().gw_from_device(100 - position)
