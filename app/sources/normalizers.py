"""Concrete Normalizer subclasses for each sensor source.

Each normalizer declares Pydantic fields matching the vendor payload shape and
a NAME_TRANSFORM mapping vendor field names to CanonicalDatastream members.
"""

from __future__ import annotations

from typing import ClassVar

from app.sta.canonical import CanonicalDatastream
from app.sta.normalizer import Normalizer


class OhnicsNormalizer(Normalizer):
    """Ohnics air quality sensors: P2 = PM2.5, T = temperature."""

    P2: float | None = None
    T: float | None = None

    NAME_TRANSFORM: ClassVar[dict[str, CanonicalDatastream]] = {
        "P2": CanonicalDatastream.PM2_5,
        "T": CanonicalDatastream.AIR_TEMPERATURE,
    }


class LevellogNormalizer(Normalizer):
    """Levellog groundwater sensors: water_level only."""

    water_level: float | None = None

    NAME_TRANSFORM: ClassVar[dict[str, CanonicalDatastream]] = {
        "water_level": CanonicalDatastream.WATER_LEVEL,
    }
