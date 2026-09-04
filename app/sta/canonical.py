"""Canonical datastream names and metadata.

Single source of truth for observed-property names, unit symbols, and CF
Conventions definition URLs across every source mapper. Source mappers must
resolve raw incoming names through :func:`resolve` and read the unit symbol
from ``.meta`` — never pass a source-provided unit string through to FROST.

Unit symbols follow pragmatic UCUM (``°C`` not ``Cel``, ``ppm`` not
``[ppm]``, ``deg`` not ``degrees``) so dashboards stay readable while every
target FROST server sees identical strings across sources.
"""

from __future__ import annotations

from enum import Enum
from typing import NamedTuple


class DatastreamMeta(NamedTuple):
    display_name: str
    unit: str
    definition: str


_CF = "https://cfconventions.org/Data/cf-standard-names/current/build/cf-standard-name-table.html"


class CanonicalDatastream(str, Enum):
    """Allowed observation field names across the entire system."""

    TEMPERATURE = "temperature"
    AIR_TEMPERATURE = "air_temperature"
    HUMIDITY = "humidity"
    RELATIVE_HUMIDITY = "relative_humidity"
    CO2 = "co2"
    PRESSURE = "pressure"
    AIR_PRESSURE = "air_pressure"
    WIND_SPEED = "wind_speed"
    WIND_DIRECTION = "wind_direction"
    PRECIPITATION = "precipitation"
    SOLAR_RADIATION = "solar_radiation"
    WATER_LEVEL = "water_level"
    PM2_5 = "pm2_5"
    PM10 = "pm10"
    DEW_POINT = "dew_point"
    HEAT_INDEX = "heat_index"
    WIND_CHILL = "wind_chill"
    WIND_GUST = "wind_gust"
    RAIN_RATE = "rain_rate"
    UV_INDEX = "uv_index"
    WET_BULB_TEMPERATURE = "wet_bulb_temperature"
    EVAPOTRANSPIRATION = "evapotranspiration"

    @property
    def meta(self) -> DatastreamMeta:
        return _META[self]


_META: dict[CanonicalDatastream, DatastreamMeta] = {
    CanonicalDatastream.TEMPERATURE: DatastreamMeta(
        "Air temperature", "°C", f"{_CF}#air_temperature",
    ),
    CanonicalDatastream.AIR_TEMPERATURE: DatastreamMeta(
        "Air temperature", "°C", f"{_CF}#air_temperature",
    ),
    CanonicalDatastream.HUMIDITY: DatastreamMeta(
        "Relative humidity", "%", f"{_CF}#relative_humidity",
    ),
    CanonicalDatastream.RELATIVE_HUMIDITY: DatastreamMeta(
        "Relative humidity", "%", f"{_CF}#relative_humidity",
    ),
    CanonicalDatastream.CO2: DatastreamMeta(
        "CO2 concentration", "ppm",
        f"{_CF}#mole_fraction_of_carbon_dioxide_in_air",
    ),
    CanonicalDatastream.PRESSURE: DatastreamMeta(
        "Air pressure", "hPa", f"{_CF}#air_pressure",
    ),
    CanonicalDatastream.AIR_PRESSURE: DatastreamMeta(
        "Air pressure", "hPa", f"{_CF}#air_pressure",
    ),
    # km/h retained: TGV sensors emit km/h; relabelling to m/s without value
    # conversion would silently corrupt observations. Follow-up: value
    # conversion via anchor points (per @znetsixe generalFunctions/convert).
    CanonicalDatastream.WIND_SPEED: DatastreamMeta(
        "Wind speed", "km/h", f"{_CF}#wind_speed",
    ),
    CanonicalDatastream.WIND_DIRECTION: DatastreamMeta(
        "Wind direction", "deg", f"{_CF}#wind_from_direction",
    ),
    CanonicalDatastream.PRECIPITATION: DatastreamMeta(
        "Precipitation", "mm", f"{_CF}#precipitation_amount",
    ),
    CanonicalDatastream.SOLAR_RADIATION: DatastreamMeta(
        "Solar radiation", "W/m2",
        f"{_CF}#surface_downwelling_shortwave_flux_in_air",
    ),
    CanonicalDatastream.WATER_LEVEL: DatastreamMeta(
        "Groundwater level", "m",
        f"{_CF}#water_surface_height_above_reference_datum",
    ),
    CanonicalDatastream.PM2_5: DatastreamMeta(
        "PM2.5 concentration", "ug/m3",
        f"{_CF}#mass_concentration_of_pm2p5_ambient_aerosol_particles_in_air",
    ),
    CanonicalDatastream.PM10: DatastreamMeta(
        "PM10 concentration", "ug/m3",
        f"{_CF}#mass_concentration_of_pm10_ambient_aerosol_particles_in_air",
    ),
    CanonicalDatastream.DEW_POINT: DatastreamMeta(
        "Dew point temperature", "°C", f"{_CF}#dew_point_temperature",
    ),
    CanonicalDatastream.HEAT_INDEX: DatastreamMeta(
        "Heat index", "°C", f"{_CF}#heat_index_of_air_temperature",
    ),
    CanonicalDatastream.WIND_CHILL: DatastreamMeta(
        "Wind chill", "°C", f"{_CF}#wind_chill_of_air_temperature",
    ),
    CanonicalDatastream.WIND_GUST: DatastreamMeta(
        "Wind gust speed", "km/h", f"{_CF}#wind_speed_of_gust",
    ),
    CanonicalDatastream.RAIN_RATE: DatastreamMeta(
        "Rainfall rate", "mm/h", f"{_CF}#rainfall_rate",
    ),
    CanonicalDatastream.UV_INDEX: DatastreamMeta(
        "UV index", "1", f"{_CF}#ultraviolet_index",
    ),
    CanonicalDatastream.WET_BULB_TEMPERATURE: DatastreamMeta(
        "Wet bulb temperature", "°C", f"{_CF}#wet_bulb_temperature",
    ),
    CanonicalDatastream.EVAPOTRANSPIRATION: DatastreamMeta(
        "Evapotranspiration", "mm", f"{_CF}#water_evapotranspiration_amount",
    ),
}


_ALIASES: dict[str, CanonicalDatastream] = {
    "temp": CanonicalDatastream.TEMPERATURE,
    "t": CanonicalDatastream.TEMPERATURE,
    "airtemperature": CanonicalDatastream.AIR_TEMPERATURE,
    "rh": CanonicalDatastream.RELATIVE_HUMIDITY,
    "co2concentration": CanonicalDatastream.CO2,
    "airpressure": CanonicalDatastream.AIR_PRESSURE,
    "windspeed": CanonicalDatastream.WIND_SPEED,
    "winddirection": CanonicalDatastream.WIND_DIRECTION,
    "solarradiation": CanonicalDatastream.SOLAR_RADIATION,
    "waterlevel": CanonicalDatastream.WATER_LEVEL,
    "groundwaterlevel": CanonicalDatastream.WATER_LEVEL,
    "pm25": CanonicalDatastream.PM2_5,
    "pm2.5": CanonicalDatastream.PM2_5,
    "p2": CanonicalDatastream.PM2_5,
    "dewpoint": CanonicalDatastream.DEW_POINT,
    "heatindex": CanonicalDatastream.HEAT_INDEX,
    "windchill": CanonicalDatastream.WIND_CHILL,
    "windgust": CanonicalDatastream.WIND_GUST,
    "rainrate": CanonicalDatastream.RAIN_RATE,
    "rainfallrate": CanonicalDatastream.RAIN_RATE,
    "uvindex": CanonicalDatastream.UV_INDEX,
    "ultravioletindex": CanonicalDatastream.UV_INDEX,
    "wetbulbtemperature": CanonicalDatastream.WET_BULB_TEMPERATURE,
    "et": CanonicalDatastream.EVAPOTRANSPIRATION,
}


def resolve(raw_name: str) -> CanonicalDatastream | None:
    """Map a raw observed-property name to its canonical enum member.

    Returns ``None`` if the name doesn't match a canonical member or a known
    alias — callers should log a WARNING and drop the observation rather than
    invent a datastream on the fly.
    """
    if not raw_name:
        return None
    key = raw_name.strip().lower().replace("-", "_").replace(" ", "_")
    try:
        return CanonicalDatastream(key)
    except ValueError:
        return _ALIASES.get(key) or _ALIASES.get(key.replace("_", ""))


assert set(_META.keys()) == set(CanonicalDatastream), (
    "Every CanonicalDatastream member must have a _META entry"
)
